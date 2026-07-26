"""
dpstudio/engine/oracle.py

Computes plan.expected deterministically from a plan's code_graph/assets and the
signal/eligibility/interaction data in the loaded feature skills. No LLM call.
This is ground truth for the regression suite, so it must never call a model
and must never be influenced by what the planner "thinks" the verdict should be.

Build and test this BEFORE wiring the planner LLM call — everything here is
pure functions over plan JSON, so it's the cheapest part of the system to get
right and the most important to trust.
"""
from __future__ import annotations

import re
from typing import Any

from .skills import SkillSet

_STRENGTH_FALLBACK = {"eligible": 0, "review_before_serverless": 1, "blocked": 2}


def _match(sig: dict, flat: dict) -> bool:
    dim, m = sig["dimension"], sig["match"]
    corpus_fields = ("api_usage", "local_filesystem", "runtime_config", "storage_path",
                      "checkpoint_location", "sql_semantics", "state_management")
    if any(k in m for k in ("any_pattern", "or_any_pattern")):
        patterns = m.get("any_pattern", []) + m.get("or_any_pattern", [])
        corpus = "\n".join(str(x) for f in corpus_fields for x in _as_list(flat.get(f, [])))
        if any(re.search(p, corpus, re.M) for p in patterns):
            return True
    if "equals" in m and flat.get(dim) == m["equals"]:
        return True
    field_map = {
        "library_type_in": "library_types", "language_in": "languages",
        "compute_binding_in": "compute_binding", "udf_kind_in": "udf_kinds",
        "or_udf_kind_in": "udf_kinds", "format_in": "format",
        "workload_type_in": "workload_type", "job_trigger_in": "job_trigger",
        "egress_kind_in": "network_egress",
    }
    for key, field in field_map.items():
        if key in m:
            vals = _as_list(flat.get(field, []))
            if set(m[key]) & set(vals):
                return True
    if "format_not_in" in m:
        f = flat.get("format")
        if f and f not in m["format_not_in"]:
            return True
    if "any_key" in m and set(m["any_key"]) & set(flat.get("config_keys", [])):
        return True
    if "all_keys_present" in m and set(m["all_keys_present"]) <= set(flat.get("table_physical", {})):
        return True
    if "key_count_gt" in m:
        c = m["key_count_gt"]
        if len(flat.get("table_physical", {}).get(c["field"], [])) > c["max"]:
            return True
    if "any_of" in m:
        for sub in m["any_of"]:
            for kk, vv in sub.items():
                base = kk.rsplit("_", 1)[0]
                if kk.endswith("_gt") and flat.get(base, 0) > vv:
                    return True
                if kk.endswith("_lt") and flat.get(base, 10**18) < vv:
                    return True
    return False


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _flatten(nodes: dict[str, dict], asset: dict) -> dict:
    """Union EXECUTABLE surfaces across the code graph. Distractors never enter here."""
    flat = {
        "api_usage": [], "local_filesystem": [], "runtime_config": [],
        "config_keys": asset.get("config_keys", []),
        "library_types": [l.get("library_type") for l in asset.get("libraries", [])],
        "languages": _as_list(asset.get("language")),
        "compute_binding": asset.get("compute_binding"),
        "network_egress": asset.get("network_egress", []),
        "streaming_trigger": asset.get("streaming_trigger"),
        "udf_kinds": asset.get("udf_kinds", []),
        "format": asset.get("format"),
        "workload_type": asset.get("workload_type"),
        "job_trigger": asset.get("job_trigger"),
        "table_physical": asset.get("table_physical", {}),
        "storage_path": asset.get("storage_path", []),
        "checkpoint_location": asset.get("checkpoint_location", []),
    }
    for node in nodes.values():
        for line in node.get("executable", []):
            flat["api_usage"].append(line)
    return flat


