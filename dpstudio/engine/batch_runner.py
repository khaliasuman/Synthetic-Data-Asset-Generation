"""
dpstudio/engine/batch_runner.py

Bulk generation via Anthropic's Message Batches API -- 50% off both input and
output tokens, combinable with prompt caching for the static grammar+client-dna
block. Use this for regression-suite / overnight bulk generation where results
don't need to come back immediately; NOT a replacement for the live interactive
pipeline (pipeline.run), which still needs synchronous responses.

Batch classification and planning are each submitted as their own batch, since
planning depends on each prompt's classification result -- run classify_batch()
first, then feed its target_features into plan_batch().
"""
from __future__ import annotations

import json
import time

from .skills import SkillSet
from .intent_router import SYSTEM_PROMPT as CLASSIFIER_SYSTEM_PROMPT
from .planner import _build_system_prompt as build_planner_system_blocks


def submit_classify_batch(client, prompts: list[str], skillset: SkillSet, model: str):
    """Submits one classification request per prompt as a single batch job.
    Returns the batch id; poll with wait_for_batch() then read with
    read_batch_results()."""
    registered = skillset.registered_features()
    system = CLASSIFIER_SYSTEM_PROMPT.format(registered_features=registered)

    requests = [
        {
            "custom_id": f"classify-{i}",
            "params": {
                "model": model,
                "max_tokens": 500,
                "system": system,
                "messages": [{"role": "user", "content": p}],
            },
        }
        for i, p in enumerate(prompts)
    ]
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def submit_plan_batch(client, prompt_and_features: list[tuple[str, list[str]]],
                      skillset: SkillSet, model: str):
    """prompt_and_features: [(prompt, target_features), ...] -- typically the
    output of a completed classify batch. Each request reuses the SAME cached
    static block (grammar+client-dna) via cache_control, so even across a large
    batch the static portion should mostly hit cache after the first few
    requests process (subject to the ~5-minute cache window and Anthropic's
    batch processing order, which isn't strictly sequential -- cache benefit
    within a batch is a bonus, not a guarantee, unlike the live pipeline where
    it's much more reliable)."""
    requests = []
    for i, (prompt, target_features) in enumerate(prompt_and_features):
        blocks = build_planner_system_blocks(skillset, target_features)
        requests.append({
            "custom_id": f"plan-{i}",
            "params": {
                "model": model,
                "max_tokens": 4000,
                "system": blocks,
                "messages": [{"role": "user", "content": prompt}],
            },
        })
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def wait_for_batch(client, batch_id: str, poll_seconds: int = 15, timeout_s: int = 3600):
    """Blocks until the batch finishes. Batches can take minutes to hours
    depending on load -- this is the tradeoff for the 50% discount. Only use
    this for workloads where you don't need the result right away."""
    t0 = time.time()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        if time.time() - t0 > timeout_s:
            raise TimeoutError(f"Batch {batch_id} did not finish within {timeout_s}s "
                              f"(status: {batch.processing_status})")
        time.sleep(poll_seconds)


def read_batch_results(client, batch_id: str) -> dict[str, dict]:
    """Returns {custom_id: parsed_json_result} for every request in the batch.
    A request that errored or was refused shows up with an 'error' key instead
    of the parsed result -- always check for that before trusting an entry."""
    results = {}
    for entry in client.messages.batches.results(batch_id):
        custom_id = entry.custom_id
        if entry.result.type != "succeeded":
            results[custom_id] = {"error": str(entry.result)}
            continue
        text = "".join(b.text for b in entry.result.message.content if b.type == "text")
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            results[custom_id] = json.loads(text)
        except json.JSONDecodeError:
            results[custom_id] = {"error": "non-JSON response", "raw_text": text}
    return results


def estimate_batch_savings(prompt_count: int, avg_input_tokens: int, avg_output_tokens: int,
                           model: str, cost_table: dict) -> dict:
    """Quick before/after estimate -- run this before submitting to sanity-check
    whether batch is worth it for a given prompt_count. Batch is always 50% off
    regardless of caching; caching stacks on top of that for the static block."""
    rates = cost_table.get(model, {"input": 0.0, "output": 0.0})
    live_cost = prompt_count * (
        (avg_input_tokens / 1_000_000) * rates["input"]
        + (avg_output_tokens / 1_000_000) * rates["output"]
    )
    batch_cost = live_cost * 0.5
    return {
        "prompt_count": prompt_count,
        "estimated_live_cost_usd": round(live_cost, 4),
        "estimated_batch_cost_usd": round(batch_cost, 4),
        "estimated_savings_usd": round(live_cost - batch_cost, 4),
    }
