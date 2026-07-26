"""
Synthetic Bundle Studio -- Databricks App (Streamlit)

Deliberately calls the pipeline stage-by-stage (rather than pipeline.run() as one
black box) so the UI can show real progress at real boundaries -- intent, plan,
scope check, oracle, materialize, validate -- instead of a generic spinner.
"""
import base64
import json
import random
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

import streamlit as st

REPO_ROOT = "/Workspace/Users/suman.khalia@ascendion.com/SDAG"
sys.path.insert(0, REPO_ROOT)

from dpstudio.engine.skills import SkillSet
from dpstudio.engine.llm import AnthropicLLM
from dpstudio.engine import intent_router, planner, dna_check, oracle, materializer, validator

# --------------------------------------------------------------------------- config
st.set_page_config(page_title="Synthetic Bundle Studio", page_icon="◆", layout="wide")

SKILLS_ROOT = f"{REPO_ROOT}/dpstudio/skills"
OUT_ROOT = f"{REPO_ROOT}/materialized"

MODE_LABELS = {
    "client_default": "Grounded",
    "general": "Open Exploration",
}
MODE_HELP = {
    "client_default": "Shaped like HP's real production jobs -- typical structure, "
                       "trigger type, and dependency patterns, checked against "
                       "reviewed data.",
    "general": "Any structurally valid pipeline, unconstrained by what's typical "
               "here. Useful for exploring beyond observed production shape.",
}

# Whimsical-but-honest status text per real stage. Kept boring on purpose where the
# content is a compatibility/defect topic -- no drama, just clarity, per the same
# principle used elsewhere in this project for serious subject matter.
STAGE_MUSINGS = {
    "intent":     ["Reading the request", "Working out what's being asked", "Parsing intent"],
    "plan":       ["Composing the plan", "Drafting the bundle structure", "Sketching the code graph",
                   "Weighing which signals apply"],
    "dna":        ["Checking against HP's typical patterns", "Comparing to approved scope"],
    "oracle":     ["Working out the expected verdict", "Matching signals against the plan",
                   "Cross-checking feature interactions"],
    "materialize":["Writing the notebooks", "Building the library", "Assembling databricks.yml"],
    "validate":   ["Running the validation gates", "Checking plan integrity",
                   "Confirming every artifact is stamped"],
}


def musing(stage: str) -> str:
    return random.choice(STAGE_MUSINGS[stage])