def evaluate_feature(skill, flat: dict, nodes: dict[str, dict]) -> dict:
    """Match every signal in one feature skill against the flattened bundle."""
    matched = []
    for sig in skill.data["signals"]:
        if _match(sig, flat):
            node_id = "task_config"
            if sig["dimension"] == "api_usage":
                for nid, node in nodes.items():
                    text = "\n".join(node.get("executable", []))
                    if any(re.search(p, text, re.M) for p in sig["match"].get("any_pattern", [])):
                        node_id = nid
                        break
            matched.append({
                "signal_id": sig["id"], "feature": skill.name, "node_id": node_id,
                "dimension": sig["dimension"], "status": sig.get("status", "verified"),
                "evidence_surface": "executable",
            })

    elig = {e["signal"]: e["verdict"] for e in skill.data["eligibility_signals"]}
    order = skill.data.get("vocabulary", {}).get("verdict_strength_order")
    verdicts = [elig[m["signal_id"]] for m in matched if m["signal_id"] in elig]
    if not verdicts:
        verdict = "eligible" if skill.name == "serverless" else "not_applicable"
    elif order:
        verdict = max(verdicts, key=lambda v: order.index(v) if v in order else -1)
    else:
        verdict = max(verdicts, key=lambda v: _STRENGTH_FALLBACK.get(v, 0))
    return {"feature": skill.name, "verdict": verdict, "matched_signals": matched}


def resolve_interactions(per_feature: list[dict], asset: dict, skillset: SkillSet) -> list[dict]:
    """Grammar's feature_interactions, driven by each feature skill's own declarations."""
    applied = []
    by_feature = {p["feature"]: p for p in per_feature}

    for p in per_feature:
        skill = skillset.feature(p["feature"])
        managed = {c["capability"] for c in skill.data.get("platform_managed_capabilities", [])}
        for other in per_feature:
            if other is p:
                continue
            other_skill = skillset.feature(other["feature"])
            cap = other_skill.data.get("recommends_capability")
            if cap and cap in managed and asset.get("compute_binding") == p.get("_compute_hint"):
                pass  # handled below via explicit compute_binding check

    # explicit, simpler pass matching the grammar's two documented rules:
    sv = by_feature.get("serverless")
    if sv:
        managed = {c["capability"] for c in skillset.feature("serverless")
                   .data.get("platform_managed_capabilities", [])}
        if asset.get("compute_binding") == "serverless":
            for p in per_feature:
                cap = skillset.feature(p["feature"]).data.get("recommends_capability")
                if cap and cap in managed and p["feature"] != "serverless":
                    p["verdict"] = "not_applicable"
                    applied.append({"rule_id": "platform_managed_capability_is_moot",
                                    "winner_feature": "serverless", "loser_feature": p["feature"]})
        if sv["verdict"] == "blocked":
            for p in per_feature:
                if p["feature"] != "serverless" and p["verdict"] not in ("not_applicable",):
                    applied.append({"rule_id": "blocking_beats_optimizing",
                                    "winner_feature": "serverless", "loser_feature": p["feature"]})
    return applied


def run(plan: dict, skillset: SkillSet) -> dict:
    """Compute plan.expected. Called once, before materialization, and frozen."""
    nodes = {n["node_id"]: plan["_node_code"][n["node_id"]] for n in plan["code_graph"]["nodes"]}
    asset = plan["assets"][0]  # single-asset plans for now; extend for multi-asset later
    flat = _flatten(nodes, asset)

    per_feature = [evaluate_feature(skillset.feature(f), flat, nodes)
                   for f in plan["target_features"]]
    interactions = resolve_interactions(per_feature, asset, skillset)

    all_signals = [m for p in per_feature for m in p["matched_signals"]]
    strongest = per_feature[0]["verdict"] if per_feature else "eligible"
    order_pref = ["blocked", "review_before_serverless", "not_recommended",
                  "review_before_clustering", "review_before_photon", "recommended",
                  "already_optimal", "not_applicable", "eligible", "neutral"]
    for p in per_feature:
        if order_pref.index(p["verdict"]) < order_pref.index(strongest) if p["verdict"] in order_pref and strongest in order_pref else False:
            strongest = p["verdict"]

    return {
        "per_feature": per_feature,
        "verdict": per_feature[0]["verdict"] if len(per_feature) == 1 else strongest,
        "matched_signals": all_signals,
        "interactions_applied": interactions,
        "oracle_version": skillset.grammar.data["version"],
    }
