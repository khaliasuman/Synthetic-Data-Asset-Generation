"""
dpstudio/engine/llm.py

Thin wrapper around Databricks' Foundation Model API (Mosaic AI Model Serving).
Used instead of calling api.anthropic.com directly, because Free Edition
restricts outbound internet to a trusted-domain allowlist -- the Foundation
Model API is served from inside the Databricks control plane, so no external
egress is needed.

Uses the OpenAI-compatible client, which Databricks Model Serving supports
natively. Endpoint name must be verified in YOUR workspace's Serving tab --
it varies by workspace/region and changes over time, so don't hardcode a
model string without checking.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    raw: dict


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
        text = resp.choices[0].message.content
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
