"""
dpstudio/engine/planner.py

Loads the grammar, client-dna, and relevant feature skills as full context and
asks the LLM to produce a plan. The planner never computes plan.expected (the
oracle does that afterward, in pure code) and never writes literal notebook or
YAML file contents (the materializer does that) -- it only produces structure:
knobs, code_graph, per-node short code snippets, and distractor placements.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .skills import SkillSet

INSTRUCTION_BLOCK = """You generate synthetic Databricks asset bundle PLANS. You do not \
write final notebook files, YAML, or wheels -- a separate deterministic materializer does \
that from your plan. You do not compute verdicts -- a separate deterministic oracle does \
that from your plan, after you produce it.

You are given, in order:
  1. The grammar skill (asset-bundle-generation) -- dimensions, vocabulary, knobs,
     plan_schema, generation_rules.
  2. The client-dna skill -- this client's approved default values and hard bounds for
     specific dimensions. Use its `defaults` to resolve any knob the request and router
     hints leave unstated. Never exceed a `bounds` entry silently -- if a resolved value
     would exceed a bound, set plan_status to "needs_review" and record which bound and
     by how much in plan_notes.
  3. One or more feature skills -- signals, eligibility, apply_rules, placement_eligibility,
     distractor_templates. Only use signal ids, verdicts, and vocabulary values that
     literally appear in the loaded feature skill YAML. Never invent one.

You also receive the original user request and structured routing hints from an upstream
classifier (target_features, complexity_hint, scenario_type_hint, explicit_signals).

YOUR JOB, IN ORDER:
1. Resolve every knob in the grammar's `knobs` block: explicit request value, else a
   client-dna default (if that dimension has one), else the grammar's own knob default.
   Record every resolved knob in plan.knobs, including defaulted ones.
2. Compose code_graph per generation_rules: exactly one entry node, no cycles, every
   node reachable. For each node, write a SHORT literal code snippet (1-4 lines) in
   node_code[node_id].executable -- real runnable-looking lines, not prose descriptions.
   This is the only literal code you produce; the materializer wraps it into full files.

   CRITICAL: never write a reference-mechanism line yourself inside node_code
   (e.g. "%run ./other_node", "dbutils.notebook.run(...)", an import of another
   NODE you already declared as an edge). Those come ONLY from the code_graph.edges
   you declare via reference_mechanism, and the materializer renders them. If you
   also write the same %run or dbutils.notebook.run line as a literal executable
   line, it gets duplicated -- once correctly as a real notebook magic command, once
   incorrectly as dead/invalid code sitting inside the executable body. node_code
   should contain ONLY the node's own business logic (reading data, transforming,
   writing, calling a genuinely external/already-imported function) -- never a line
   that duplicates an edge you already declared.

2c. When composing a MULTI-MODULE positive scenario (node_count >= 3), follow this
    real production shape rather than an arbitrary graph: one entry/orchestrator node
    that navigates to children via magic_run or dbutils_notebook_run edges (this is
    the ONLY place those mechanisms belong), plus one or more separate MODULE-role
    nodes (schema/helper/utility logic) reached via python_import edges from the
    entry node, not chained through the orchestrator. Do not make python_import
    nodes children of a %run chain; import them directly where their logic is used.
    This mirrors real HP production jobs: an orchestrator notebook plus distinct
    internal utility modules imported as code, not run as sub-notebooks.

    IMPORTANT -- mechanism diversity: when reference_mechanism_mix includes both
    magic_run AND dbutils_notebook_run and reference_depth >= 2, use
    dbutils_notebook_run for at least one orchestrator->child edge instead of
    defaulting to magic_run for all of them. A mechanism listed as available but
    never actually chosen is not being tested. Vary which one you pick across a
    session rather than always reaching for magic_run out of habit.

2d. IMPORTANT -- knobs.library_type must be BACKED BY A REAL NODE, not just set as
    a value with nothing behind it. If knobs.library_type is "whl_workspace_file"
    and knobs.library_count >= 1, the code_graph MUST include at least one node
    with role="library_src" whose node_code.executable contains real library
    function code, referenced via a python_import edge from wherever it's used.
    Setting library_type in knobs without creating the corresponding library_src
    node produces a plan that claims a library exists but never materializes one --
    this is a real, previously-confirmed defect. If library_type is "none", do not
    create a library_src node at all.

