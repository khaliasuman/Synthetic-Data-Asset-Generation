"""
dpstudio/engine/intent_router.py

Cheap classification call: routes target_features and a complexity hint from
the raw prompt. Never sees skill file content, never produces plan content --
that separation is deliberate so it can't hallucinate a signal id it was never
shown.
"""
from __future__ import annotations

from .llm import DatabricksLLM, StubLLM
from .skills import SkillSet

SYSTEM_PROMPT = """You are the intent router for a synthetic Databricks asset bundle \
generator. You do not generate plans, bundles, or any content. You only classify a \
request and output routing instructions as JSON.

Given a user prompt requesting a synthetic test scenario, determine:

1. target_features: which capability skills the request concerns, from this exact \
registered set: {registered_features}. A request may name one, several, or none \
explicitly. If a named capability is not in this set, do not invent an id --
set needs_clarification true instead.

2. complexity_hint: "simple", "moderate", or "high", based on language cues only.

3. scenario_type_hint: one of "positive", "negative", "edge", "distractor", "mixed", \
or null if unstated.

4. explicit_signals: any signal-like phrases the user names directly, verbatim -- do \
not translate these to canonical ids yourself.

Output ONLY this JSON shape, nothing else, no markdown fences:
{{
  "target_features": ["<feature_id>", ...],
  "confidence": "high" | "low",
  "complexity_hint": "simple" | "moderate" | "high",
  "scenario_type_hint": "positive" | "negative" | "edge" | "distractor" | "mixed" | null,
  "explicit_signals": ["<verbatim phrase>", ...],
  "needs_clarification": true | false,
  "clarification_reason": "<one sentence, or null>"
}}
"""


def classify(prompt: str, skillset: SkillSet, llm) -> dict:
    registered = skillset.registered_features()
    system = SYSTEM_PROMPT.format(registered_features=registered)
    return llm.complete_json(system=system, user=prompt)
