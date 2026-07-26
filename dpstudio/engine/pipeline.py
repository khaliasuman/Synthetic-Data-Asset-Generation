"""
dpstudio/engine/pipeline.py

Wires the five stages together. This is the only module a UI or notebook
entrypoint should call directly.
"""
from __future__ import annotations

from pathlib import Path

from . import intent_router, planner, oracle, materializer, validator, dna_check
from .skills import SkillSet


def run(prompt: str, skills_root: str | Path, out_root: str | Path, llm,
        generation_mode: str = "client_default") -> dict:
    """
    generation_mode: "client_default" loads client-dna (Typical for HP);
                     "general" skips it entirely (General / Any Pattern).
    """
    skillset = SkillSet(skills_root)

    router_output = intent_router.classify(prompt, skillset, llm)
    if router_output.get("needs_clarification"):
        return {"status": "needs_clarification", "router_output": router_output}

    plan = planner.generate_plan(prompt, router_output, skillset, llm)
    plan["generation_mode"] = generation_mode
    if plan["plan_status"] == "invalid":
        return {"status": "invalid", "plan": plan}

    # Code-level client-dna enforcement -- do NOT rely on the planner LLM to have
    # respected client-dna's bounds itself. Confirmed unreliable: two consecutive
    # live runs resolved library_type outside its declared scope despite explicit
    # prompt instructions. This check is deterministic and cannot be skipped.
    #
    # Materialize even on a violation, rather than halting: a human reviewing a
    # needs_review plan needs to actually SEE the generated notebooks/YAML to make
    # an informed approve/reject decision. Approving something you can't inspect
    # isn't a real review. The violation banner still shows first regardless.
    dna_flagged = False
    if generation_mode == "client_default":
        plan = dna_check.enforce(plan, skillset)
        if plan["plan_status"] == "needs_review":
            dna_flagged = True

    plan["expected"] = oracle.run(plan, skillset)

    out_dir = Path(out_root) / plan["plan_id"]
    plan = materializer.materialize(plan, out_dir)

    check_results = validator.run(plan, out_dir)
    summary = validator.summarize(check_results)
    # A dna violation always wins the status label, even if validation also passed --
    # the point is the reviewer sees "needs_review" and why, with the bundle attached.
    plan["plan_status"] = "needs_review" if (dna_flagged or not summary["all_passed"]) else "materialized"

    # Persist the full plan JSON alongside the materialized bundle. Without this,
    # the plan (knobs, code_graph, expected verdict, dna_violations) only ever
    # existed as an in-memory dict and was never actually inspectable after the
    # fact -- exactly the "where can I see the plan" gap this closes.
    import json
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))

    return {
        "status": "needs_review" if dna_flagged else "ok",
        "plan": plan,
        "validation": summary,
        "out_dir": str(out_dir),
        "router_output": router_output,
    }
