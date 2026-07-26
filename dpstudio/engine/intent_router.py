"""
dpstudio/engine/intent_router.py

Cheap classification call: routes target_features and a complexity hint from
the raw prompt. Never sees skill file content, never produces plan content --
that separation is deliberate so it can't hallucinate a signal id it was never
shown.
"""
from __future__ import annotations

import re

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

   CRITICAL -- COMPLETENESS: if the prompt names multiple capabilities (e.g.
   "serverless AND liquid clustering AND photon", or "assess X, Y, and Z"),
   target_features MUST include every one of them. Do not narrow the list to
   only the most prominent, most recently mentioned, or most specific capability.
   A request combining three named features is a request to evaluate all three
   together -- dropping one silently produces an incomplete plan that looks
   complete, which is the single most dangerous failure mode this router can
   produce. Re-read the prompt once specifically checking that every registered
   feature name or its synonyms that appear in the text also appears in your
   target_features list before finalizing your answer.

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

# Synonyms used for the code-level cross-check below. Kept intentionally small
# and literal -- this is a safety net catching an obvious keyword miss, not a
# second classifier.
_FEATURE_KEYWORDS = {
    "serverless": ["serverless"],
    "liquid_clustering": ["liquid clustering", "liquid_clustering", "cluster by", "clustering"],
    "photon": ["photon"],
}


def _keyword_cross_check(prompt: str, target_features: list[str], registered: list[str]) -> list[str]:
    """Code-level safety net: same pattern as dna_check and normalize_table_physical
    elsewhere in this codebase -- an instruction alone has already proven capable
    of silently dropping an explicitly-named feature (confirmed live: a prompt
    naming serverless, liquid clustering, and photon together classified only
    serverless and photon). This does not replace the LLM's judgment; it only
    flags an obvious literal-keyword miss for visibility.
    """
    prompt_lower = prompt.lower()
    missed = []
    for feat in registered:
        if feat in target_features:
            continue
        keywords = _FEATURE_KEYWORDS.get(feat, [feat.replace("_", " ")])
        if any(kw in prompt_lower for kw in keywords):
            missed.append(feat)
    return missed


def classify(prompt: str, skillset: SkillSet, llm) -> dict:
    registered = skillset.registered_features()
    system = SYSTEM_PROMPT.format(registered_features=registered)
    result = llm.complete_json(system=system, user=prompt)

    missed = _keyword_cross_check(prompt, result.get("target_features", []), registered)
    if missed:
        result.setdefault("target_features", [])
        result["target_features"] = list(dict.fromkeys(result["target_features"] + missed))
        result["router_notes"] = (
            f"keyword_cross_check added {missed} -- these feature names appeared "
            f"literally in the prompt but were missing from the classifier's own "
            f"target_features list."
        )

    return result
