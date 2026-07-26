"""
dpstudio/engine/validator.py

Gate 2 (plan integrity) + Gate 3 (materialization fidelity), per the six-gate
design. Pure code, no LLM, no re-running the oracle. Returns a list of
(check_id, passed, detail) so failures are attributable to a specific gate.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path


def _graph_ok(plan: dict) -> tuple[bool, str]:
    nodes = {n["node_id"] for n in plan["code_graph"]["nodes"]}
    entries = [n for n in plan["code_graph"]["nodes"] if n.get("role") == "entry"]
    adj: dict[str, list[str]] = {}
    for e in plan["code_graph"]["edges"]:
        adj.setdefault(e["from_node"], []).append(e["to_node"])
    if len(entries) != 1:
        return False, f"expected exactly 1 entry node, found {len(entries)}"
    seen, stack = set(), [entries[0]["node_id"]]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack += adj.get(x, [])
    return seen == nodes, f"reachable {len(seen)}/{len(nodes)}"


def run(plan: dict, out_dir: str | Path) -> list[tuple[str, bool, str]]:
    out_dir = Path(out_dir)
    results = []

    def check(cid, ok, detail=""):
        results.append((cid, ok, detail))

    # --- Gate 2: plan integrity ---
    required = ["plan_id", "plan_version", "plan_intent", "plan_generated_at",
                "plan_status", "target_features", "knobs", "assets", "code_graph",
                "expected", "artifacts"]
    check("plan_schema_complete", all(k in plan for k in required),
          str([k for k in required if k not in plan]))

    ok, detail = _graph_ok(plan)
    check("graph_well_formed", ok, detail)

    if plan.get("scenario_type") == "negative":
        check("negative_scenario_has_injected_signal",
              bool(plan["expected"]["matched_signals"]), "no signals matched")

    dx = plan.get("distractors", [])
    if plan.get("scenario_type") == "distractor":
        check("distractor_scenario_stays_clean", not plan["expected"]["matched_signals"],
              f"{len(plan['expected']['matched_signals'])} signals matched, expected 0")

    # distractor immunity, whatever the scenario type: a matched signal's node must
    # not be a node whose ONLY content was a planted distractor for that same signal
    dx_by_node = {d["node_id"]: d["imitates_signal"] for d in dx}
    leaked = [m for m in plan["expected"]["matched_signals"]
              if dx_by_node.get(m["node_id"]) == m["signal_id"]
              and not plan["_node_code"].get(m["node_id"], {}).get("executable")]
    check("no_distractor_leak", not leaked, str(leaked))

    check("expected_frozen", plan["expected"] is not None and
          bool(plan["expected"].get("oracle_version")))

    # --- Gate 3: materialization fidelity ---
    node_paths = {n["node_id"]: out_dir / f"src/notebooks/{n['node_id']}.py"
                  for n in plan["code_graph"]["nodes"]}
    check("every_reference_resolves",
          all(p.exists() for nid, p in node_paths.items()
              if next(n["role"] for n in plan["code_graph"]["nodes"] if n["node_id"] == nid)
              in ("entry", "child", "module")),
          str([nid for nid, p in node_paths.items() if not p.exists()]))

    wheel_artifacts = [a for a in plan["artifacts"] if a["artifact_kind"] == "wheel"]
    if wheel_artifacts:
        check("wheel_builds", all(a.get("build_status") == "success" for a in wheel_artifacts))

    stamped, missing = 0, []
    for a in plan["artifacts"]:
        p = out_dir / a["path"]
        if not p.exists():
            missing.append(a["path"])
            continue
        if a["artifact_kind"] == "wheel":
            with zipfile.ZipFile(p) as z:
                ok = any(plan["plan_id"] in z.read(n).decode(errors="ignore")
                         for n in z.namelist())
        else:
            ok = plan["plan_id"] in p.read_text()
        stamped += ok
        if not ok:
            missing.append(a["path"])
    check("artifacts_stamped", not missing, f"{stamped}/{len(plan['artifacts'])} stamped; missing {missing}")

    return results


def summarize(results: list[tuple[str, bool, str]]) -> dict:
    passed = sum(1 for _, ok, _ in results if ok)
    return {
        "total": len(results), "passed": passed, "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "failures": [{"check": c, "detail": d} for c, ok, d in results if not ok],
    }
