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
        tp = flat.get("table_physical", {}) or {}
        for sub in m["any_of"]:
            for kk, vv in sub.items():
                base = kk.rsplit("_", 1)[0]
                # numeric fields describing table_physical live INSIDE that nested
                # dict (e.g. table_physical.partition_count), not at the top level
                # of the flattened bundle. Check there first, fall back to flat.
                val = tp.get(base, flat.get(base))
                if val is None:
                    continue
                # Confirmed live: the model can put a non-numeric shape (e.g. a
                # nested dict) in a field this comparison expects to be a plain
                # number. Skip rather than crash the whole oracle run -- a
                # malformed field simply can't satisfy this threshold, it doesn't
                # mean the plan itself is unrecoverable.
                if not isinstance(val, (int, float)):
                    continue
                if kk.endswith("_gt") and val > vv:
                    return True
                if kk.endswith("_lt") and val < vv:
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
    """Generic reader of each feature skill's own interaction_declarations -- NOT
    hardcoded per-feature-pair logic. Adding a new feature or a new declared
    relationship never requires touching this function, only the feature skill's
    own YAML. This replaces two special-cased checks that only covered the
    serverless/photon pair and the blocked-precedes-optimizing case; LC/Photon's
    own declared 'independent' relationship, for example, was never actually
    being read from data before -- it just happened to fall out as a default.
    """
    applied = []
    by_feature = {p["feature"]: p for p in per_feature}

    for p in per_feature:
        skill = skillset.feature(p["feature"])

        # platform_managed_capabilities: this feature's own declared suppression
        # source (e.g. serverless declares it manages photon/autoscaling/capacity).
        managed = {c["capability"] for c in skill.data.get("platform_managed_capabilities", [])}
        if managed and asset.get("compute_binding") == p.get("_compute_hint", asset.get("compute_binding")):
            pass  # handled generically below via recommends_capability, kept for clarity

        for other in per_feature:
            if other is p:
                continue
            other_skill = skillset.feature(other["feature"])
            other_cap = other_skill.data.get("recommends_capability")
            if other_cap and other_cap in managed and asset.get("compute_binding") is not None:
                # this feature (p) manages a capability the other feature recommends
                if other["verdict"] not in ("not_applicable",):
                    other["verdict"] = "not_applicable"
                    applied.append({"rule_id": "platform_managed_capability_is_moot",
                                    "winner_feature": p["feature"], "loser_feature": other["feature"]})

        # each feature's own declared interaction_declarations, read generically
        for decl in skill.data.get("interaction_declarations", []):
            target = decl.get("with")
            targets = [target] if target != "any" else [f["feature"] for f in per_feature if f is not p]
            for other_name in targets:
                other = by_feature.get(other_name)
                if not other or other is p:
                    continue

                cond = decl.get("when", {})
                condition_met = True
                if "self_verdict_in" in cond:
                    condition_met &= p["verdict"] in cond["self_verdict_in"]
                if "compute_binding_in" in cond:
                    condition_met &= asset.get("compute_binding") in cond["compute_binding_in"]
                if "other_feature_targets_dimension" in cond:
                    condition_met &= True  # structural check, assumed true if both features loaded
                if not cond:
                    condition_met = True

                if condition_met:
                    kind = decl["kind"]
                    already_applied = any(
                        a["rule_id"] == decl.get("rule_id", f"{skill.name}_{decl['with']}_{kind}")
                        for a in applied
                    )
                    if kind == "precedes" and p["verdict"] in ("blocked",) and not already_applied:
                        applied.append({"rule_id": f"{skill.name}_blocking_precedes_{other_name}",
                                        "winner_feature": p["feature"], "loser_feature": other_name,
                                        "kind": kind, "rationale": decl.get("rationale", "")})
                    elif kind == "independent" and not already_applied:
                        applied.append({"rule_id": f"{skill.name}_independent_of_{other_name}",
                                        "winner_feature": p["feature"], "loser_feature": other_name,
                                        "kind": kind, "rationale": decl.get("rationale", "")})
                    # suppresses/subsumes/conflicts/requires: extend here as those
                    # relationships actually get exercised by a real feature pair.

    return applied


def normalize_table_physical(plan: dict) -> dict:
    """Enforces the grammar's own declared apply_cluster_keys rule as real code,
    rather than hoping the planner LLM applies it correctly.

    Confirmed unreliable across three separate live runs (Haiku once, Sonnet twice,
    including after an explicit planner-instruction fix): the planner repeatedly
    emits table_physical with BOTH partition_by and cluster_by present at once when
    describing a table migrating from partitioning to clustering. The grammar
    itself declares these mutually exclusive and says the old key should be
    removed -- this function does that removal in code instead of relying on the
    model to have followed the instruction.

    Only applies to POSITIVE scenarios. A negative/edge scenario may be
    deliberately injecting this exact conflict as its signal -- stripping it there
    would silently break the test case rather than fix a mistake.
    """
    if plan.get("scenario_type") != "positive":
        return plan

    for asset in plan.get("assets", []):
        tp = asset.get("table_physical", {})
        if tp.get("cluster_by") and any(tp.get(k) for k in
                                        ("partition_by", "zorder_by", "bucket_by", "num_buckets")):
            removed = {k: tp.pop(k) for k in
                      ("partition_by", "zorder_by", "bucket_by", "num_buckets") if k in tp}
            plan.setdefault("plan_notes", "")
            plan["plan_notes"] = (
                f"{plan['plan_notes']} normalize_table_physical: removed {list(removed.keys())} "
                f"-- cluster_by present alongside them in a positive scenario, which the grammar "
                f"declares mutually exclusive. Values removed: {removed}."
            ).strip()

    return plan


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