2e. Parameterization and trigger, per client-dna. When client-dna sets
    param_passing default widgets (and the request doesn't override), set
    knobs.param_passing to "widgets" and knobs.param_names to a SHORT list (2-5)
    of realistic parameter names for this scenario (e.g. catalog, schema,
    receive_date, secret_name) -- the materializer renders the client's canonical
    widgets.removeAll()/text/get preamble from these; do NOT write widget lines in
    node_code yourself. When client-dna sets job_trigger default schedule, set
    knobs.job_trigger to "schedule" -- the materializer renders the cron+timezone
    block; do NOT invent schedule YAML in node_code or notes.

2f. CRITICAL when liquid_clustering is a target feature: setting cluster_by or
    liquid_clustering_enabled in table_physical WITHOUT the numeric fields that
    actually justify a benefit verdict produces an intent with no evidence behind
    it -- confirmed to happen specifically when multiple features compete for
    attention in one complex request. If liquid_clustering is targeted, populate
    AT LEAST TWO of: partition_count (with avg_partition_row_count), small_file_count
    (with avg_file_size_mb), skew_factor, or has_filtered_column_with_cardinality --
    with real, internally consistent numbers (small_file_count * avg_file_size_mb
    should roughly match a plausible table size; partition_count * avg_partition_row_count
    should equal a plausible total_row_count). Declaring liquid_clustering_enabled
    without any of these fields will resolve to not_applicable, which is not what a
    request naming this feature is asking for.

2g. CRITICAL graph connectivity when a library is created alongside other modules:
    every node you create MUST have at least one edge connecting it to the graph
    from the entry node's reachable set -- confirmed to fail specifically when a
    library_src node is created in addition to a regular module node under
    multi-feature complexity, producing two similarly-named nodes (e.g.
    pipeline_utils and pipeline_utils_lib) where only one gets wired in. Before
    finalizing code_graph, verify every node_id appears as a `to_node` in at least
    one edge (except the entry node itself). Do not create a node "for completeness"
    without also creating the edge that reaches it.

2h. CRITICAL every declared widget param must actually be used. Confirmed as a real
    defect: knobs.param_names sometimes lists a parameter (e.g. lookback_days) that
    is declared via dbutils.widgets.text/get in the entry notebook but never
    referenced again anywhere -- a widget nobody reads is a decoration, not a real
    parameter, and it's exactly the kind of thing a reviewer notices immediately.
    Every name in knobs.param_names MUST appear in at least one node's
    node_code.executable line, used meaningfully (a filter predicate, a path
    component, a conditional, a log line) -- not just re-assigned and dropped. If
    you cannot think of a real use for a parameter, do not declare it in the first
    place; a shorter, fully-used param list is better than a longer decorative one.

2i. Realism floor for node_code, since production code this thin is a visible
    synthetic tell to any real reviewer. Within the same 1-4 line budget per node,
    prefer lines that carry real signal over lines that are purely narrative:
    - The entry/orchestrator node should wrap its core action in a minimal
      try/except, calling dbutils.notebook.exit("FAILED: <reason>") or raising on
      failure -- not silently succeeding regardless of outcome.
    - At least one data-loading node should include a basic null/quality check
      (e.g. .filter(col(...).isNotNull()) or an explicit row-count assertion)
      rather than reading and writing with no validation at all.
    - Prefer a real log line (print or a logger call carrying an actual variable,
      e.g. row count or a computed value) over a static narrative string with
      nothing computed in it.
    This is a floor, not a rewrite -- still short, still 1-4 lines per node, just
    lines that would survive a senior engineer's glance rather than reading as
    obviously synthetic filler.

2b. table_physical fields are MUTUALLY EXCLUSIVE per the feature skill's own rules --
    check each feature skill's eligibility_signals and apply_rules for constraint pairs
    (e.g. a table cannot have both partition_by AND cluster_by set at once; ZORDER and
    cluster_by are likewise exclusive). When describing a table that is migrating FROM
    one layout TO another (e.g. "was partitioned, now benefits from clustering"), the
    resolved table_physical must reflect ONLY the target state -- do not carry the prior
    layout's key forward into the same object alongside the new one. If you need to show
    the "before" state for narrative context, put it in plan_notes as prose, never as a
    literal field alongside its mutually-exclusive replacement.
3. If scenario_type is negative/edge: pick a signal from the loaded feature skill's
   eligibility_signals, check placement_eligibility for it, and inject it into the
   node_code at that placement (i.e. write the actual line of code that trips that
   signal's match pattern into that node's executable list).
4. If distractor_count > 0: place near-misses ONLY from distractor_templates verbatim
   text, onto their declared surface, in distractors[].
5. Do NOT fill plan.expected -- leave it absent; the oracle fills it after you.
6. If genuinely ambiguous even with router hints, set plan_status: "invalid" and explain
   in plan_notes rather than guessing.

OUTPUT FORMAT: a single JSON object with these top-level keys, no markdown fences, no
prose outside the JSON:
{
  "plan_intent": "<restate the request in one sentence>",
  "target_features": [...],
  "scenario_type": "positive|negative|edge|distractor|mixed",
  "knobs": { ... every resolved knob ... },
  "plan_status": "planned|needs_review|invalid",
  "plan_notes": "<string, empty if nothing to flag>",
  "assets": [ { "compute_binding":..., "language":..., "format":..., "workload_type":...,
                "table_physical": {...}, "libraries": [...] } ],
  "code_graph": { "nodes": [{"node_id":..., "role": "entry|child|module|library_src"}],
                  "edges": [{"from_node":..., "to_node":..., "reference_mechanism":...}] },
  "node_code": { "<node_id>": {"executable": ["<line>", ...]} },
  "distractors": [ {"distractor_id":..., "imitates_signal":..., "surface":..., "node_id":...} ]
}
"""


def _build_system_prompt(skillset: SkillSet, target_features: list[str]) -> list[dict]:
    """Returns cache-marked content blocks rather than one joined string. The
    static block (instructions + grammar + client-dna) is IDENTICAL on every
    single planner call regardless of target_features -- confirmed to be ~14K
    of the ~25K tokens per call. Marking it cache_control lets Anthropic bill it
    at ~10% of normal input cost on every call within the ~5-minute cache window
    after the first, instead of full price every time. Each feature skill gets
    its own cache breakpoint too, since the same feature recurs across calls in
    a session. Anthropic allows up to 4 cache breakpoints per request -- this
    uses 1 (static) + up to 3 (one per registered feature), exactly at the limit
    if all three features are ever targeted together.
    """
    static_text = "\n\n".join([INSTRUCTION_BLOCK, skillset.grammar.text, skillset.client_dna.text])
    blocks = [{"type": "text", "text": static_text, "cache_control": {"type": "ephemeral"}}]
    for f in target_features:
        blocks.append({"type": "text", "text": skillset.feature(f).text,
                       "cache_control": {"type": "ephemeral"}})
    return blocks


def generate_plan(prompt: str, router_output: dict, skillset: SkillSet, llm) -> dict:
    target_features = router_output.get("target_features") or []
    if not target_features:
        raise ValueError("Router produced no target_features; resolve via clarification "
                          "before calling the planner.")

    system = _build_system_prompt(skillset, target_features)
    user = (
        f"Original request: \"{prompt}\"\n\n"
        f"Router output:\n{json.dumps(router_output, indent=2)}\n\n"
        "Generate the plan."
    )
    raw = llm.complete_json(system=system, user=user, max_tokens=4000)

    # Stamp the fields the LLM must never fabricate itself.
    plan = dict(raw)
    plan["plan_id"] = "plan_" + uuid.uuid4().hex[:12]
    plan["plan_version"] = 1
    plan["plan_generated_at"] = datetime.now(timezone.utc).isoformat()
    plan.setdefault("plan_status", "planned")
    plan.setdefault("plan_notes", "")
    plan["expected"] = None  # filled by oracle.run(), never by the planner
    # oracle.py reads plan['_node_code'] as {node_id: {"executable": [...]}}
    plan["_node_code"] = plan.get("node_code", {})
    return plan
