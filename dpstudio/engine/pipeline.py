"""
dpstudio/engine/pipeline.py

Wires the five stages together. This is the only module a UI or notebook
entrypoint should call directly.
"""
from __future__ import annotations

from pathlib import Path

from . import intent_router, planner, oracle, materializer, validator
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

    plan["expected"] = oracle.run(plan, skillset)

    out_dir = Path(out_root) / plan["plan_id"]
    plan = materializer.materialize(plan, out_dir)

    check_results = validator.run(plan, out_dir)
    summary = validator.summarize(check_results)
    plan["plan_status"] = "materialized" if summary["all_passed"] else "needs_review"

    return {
        "status": "ok",
        "plan": plan,
        "validation": summary,
        "out_dir": str(out_dir),
        "router_output": router_output,
    }