# --------------------------------------------------------------------------- style
st.markdown("""
<style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 2.5rem; max-width: 1100px;}
    div[data-testid="stStatusWidget"] {display: none;}

    .sbs-title {font-size: 1.9rem; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 0.1rem;}
    .sbs-subtitle {color: #6b7280; font-size: 0.95rem; margin-bottom: 2rem;}

    .mode-card {
        border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px;
        cursor: pointer; transition: border-color 0.15s ease;
    }
    .mode-card.selected {border-color: #4f46e5; background: #f5f5ff;}
    .mode-card h4 {margin: 0 0 4px 0; font-size: 0.95rem;}
    .mode-card p {margin: 0; font-size: 0.82rem; color: #6b7280; line-height: 1.4;}

    .badge {display: inline-block; font-size: 0.72rem; padding: 2px 8px; border-radius: 20px;
            font-weight: 500; margin-left: 6px;}
    .badge-reviewed {background: #dcfce7; color: #15803d;}
    .badge-pending {background: #fef3c7; color: #b45309;}

    .verdict-eligible, .verdict-recommended {color: #15803d; font-weight: 600;}
    .verdict-blocked, .verdict-not_recommended {color: #dc2626; font-weight: 600;}
    .verdict-review_before_serverless, .verdict-review_before_clustering,
    .verdict-review_before_photon, .verdict-needs_review {color: #b45309; font-weight: 600;}
    .verdict-not_applicable, .verdict-neutral {color: #9ca3af; font-weight: 500;}

    .review-banner {
        background: #fef3c7; border: 1px solid #fbbf24; border-radius: 10px;
        padding: 14px 18px; margin: 12px 0;
    }
    .clean-banner {
        background: #f0fdf4; border: 1px solid #86efac; border-radius: 10px;
        padding: 14px 18px; margin: 12px 0;
    }
    .path-chip {
        font-family: monospace; font-size: 0.8rem; background: #f3f4f6;
        padding: 6px 10px; border-radius: 6px; display: inline-block;
    }
    .stage-line {color: #6b7280; font-size: 0.88rem;}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- setup
@st.cache_resource
def get_skillset():
    return SkillSet(SKILLS_ROOT)


@st.cache_resource
def get_llm():
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    secret = w.secrets.get_secret(scope="dpstudio", key="anthropic_key")
    api_key = base64.b64decode(secret.value).decode()
    return AnthropicLLM(api_key=api_key, model="claude-sonnet-4-5")


ss = get_skillset()
llm = get_llm()

if "history" not in st.session_state:
    st.session_state.history = []
if "current" not in st.session_state:
    st.session_state.current = None

# --------------------------------------------------------------------------- header
st.markdown('<div class="sbs-title">Synthetic Bundle Studio</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sbs-subtitle">Generate synthetic Databricks asset bundles. '
    'Plans, validates, and materializes them for you.</div>',
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- 1. prompt
prompt = st.text_area(
    "Describe the synthetic pipeline you want to generate",
    placeholder="Generate a synthetic data pipeline for serverless.",
    height=90,
    max_chars=2000,
)

# --------------------------------------------------------------------------- 2. mode
st.markdown("**Generation mode**")
mode_cols = st.columns(2)
if "generation_mode" not in st.session_state:
    st.session_state.generation_mode = "client_default"

for col, key in zip(mode_cols, ["client_default", "general"]):
    with col:
        selected = st.session_state.generation_mode == key
        if st.button(
            f"{MODE_LABELS[key]}" + ("  ●" if selected else ""),
            key=f"mode_{key}", use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            st.session_state.generation_mode = key
            st.rerun()
        st.caption(MODE_HELP[key])

if st.session_state.generation_mode == "general":
    st.warning("Bundles generated in Open Exploration mode are not guaranteed to "
               "reflect HP's real production patterns.", icon="⚠")

# --------------------------------------------------------------------------- 3. features
st.markdown("**Feature selection**")
feature_scope = {f["feature"]: f["status"] for f in ss.client_dna.data.get("feature_scope", [])}
registered = ss.registered_features()

feat_cols = st.columns(len(registered))
selected_features = []
for col, feat in zip(feat_cols, registered):
    with col:
        checked = st.checkbox(feat.replace("_", " ").title(), value=True, key=f"feat_{feat}")
        if checked:
            selected_features.append(feat)
        status = feature_scope.get(feat, "not_yet_reviewed")
        badge_class = "badge-reviewed" if status == "reviewed" else "badge-pending"
        badge_text = "reviewed for HP" if status == "reviewed" else "not yet reviewed"
        st.markdown(f'<span class="badge {badge_class}">{badge_text}</span>', unsafe_allow_html=True)

# --------------------------------------------------------------------------- 4. advanced
with st.expander("Advanced (optional) -- override scenario type"):
    scenario_override = st.selectbox(
        "Scenario type", ["auto", "positive", "negative", "edge", "distractor", "mixed"], index=0)

# --------------------------------------------------------------------------- 5. generate
generate = st.button("Generate Bundle", type="primary", use_container_width=False)

st.divider()

# --------------------------------------------------------------------------- run pipeline
def run_pipeline_with_progress(prompt: str, features: list[str], mode: str, scenario_override: str):
    status_box = st.status(musing("intent"), expanded=True)
    t0 = time.time()

    with status_box:
        st.write(f"_{musing('intent')}..._")
        router_output = intent_router.classify(prompt, ss, llm)
        if features:
            router_output["target_features"] = features
        if router_output.get("needs_clarification"):
            status_box.update(label="Needs clarification", state="error")
            return {"status": "needs_clarification", "router_output": router_output}
        st.write(f"→ targeting: {', '.join(router_output['target_features'])}")

        status_box.update(label=musing("plan"))
        st.write(f"_{musing('plan')}..._")
        if scenario_override != "auto":
            router_output["scenario_type_hint"] = scenario_override
        plan = planner.generate_plan(prompt, router_output, ss, llm)
        plan["generation_mode"] = mode
        if plan["plan_status"] == "invalid":
            status_box.update(label="Plan invalid", state="error")
            return {"status": "invalid", "plan": plan, "router_output": router_output}
        st.write(f"→ plan drafted: {len(plan['code_graph']['nodes'])} nodes, "
                 f"depth {plan['knobs'].get('reference_depth', 0)}")

        if mode == "client_default":
            status_box.update(label=musing("dna"))
            st.write(f"_{musing('dna')}..._")
            plan = dna_check.enforce(plan, ss)
            if plan["plan_status"] == "needs_review":
                status_box.update(label="Needs review -- scope check flagged an issue", state="error")
                return {"status": "needs_review", "plan": plan, "router_output": router_output,
                        "elapsed": time.time() - t0}
            st.write("→ within approved scope")

        status_box.update(label=musing("oracle"))
        st.write(f"_{musing('oracle')}..._")
        plan["expected"] = oracle.run(plan, ss)
        st.write(f"→ verdict: {plan['expected']['verdict']}")

        status_box.update(label=musing("materialize"))
        st.write(f"_{musing('materialize')}..._")
        out_dir = Path(OUT_ROOT) / plan["plan_id"]
        plan = materializer.materialize(plan, out_dir)
        st.write(f"→ {len(plan['artifacts'])} artifacts written")

        status_box.update(label=musing("validate"))
        st.write(f"_{musing('validate')}..._")
        check_results = validator.run(plan, out_dir)
        summary = validator.summarize(check_results)
        plan["plan_status"] = "materialized" if summary["all_passed"] else "needs_review"

        elapsed = time.time() - t0
        status_box.update(label=f"Done in {elapsed:.1f}s", state="complete", expanded=False)

    return {"status": "ok", "plan": plan, "validation": summary,
            "out_dir": str(out_dir), "router_output": router_output, "elapsed": elapsed}


if generate:
    if not prompt.strip():
        st.error("Describe the pipeline you want first.")
    else:
        result = run_pipeline_with_progress(
            prompt, selected_features, st.session_state.generation_mode, scenario_override)
        st.session_state.current = result
        st.session_state.history.insert(0, {
            "prompt": prompt, "mode": st.session_state.generation_mode,
            "status": result["status"],
            "plan_id": result.get("plan", {}).get("plan_id", "—"),
        })

# --------------------------------------------------------------------------- results
result = st.session_state.current
if result:
    st.subheader("Results")

    plan = result.get("plan", {})
    meta_cols = st.columns(4)
    meta_cols[0].metric("Generation mode", MODE_LABELS.get(plan.get("generation_mode", "—"), "—"))
    meta_cols[1].metric("Status", result["status"])
    if "elapsed" in result:
        meta_cols[2].metric("Time", f"{result['elapsed']:.1f}s")
    meta_cols[3].metric("Plan ID", plan.get("plan_id", "—")[:16] if plan.get("plan_id") else "—")

    # -------- needs_clarification --------
    if result["status"] == "needs_clarification":
        st.markdown(
            f'<div class="review-banner"><b>Needs clarification</b><br>'
            f'{result["router_output"].get("clarification_reason", "The request is ambiguous.")}'
            f'</div>', unsafe_allow_html=True)

    # -------- needs_review (dna violation) --------
    elif result["status"] == "needs_review" and plan.get("dna_violations"):
        st.markdown('<div class="review-banner"><b>⚠ Plan status: NEEDS REVIEW</b></div>',
                    unsafe_allow_html=True)
        for v in plan["dna_violations"]:
            st.write(f"**{v['dimension']}** -- {v['detail']}")
            st.caption(f"resolved value: `{v['resolved_value']}`  ·  approved bound: `{v['bound']}`")
        c1, c2 = st.columns(2)
        if c1.button("Approve anyway", type="secondary"):
            st.success("Approved as an exception. (Logged for review -- wire to an audit table in production.)")
        if c2.button("Reject", type="secondary"):
            st.info("Rejected. Adjust the prompt or switch to Open Exploration mode.")

    # -------- ok or validation-failed --------
    elif result["status"] in ("ok", "needs_review"):
        exp = plan.get("expected")
        if exp:
            st.markdown("**Verdict**")
            for pf in exp["per_feature"]:
                st.markdown(f"- {pf['feature'].replace('_',' ').title()}: "
                            f'<span class="verdict-{pf["verdict"]}">{pf["verdict"]}</span>',
                            unsafe_allow_html=True)
            if exp["interactions_applied"]:
                st.caption("Interactions: " + ", ".join(i["rule_id"] for i in exp["interactions_applied"]))
            if exp["matched_signals"]:
                with st.expander(f"Matched signals ({len(exp['matched_signals'])})"):
                    for m in exp["matched_signals"]:
                        st.write(f"`{m['signal_id']}` ({m['feature']}) at `{m['node_id']}` "
                                 f"— {m.get('status', 'verified')}")

        val = result.get("validation")
        if val:
            if val["all_passed"]:
                st.markdown('<div class="clean-banner">✓ All validation checks passed '
                            f'({val["passed"]}/{val["total"]})</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="review-banner"><b>Validation failed '
                            f'({val["failed"]}/{val["total"]} checks)</b></div>',
                            unsafe_allow_html=True)
                for f in val["failures"]:
                    st.write(f"**{f['check']}**")
                    st.caption(f["detail"] or "no detail recorded")

        # -------- paths + download --------
        if result.get("out_dir"):
            st.markdown("**Output location**")
            st.markdown(f'<span class="path-chip">{result["out_dir"]}</span>', unsafe_allow_html=True)

            out_path = Path(result["out_dir"])
            if out_path.exists():
                buf = BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    for f in out_path.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(out_path))
                st.download_button("Download bundle", buf.getvalue(),
                                   file_name=f"{plan['plan_id']}.zip", mime="application/zip")

                with st.expander("Browse materialized files"):
                    files = sorted(f.relative_to(out_path) for f in out_path.rglob("*") if f.is_file())
                    chosen = st.selectbox("File", files, format_func=str)
                    if chosen:
                        content = (out_path / chosen).read_text(errors="ignore")
                        lang = "yaml" if str(chosen).endswith((".yml", ".yaml")) else \
                               "sql" if str(chosen).endswith(".sql") else "python"
                        st.code(content, language=lang)

    # -------- invalid --------
    elif result["status"] == "invalid":
        st.markdown(f'<div class="review-banner"><b>Plan invalid</b><br>'
                    f'{plan.get("plan_notes", "The planner could not produce a valid plan.")}'
                    f'</div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------- session history
if st.session_state.history:
    with st.sidebar:
        st.markdown("**Session history**")
        for h in st.session_state.history[:15]:
            st.caption(f"[{h['status']}] {h['plan_id'][:14]} — {h['prompt'][:40]}")
