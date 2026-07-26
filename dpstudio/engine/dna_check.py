"""
dpstudio/engine/dna_check.py

Code-level enforcement of client-dna's declared defaults/bounds. This exists because
the planner LLM proved unreliable at connecting client-dna's dimension entries to the
grammar knobs it resolves -- confirmed by two consecutive live runs where the model
resolved library_type to whl_in_environment despite client-dna's bounds explicitly
scoping that dimension to [whl_workspace_file]. Prompt instructions alone are not
sufficient; this is a real code check that runs after the planner and before the
oracle, so a scope violation is caught deterministically rather than hoped for.
"""
from __future__ import annotations

from .skills import SkillSet


def _get_knob_value(plan: dict, dimension: str):
    """A dimension may live in plan.knobs (most cases) or on the asset itself
    (e.g. job_trigger, which the grammar models as an asset field, not a knob)."""
    if dimension in plan.get("knobs", {}):
        return plan["knobs"][dimension], "knobs"
    for asset in plan.get("assets", []):
        if dimension in asset:
            return asset[dimension], "assets[0]"
    return None, None


def enforce(plan: dict, skillset: SkillSet) -> dict:
    """Checks plan's resolved values against client-dna's dimension_profiles.
    Mutates plan in place: sets plan_status to needs_review and appends a
    structured violation record if any bound is exceeded. Returns the plan.
    """
    dna = skillset.client_dna.data
    violations = []

    for entry in dna.get("dimension_profiles", []):
        dim = entry["dimension"]
        bounds = entry.get("bounds")
        if not bounds:
            continue  # a default with no bounds has nothing to enforce

        value, location = _get_knob_value(plan, dim)
        if value is None:
            continue  # dimension not present in this plan at all

        violation = None

        if "in_scope" in bounds:
            values = value if isinstance(value, list) else [value]
            out_of_scope = [v for v in values if v not in bounds["in_scope"]]
            if out_of_scope:
                violation = {
                    "dimension": dim, "location": location,
                    "resolved_value": value, "bound": bounds,
                    "detail": f"{out_of_scope} not in approved scope {bounds['in_scope']}",
                }

        if "max" in bounds and isinstance(value, (int, float)) and value > bounds["max"]:
            violation = {
                "dimension": dim, "location": location,
                "resolved_value": value, "bound": bounds,
                "detail": f"{value} exceeds max {bounds['max']} by {value - bounds['max']}",
            }

        if "min" in bounds and isinstance(value, (int, float)) and value < bounds["min"]:
            violation = {
                "dimension": dim, "location": location,
                "resolved_value": value, "bound": bounds,
                "detail": f"{value} below min {bounds['min']}",
            }

        if violation:
            violations.append(violation)

    if violations:
        plan["plan_status"] = "needs_review"
        existing_notes = plan.get("plan_notes", "")
        note = "client-dna scope violations: " + "; ".join(
            f"{v['dimension']} ({v['detail']})" for v in violations
        )
        plan["plan_notes"] = f"{existing_notes} {note}".strip()
        plan["dna_violations"] = violations

    return plan
