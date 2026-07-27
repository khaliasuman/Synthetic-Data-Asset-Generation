"""
dpstudio/engine/llm.py

Thin wrappers for the LLM calls this pipeline makes, plus lightweight
observability: every call's latency, token counts, and estimated cost are
recorded, since the planner call is both the most expensive and the least
reliable step in the pipeline and is the natural target for a future
architectural shift (deterministic composition, LLM only for intent + content).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# Rough public per-MTok rates for observability only -- not billing-accurate,
# just enough to compare approaches/models directionally. Update as needed.
_COST_PER_MTOK = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
}


@dataclass
class LLMResponse:
    text: str
    raw: dict


@dataclass
class CallLog:
    """One entry per LLM call. Append to CALL_LOG for the lifetime of a session;
    a UI or notebook can read this directly for a running cost/latency view."""
    model: str
    latency_s: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


CALL_LOG: list[CallLog] = []


def _record(model: str, latency_s: float, input_tokens: int, output_tokens: int) -> CallLog:
    rates = _COST_PER_MTOK.get(model, {"input": 0.0, "output": 0.0})
    cost = (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]
    entry = CallLog(model=model, latency_s=latency_s, input_tokens=input_tokens,
                    output_tokens=output_tokens, estimated_cost_usd=cost)
    CALL_LOG.append(entry)
    return entry


def session_summary() -> dict:
    """Quick aggregate view -- total calls, latency, tokens, cost this session."""
    if not CALL_LOG:
        return {"calls": 0}
    return {
        "calls": len(CALL_LOG),
        "total_latency_s": round(sum(c.latency_s for c in CALL_LOG), 2),
        "total_input_tokens": sum(c.input_tokens for c in CALL_LOG),
        "total_output_tokens": sum(c.output_tokens for c in CALL_LOG),
        "total_estimated_cost_usd": round(sum(c.estimated_cost_usd for c in CALL_LOG), 4),
        "by_model": {
            m: {"calls": sum(1 for c in CALL_LOG if c.model == m),
                "cost_usd": round(sum(c.estimated_cost_usd for c in CALL_LOG if c.model == m), 4)}
            for m in {c.model for c in CALL_LOG}
        },
    }


class DatabricksLLM:
    def __init__(self, endpoint: str, base_url: str | None = None, token: str | None = None):
        """
        endpoint: the Model Serving endpoint name, e.g. "databricks-claude-sonnet-4"
                  -- CHECK your workspace's Serving tab for the actual available name.
        base_url/token: default to the notebook's own auth context when run inside
                  Databricks (WorkspaceClient picks these up automatically); pass
                  explicitly only when running outside a notebook.
        """
        self.endpoint = endpoint
        self._client = self._build_client(base_url, token)

    def _build_client(self, base_url, token):
        from openai import OpenAI
        from databricks.sdk import WorkspaceClient

        if base_url and token:
            return OpenAI(base_url=base_url, api_key=token)

        # Inside a Databricks notebook, WorkspaceClient() picks up auth automatically.
        w = WorkspaceClient()
        return OpenAI(
            base_url=f"{w.config.host}/serving-endpoints",
            api_key=w.config.token,
        )

    def complete(self, system: str, user: str, max_tokens: int = 2000,
                 temperature: float = 0.0) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self.endpoint,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content
        # Reasoning-style models (e.g. gpt-oss) return a list of typed blocks
        # ({"type": "reasoning", ...}, {"type": "text", ...}) instead of a plain
        # string. Extract only the actual answer text, skip reasoning/thinking blocks.
        if isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = content
        return LLMResponse(text=text, raw=resp.model_dump())

    def complete_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        """Calls complete() and parses the result as JSON, stripping code fences
        if the model wraps its output in ```json ... ``` despite instructions not to."""
        r = self.complete(system, user, max_tokens=max_tokens)
        text = r.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text)


class AnthropicLLM:
    """Direct calls to api.anthropic.com using your own API key. Confirmed reachable
    from Free Edition (both api.anthropic.com and api.openai.com return real HTTP
    responses rather than connection errors -- egress isn't blocked for these).

    Prefer dbutils.secrets over a hardcoded key once this leaves quick testing --
    see the notebook entrypoint for the pattern.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 2000,
                 temperature: float = 0.0) -> LLMResponse:
        t0 = time.time()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency = time.time() - t0
        _record(self.model, latency, resp.usage.input_tokens, resp.usage.output_tokens)
        # Anthropic responses are a list of content blocks; take the text ones.
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResponse(text=text, raw=resp.model_dump())

    def complete_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        r = self.complete(system, user, max_tokens=max_tokens)
        text = r.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text)


class StubLLM:
    """Deterministic stand-in for local testing without a live endpoint.
    Swap DatabricksLLM in for this once running inside the workspace."""

    def __init__(self, canned: dict[str, dict]):
        self.canned = canned  # keyed by a short tag the caller passes in `user`

    def complete_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        for tag, payload in self.canned.items():
            if tag in user:
                return payload
        raise KeyError(f"StubLLM has no canned response matching any tag in: {user[:200]}")
