---
name: asset-bundle-generation
description: The authoritative grammar and generation contract for synthetic Databricks asset bundles. Use this skill for ANY activity that produces, plans, composes, materializes, or validates a generated bundle — building synthetic golden datasets, generating multi-notebook jobs, emitting wheel/library dependencies, constructing task or pipeline graphs, declaring table physical layout, planting distractors, stamping plan lineage onto artifacts, or computing the expected verdict for a generated scenario. It owns the archetype-agnostic dimension vocabulary that feature skills (serverless, liquid clustering, photon) supply values for, and the interaction rules that resolve their verdicts when more than one applies. Use it whenever the user mentions synthetic data generation, golden datasets, asset bundles, databricks.yml, generated notebooks or jobs, plan schemas, false positives, or "generate a test case for <feature>" — even if they name only the feature and not the bundle.
---

# Synthetic Asset Bundle Generation — Grammar & Contract

This file is the single authority for **how a synthetic bundle is planned, composed, and
materialized**. It is deliberately *feature-agnostic*: it knows nothing about whether a
bundle is serverless-eligible, well-clustered, or Photon-friendly. Those judgements belong
to **feature skills**, which supply values and signals for the dimensions defined here.

The split exists so that adding a fourth or tenth feature never requires editing this file.
If a new feature needs a dimension that does not exist yet, extend `dimensions` — never add
a feature-specific branch to a workflow.

Two zones, strictly separated:

1. **Machine-readable zone** — structured YAML. Code generates, materializes, and validates
   against this.
2. **Guidance zone** — prose hints for LLM reasoning only. Never code-checked. Nothing in it
   may contradict the YAML; when in doubt, the YAML wins.

Five invariants to preserve:

- **Config-agnostic composition.** Nothing here matches on a named table, job, notebook, or
  library. Everything is expressed as structural dimensions and knobs.
- **Feature skills own signals.** This file defines *where* a signal can be placed and *how*
  it is recorded; it never defines what makes a bundle good or bad.
- **The plan is the contract.** Generation emits a plan. Materialization consumes only the
  plan. Nothing is invented at materialization time.
- **The oracle is deterministic.** The expected verdict is computed from the plan by code,
  before materialization. No model grades a model.
- **Recall and precision are both measurable.** Every negative scenario has a `distractor`
  counterpart. A suite that only proves signals are found is half a suite.

---

## Machine-readable zone

```yaml
version: "2.0"

# =====================================================================
# DIMENSIONS — the archetype-agnostic grammar. Feature skills reference
# these names and supply values/patterns for them. A dimension is a
# structural property of a bundle that a rule can match on.
# =====================================================================
dimensions:
  # --- workload shape ---
  - id: workload_type
    kind: enum
  - id: language
    kind: enum
  - id: format
    kind: enum
  - id: streaming_trigger
    kind: enum
  - id: job_trigger
    kind: enum
    description: "How the job itself is initiated (schedule, file arrival, table update, continuous)."

  # --- code and dependency surface ---
  - id: api_usage
    kind: text_corpus
    description: >
      Union of EXECUTABLE code text across all reachable nodes. Comments, docstrings,
      markdown cells, string literals, and unreachable code are excluded — they belong
      to distractor_surface, not here.
  - id: udf_kind
    kind: enum
    description: "UDF flavour; support differs sharply across flavours."
  - id: library
    kind: object_list
    description: "Declared dependencies, each carrying a library_type."
  - id: runtime_config
    kind: key_map
  - id: sql_semantics
    kind: derived
  - id: network_egress
    kind: object_list
    description: "External calls: HTTP, JDBC/ODBC, cloud SDK, private endpoints."
  - id: local_filesystem
    kind: path_list
    description: "Driver-local paths (/tmp, /local_disk0), distinct from DBFS."

  # --- physical layout ---
  - id: storage_path
    kind: path_list
  - id: column_types
    kind: type_list
  - id: table_physical
    kind: key_map
    description: >
      Physical layout of a data asset: partitioning, clustering keys, Z-order,
      CDF, deletion vectors, generated/identity columns, table properties,
      file sizes and count. Primary surface for clustering/Photon features.
  - id: checkpoint_location
    kind: path_list
    description: "Streaming checkpoint paths; independently constrained from data paths."
  - id: state_management
    kind: key_map
    description: "foreachBatch, watermarks, state store backend, stateful operators."

  # --- catalog surface ---
  - id: uc_object_kind
    kind: enum_set
    description: "Non-table catalog objects the bundle creates or depends on."
  - id: identity_context
    kind: key_map
    description: "run_as principal, service principal, credential passthrough."

  # --- compute surface ---
  - id: compute_binding
    kind: enum
    description: "What the asset is bound to: serverless, new cluster, existing cluster, SQL warehouse."
  - id: environment_version
    kind: key_map
    description: "Serverless environment/client version and base environment."

  # --- composition (production realism) ---
  - id: code_graph
    kind: graph
    description: "Nodes = code units; edges = reference mechanism. Depth > 1 is normal in production."
  - id: reference_mechanism
    kind: enum
  - id: task_graph
    kind: graph
    description: "Job tasks and their dependency edges."
  - id: task_kind
    kind: enum
    description: "What a task executes; not every kind is supported everywhere."
  - id: pipeline_semantics
    kind: key_map
    description: "DLT internals: expectations, streaming tables, materialized views, APPLY CHANGES."
  - id: source_control
    kind: key_map
    description: "Git-sourced job definitions vs workspace files."
  - id: signal_placement
    kind: enum
    description: "Where in the code graph an injected signal physically lives."
  - id: distractor_surface
    kind: enum
    description: >
      Where a NEAR-MISS token is placed. Anything on a distractor surface must never
      be treated as api_usage by the oracle or by an analyzer under test.

# =====================================================================
# VOCABULARY — closed value sets owned by the grammar. Feature skills
# may CONSTRAIN these (e.g. serverless allows only python/sql) but may
# not add values.
# =====================================================================
vocabulary:
  workload_types: [batch, streaming, pipeline, sql, ml]
  languages: [python, sql, scala, java, r]
  formats: [delta, parquet, json, csv, avro, orc, iceberg, text, binary]
  streaming_triggers: [available_now, processing_time, continuous, once]
  job_triggers: [manual, schedule, file_arrival, table_update, continuous]
  task_kinds:
    - notebook
    - python_script
    - python_wheel        # entry-point task
    - sql_task
    - sql_file
    - dbt
    - jar
    - spark_submit
    - pipeline_task
    - run_job             # job calling another job
    - for_each
    - condition           # if/else branching
  udf_kinds: [none, python_udf, pandas_udf, python_uc_udf, sql_udf, scala_udf, java_udf, applyInPandas]
  compute_bindings: [serverless, new_job_cluster, existing_cluster, instance_pool, sql_warehouse, unspecified]
  uc_object_kinds:
    [table, view, materialized_view, streaming_table, volume, function, model,
     external_location, storage_credential, connection, foreign_catalog,
     row_filter, column_mask]
  table_physical_keys:
    [partition_by, cluster_by, zorder_by, liquid_clustering_enabled, auto_optimize,
     change_data_feed, deletion_vectors, generated_columns, identity_columns,
     table_properties, target_file_size, small_file_count, skew_factor]
  pipeline_semantics_keys:
    [expectations, expectation_action, streaming_tables, materialized_views,
     apply_changes, scd_type, pipeline_edition, serverless_pipeline, channel]
  state_management_keys:
    [foreach_batch, watermark, stateful_operator, state_store_backend, output_mode]
  network_egress_kinds: [none, http_api, jdbc, odbc, cloud_sdk, private_endpoint, external_service]
  reference_mechanisms:
    - magic_run              # %run ./child_notebook
    - dbutils_notebook_run   # dbutils.notebook.run("child", timeout, args)
    - python_import          # repo module or wheel import
    - sql_include
    - pip_magic              # %pip install inside a notebook
    - none
  library_types:
    - none
    - whl_in_environment     # wheel declared in a serverless environment spec
    - whl_workspace_file     # wheel referenced from a workspace/Volume path
    - pip_requirements
    - pypi_coordinate
    - pip_magic_inline       # %pip install at notebook runtime
    - maven
    - jar
    - init_script
    - compute_scoped
  signal_placements:
    - entry
    - referenced             # one edge from entry
    - transitive             # two or more edges from entry
    - inside_library         # inside the generated wheel's source
    - task_config            # job/task configuration, not code
    - pipeline_config
    - table_property
  distractor_surfaces:
    - line_comment
    - block_comment_docstring
    - markdown_cell
    - string_literal
    - variable_or_column_name
    - unreachable_code       # after return, in a False branch
    - disabled_cell          # commented-out block
    - unrelated_library_name
    - log_message
  plan_statuses: [planned, materializing, materialized, failed, invalid, superseded]
  scenario_types: [positive, negative, edge, distractor, mixed]
  interaction_kinds: [independent, suppresses, subsumes, precedes, conflicts, requires]
  artifact_kinds:
    - bundle_yaml
    - notebook
    - python_module
    - sql_file
    - dbt_project
    - wheel
    - requirements_file
    - environment_spec
    - init_script
    - job
    - pipeline
    - table
    - view
    - function
    - volume
    - checkpoint_dir
    - model

# =====================================================================
# KNOBS — every tunable the generator consumes. Complexity is a knob,
# never a template. Bounds here are what knob_bounds_respected checks.
# =====================================================================
knobs:
  scenario_type:
    type: enum
    values: [positive, negative, edge, distractor, mixed]
    default: positive
  node_count:
    type: int
    min: 1
    max: 40
    default: 1
  reference_depth:
    type: int
    min: 0
    max: 5
    default: 0
  reference_mechanism_mix:
    type: enum_set
    values_from: vocabulary.reference_mechanisms
    default: [none]
  library_type:
    type: enum
    values_from: vocabulary.library_types
    default: none
  library_count:
    type: int
    min: 0
    max: 10
    default: 0
  task_count:
    type: int
    min: 1
    max: 25
    default: 1
  task_kind_mix:
    type: enum_set
    values_from: vocabulary.task_kinds
    default: [notebook]
  task_graph_shape:
    type: enum
    values: [single, linear, fan_out, fan_in, diamond, nested_for_each, conditional, mixed]
    default: single
  signal_placement:
    type: enum
    values_from: vocabulary.signal_placements
    default: entry
    applies_when: { scenario_type_in: [negative, edge, mixed] }
  defect_count:
    type: int
    min: 1
    max: 5
    default: 1
    applies_when: { scenario_type_in: [negative, edge, mixed] }
  distractor_count:
    type: int
    min: 0
    max: 10
    default: 0
  distractor_surface_mix:
    type: enum_set
    values_from: vocabulary.distractor_surfaces
    default: []
    applies_when: { knob_gt: { distractor_count: 0 } }
  distractor_targets_signal:
    type: signal_id_list
    default: []
    description: "Which signal ids the near-misses should imitate. Empty = spread across all."
  table_physical_profile:
    type: enum
    values: [unset, partitioned, clustered, zordered, liquid, skewed, small_files, wide_schema]
    default: unset
  pipeline_profile:
    type: enum
    values: [none, basic, expectations, apply_changes_scd1, apply_changes_scd2, mixed]
    default: none
  uc_object_mix:
    type: enum_set
    values_from: vocabulary.uc_object_kinds
    default: [table]
  compute_binding:
    type: enum
    values_from: vocabulary.compute_bindings
    default: unspecified
  environment_version_profile:
    type: enum
    values: [unset, current, pinned_older, mismatched]
    default: unset
  udf_kind_mix:
    type: enum_set
    values_from: vocabulary.udf_kinds
    default: [none]
  network_egress_profile:
    type: enum
    values: [none, light, heavy, private_endpoint]
    default: none
  param_passing:
    type: enum
    values: [none, widgets, job_parameters, notebook_run_args, env_var, task_values]
    default: none
  row_multiplier:
    type: int
    min: 1
    max: 1000
    default: 1
  quality_checks:
    type: enum
    values: [auto, force_on, force_off]
    default: auto
  feature_interaction_mode:
    type: enum
    values: [strict, report_only]
    default: strict
    description: "strict = interaction rules rewrite the merged verdict; report_only = record but keep per-feature verdicts."

# =====================================================================
# PLAN SCHEMA — the contract between generation and materialization,
# and the record reviewed downstream. Field names are normative.
# =====================================================================
plan_schema:
  required:
    - plan_id            # stable unique id; stamped onto every artifact
    - plan_version
    - plan_intent        # the natural-language ask this plan realizes
    - plan_generated_at  # ISO-8601 UTC
    - plan_status
    - target_features    # drives which feature skills load
    - knobs              # every resolved knob value, including defaults
    - assets
    - code_graph
    - expected           # deterministic oracle result
    - artifacts
  optional:
    - parent_plan_id
    - distractors
    - notes

  asset_schema:
    required: [asset_id, workload_type, language]
    optional:
      [format, streaming_trigger, job_trigger, storage_path, checkpoint_location,
       row_count, column_types, table_physical, libraries, runtime_config,
       compute_binding, environment_version, udf_kind, network_egress,
       local_filesystem, state_management, uc_object_kind, identity_context,
       pipeline_semantics, source_control, has_quality_checks, compute_target]

  code_graph_schema:
    nodes: "[{ node_id, artifact_kind, language, role: entry|child|module|library_src }]"
    edges: "[{ from_node, to_node, reference_mechanism, args_passed }]"
    constraints:
      - "exactly one node with role=entry per asset"
      - "no cycles"
      - "every node reachable from an entry node"

  distractor_schema:
    required: [distractor_id, imitates_signal, surface, node_id]
    note: >
      A distractor records a near-miss deliberately planted. It must NEVER appear in
      expected.matched_signals. Its presence in that list is an oracle bug.

  expected_schema:
    required: [per_feature, verdict, matched_signals, interactions_applied, oracle_version]
    per_feature: "[{ feature, verdict, matched_signals }]"
    verdict: "merged verdict after interaction resolution"
    matched_signals: "[{ signal_id, feature, dimension, node_id, evidence_surface }]"
    interactions_applied: "[{ rule_id, kind, winner_feature, loser_feature, rationale }]"
    note: "evidence_surface must be 'executable'; anything else indicates a distractor leak."

  artifact_schema:
    required: [artifact_id, artifact_kind, path, plan_id]
    optional: [node_id, checksum, build_status]
    note: "plan_id is mandatory on every artifact — jobs, pipelines, tables, views, wheels, volumes, checkpoints."

# =====================================================================
# GENERATION RULES — how a plan is composed from knobs. Structural only;
# feature skills inject their own signals through apply rules.
# =====================================================================
generation_rules:
  - id: resolve_knobs_first
    effect: "Resolve every knob (explicit or default) and record it in plan.knobs before composing anything."

  - id: build_code_graph
    match: { knob_present: node_count }
    effect:
      build: code_graph
      with: { node_count: from_knob, depth: from_knob.reference_depth, edge_labels: from_knob.reference_mechanism_mix }
      constraints_from: plan_schema.code_graph_schema.constraints

  - id: single_node_shortcut
    match: { knob_equals: { node_count: 1 } }
    effect: "Emit one entry node, reference_mechanism=none. Do not fabricate children."

  - id: build_task_graph
    match: { knob_present: task_count }
    effect:
      build: task_graph
      shape_from: knob.task_graph_shape
      kinds_from: knob.task_kind_mix
      note: "A task graph is not a code graph. for_each and condition tasks nest; record nesting depth."

  - id: apply_table_physical
    match: { knob_not_equals: { table_physical_profile: unset } }
    effect:
      set_field: table_physical
      value_from_profile: knob.table_physical_profile
      note: "Profiles expand to table_physical_keys; feature skills read them, this file only sets them."

  - id: apply_pipeline_semantics
    match: { workload_type_in: [pipeline], knob_not_equals: { pipeline_profile: none } }
    effect: { set_field: pipeline_semantics, value_from_profile: knob.pipeline_profile }

  - id: require_checkpoint_for_streaming
    match: { workload_type_in: [streaming] }
    effect: { ensure_field: checkpoint_location }
    note: "A streaming asset without a checkpoint is structurally invalid regardless of feature."

  - id: attach_libraries
    match: { knob_gt: { library_count: 0 } }
    effect:
      add_libraries: { type_from: knob.library_type, count_from: knob.library_count }
      and_when: { library_type_in: [whl_in_environment, whl_workspace_file] }
      also_emit: [wheel, environment_spec]

  - id: synthesize_wheel_source
    match: { artifact_kind_planned: wheel }
    effect:
      emit_nodes: { role: library_src, contents: "generated module with a deterministic, plan-derived package name" }
      note: "The wheel must be BUILT from generated source, never copied from a fixture."

  - id: place_signal
    match: { scenario_type_in: [negative, edge, mixed] }
    effect:
      select_node:
        by_knob: signal_placement
        resolution:
          entry: "role=entry"
          referenced: "any node exactly 1 edge from entry"
          transitive: "any node >=2 edges from entry"
          inside_library: "role=library_src"
          task_config: "task_graph node config, no code node"
          pipeline_config: "pipeline_semantics key, no code node"
          table_property: "table_physical key, no code node"
      then: "delegate injection to the feature skill's apply_rules for that node"

  - id: plant_distractors
    match: { knob_gt: { distractor_count: 0 } }
    effect:
      for_each_distractor:
        imitate_signal_from: knob.distractor_targets_signal
        place_on_surface_from: knob.distractor_surface_mix
        record_in: plan.distractors
      note: >
        The near-miss must be textually convincing (a real signal token) but placed
        where it cannot execute. This is the precision test.

  - id: distractor_only_scenario
    match: { knob_equals: { scenario_type: distractor } }
    effect: "Plant distractors and NO real signals. expected.matched_signals must resolve empty."

  - id: stamp_lineage
    match: { artifact: any }
    effect: { set_field: plan_id, value_from: plan.plan_id }

  - id: scale_rows
    match: { asset_has: row_count }
    effect: { multiply_field: row_count, by_knob: row_multiplier }

# =====================================================================
# FEATURE INTERACTIONS — how verdicts from multiple feature skills are
# reconciled. Feature skills declare their side; this table defines how
# a declaration resolves. Without this, target_features is just a list
# and two features can return contradictory advice about one asset.
# =====================================================================
feature_interactions:
  resolution_order:
    - "Resolve each feature independently -> expected.per_feature."
    - "Evaluate interaction rules in declared order; first match per feature pair wins."
    - "Write the merged verdict to expected.verdict and log every rule fired."
  kinds:
    - kind: independent
      effect: "Both verdicts stand; merged verdict is the strongest."
    - kind: suppresses
      effect: "Winner's verdict stands; loser's recommendation is marked not_applicable with a rationale."
    - kind: subsumes
      effect: "Loser's finding is folded into the winner's verdict; not reported separately."
    - kind: precedes
      effect: "Loser's recommendation is deferred until the winner's blocking findings are resolved."
    - kind: conflicts
      effect: "Both are reported and the merged verdict is review; a conflict must never be auto-resolved silently."
    - kind: requires
      effect: "Loser's verdict is only valid if the winner's verdict is eligible."
  rules:
    - id: platform_managed_capability_is_moot
      when:
        feature_a_provides: platform_managed_capability
        feature_b_recommends: same_capability
      kind: suppresses
      rationale: "A capability the platform manages automatically cannot be a recommendation on that platform."
      note: >
        Concrete instance: a target bound to serverless manages Photon itself, so a
        Photon-enablement recommendation is not_applicable there. Expressed structurally
        so it holds for any future platform-managed capability.
    - id: blocking_beats_optimizing
      when:
        feature_a_verdict_in: [blocked]
        feature_b_verdict_in: [eligible, review]
      kind: precedes
      rationale: "Layout or performance advice is premature while the workload cannot run at all."
    - id: layout_advice_contested
      when:
        both_features_target_dimension: table_physical
        recommendations_disagree: true
      kind: conflicts
      rationale: "Two features prescribing different physical layouts for one table is a real conflict, not a merge."
    - id: default_independent
      when: { always: true }
      kind: independent

# =====================================================================
# ORACLE — computes plan.expected deterministically. Ground truth for
# regression testing. Runs on the PLAN, before any file is written.
# =====================================================================
oracle:
  procedure:
    - "Load the feature skills named in plan.target_features."
    - "Flatten the code graph: api_usage is the union of EXECUTABLE text across all reachable nodes."
    - "Exclude every distractor surface from that union before matching."
    - "Evaluate each feature skill's signal definitions against the flattened dimensions."
    - "Record each match as { signal_id, feature, dimension, node_id, evidence_surface }."
    - "Resolve each feature's verdict via its own eligibility table -> expected.per_feature."
    - "Apply feature_interactions to produce expected.verdict and interactions_applied."
    - "Freeze expected before materialization."
  rules:
    - id: union_across_graph
      rule: "A signal in ANY reachable node counts. Entry-only evaluation is an oracle defect."
    - id: executable_surface_only
      rule: >
        Only executable code contributes to api_usage. Comments, docstrings, markdown,
        string literals, disabled cells, and unreachable branches never match a signal.
    - id: no_model_in_oracle
      rule: "Pure code over plan data. If a judgement needs a model, it is not oracle-eligible."
    - id: freeze_before_materialize
      rule: "expected is written before materialization and never recomputed from emitted files."
    - id: distractors_never_match
      rule: "If a planted distractor appears in matched_signals, fail the plan as invalid — the oracle is wrong, not the bundle."

# =====================================================================
# MATERIALIZATION CONTRACT — what deterministic code emits from a plan.
# =====================================================================
materialization_contract:
  emits:
    - artifact_kind: bundle_yaml
      rule: "One databricks.yml declaring jobs/pipelines from task_graph; resources reference emitted files by path."
    - artifact_kind: notebook
      rule: "One file per code node with role in [entry, child]; edges rendered using the edge's reference_mechanism."
    - artifact_kind: python_module
      rule: "One file per node with role=module; imported, not %run."
    - artifact_kind: wheel
      rule: "Build from role=library_src nodes via a generated pyproject.toml; build must succeed or plan_status=failed."
    - artifact_kind: environment_spec
      rule: "Emitted whenever library_type is whl_in_environment or pip_requirements; carries environment_version."
    - artifact_kind: pipeline
      rule: "Emitted for workload_type=pipeline; renders pipeline_semantics into DLT decorators/SQL."
    - artifact_kind: table
      rule: "Emitted per data asset with row_count rows honouring column_types and table_physical."
    - artifact_kind: view
      rule: "Emitted for uc_object_kind entries of view/materialized_view/streaming_table."
    - artifact_kind: function
      rule: "Emitted per udf_kind entry other than none."
    - artifact_kind: volume
      rule: "Emitted when any path is Volume-shaped or a wheel needs a landing location."
    - artifact_kind: checkpoint_dir
      rule: "Emitted for every streaming asset's checkpoint_location."
  rules:
    - id: plan_only_input
      rule: "The materializer reads the plan and nothing else. No re-planning, no defaults invented here."
    - id: edges_must_resolve
      rule: "Every rendered reference must point at a path that exists in the emitted artifact set."
    - id: widgets_preamble
      rule: >
        When knobs.param_passing is widgets, the entry notebook opens with the client's
        canonical parameter preamble: dbutils.widgets.removeAll(), then a
        widgets.text(name, "") / widgets.get(name) pair per declared parameter, BEFORE any
        edge rendering or business logic. Parameters come from the plan's asset/param
        declarations; if none are declared, render a small representative set. Evidence:
        this preamble is the single most consistent pattern in the client's real entry
        notebooks (~237 dbutils.widgets occurrences across three sampled bundles).
    - id: schedule_block
      rule: >
        When the plan's job_trigger resolves to schedule, databricks.yml's job carries a
        schedule block with a Quartz cron expression and an explicit timezone_id, matching
        the client's observed form ("13 0 7 * * ?" + America/Los_Angeles). manual renders
        no schedule block at all. Other trigger kinds (file_arrival, table_update,
        continuous) remain vocabulary-valid but are not yet rendered — a plan resolving to
        one of them must be flagged, not silently rendered as manual.
    - id: job_unit_scope
      rule: >
        Generated bundles model the JOB unit — one job's entry notebook, children, modules,
        and library — never the repository unit. Real client repos accumulate hundreds of
        operational files (playbooks, monitoring configs, batch lists) around their jobs;
        that sprawl is deliberately out of scope, and a size gap between a generated bundle
        and a real repo is expected, not a fidelity failure.
    - id: distractors_rendered_inert
      rule: "Planted distractors are rendered on their declared surface only and must not be executable."
    - id: stamp_everything
      rule: "Write plan_id and plan_version into every artifact — YAML comment, notebook header cell, table property, wheel metadata."
    - id: status_transitions
      rule: "planned -> materializing -> materialized | failed. Record terminal status and timestamp on the plan."

# =====================================================================
# VALIDATION RULES — deterministic checks over plan + emitted artifacts.
# Feature-specific correctness is delegated.
# =====================================================================
validation_rules:
  - id: plan_schema_complete
    check: { assert_fields_present: plan_schema.required }
  - id: knob_bounds_respected
    check: { assert_knobs_within_declared_bounds: true }
  - id: vocabulary_closed
    check: { assert_all_enum_fields_in_vocabulary: true }
    note: "Out-of-vocabulary values are rejected, never coerced."
  - id: graph_well_formed
    check: { assert_graph_constraints: plan_schema.code_graph_schema.constraints }
  - id: depth_matches_knob
    check: { assert_equals: { measured: code_graph.max_depth, expected: knobs.reference_depth } }
  - id: every_reference_resolves
    check: { for_each: edge, assert_target_artifact_exists: true }
  - id: task_kinds_in_vocabulary
    check: { for_each: task, assert_field_in_vocabulary: { field: task_kind, vocabulary: task_kinds } }
  - id: streaming_has_checkpoint
    check: { for_each: { asset_where: { workload_type_in: [streaming] } }, assert_field_present: checkpoint_location }
  - id: signal_present_in_graph
    check:
      when: { knob_equals: { scenario_type: negative } }
      assert: "expected.matched_signals is non-empty"
  - id: signal_at_declared_placement
    check:
      when: { knob_present: signal_placement }
      assert: "every injected signal's node_id satisfies the placement resolution rule"
  - id: distractor_scenario_is_clean
    check:
      when: { knob_equals: { scenario_type: distractor } }
      assert: "expected.matched_signals is empty AND plan.distractors is non-empty"
    note: "A distractor bundle that resolves to a real signal is invalid — it tests nothing."
  - id: distractors_on_declared_surface
    check: { for_each: distractor, assert_field_in_vocabulary: { field: surface, vocabulary: distractor_surfaces } }
  - id: no_distractor_in_matched_signals
    check: { assert_disjoint: [plan.distractors.node_id_surface, expected.matched_signals.evidence_surface] }
  - id: evidence_is_executable
    check: { for_each: matched_signal, assert_field_equals: { field: evidence_surface, value: executable } }
  - id: interactions_resolved
    check:
      when: { target_features_count_gt: 1 }
      assert: "expected.interactions_applied is present and every feature pair resolved to a declared interaction kind"
  - id: conflicts_not_auto_resolved
    check: { assert: "no interaction of kind=conflicts was silently merged; merged verdict must be review" }
  - id: artifacts_stamped
    check: { for_each: artifact, assert_field_present: plan_id }
  - id: wheel_builds
    check: { when: { artifact_kind_present: wheel }, assert_build_status: success }
  - id: expected_frozen
    check: { assert_present: [expected.verdict, expected.oracle_version] }
  - id: feature_rules_delegated
    check: { for_each: feature_in_target_features, run_validation_rules_of: feature_skill }

# =====================================================================
# COVERAGE — what a suite must span before it can be called complete.
# Used to report gaps, not to block a single generation.
# =====================================================================
coverage_axes:
  - axis: signal_placement
    require: "every placement value exercised per signal that can be placed there"
  - axis: distractor_surface
    require: "every surface exercised at least once per signal it can imitate"
  - axis: task_kind
    require: "every task kind appearing at least once"
  - axis: workload_type
    require: "every workload type appearing at least once per target feature"
  - axis: library_type
    require: "every library type exercised, since type drives verdict"
  - axis: scenario_type
    require: "positive, negative, edge and distractor all present for each feature"
  - axis: feature_pair
    require: "every registered feature pair exercised at least once when features are combined"

# =====================================================================
# FEATURE SKILL REGISTRY — the extension point. Adding a feature means
# adding an entry and a skill file. Nothing above changes.
# =====================================================================
feature_skills:
  contract:
    must_define: [signals, eligibility_signals, apply_rules, validation_rules, vocabulary]
    may_define: [interaction_declarations, platform_managed_capabilities]
    may_constrain: "grammar vocabulary values (e.g. restrict languages to python/sql)"
    must_not: "define plan fields, artifact kinds, materialization behavior, or composition knobs"
  registered:
    - feature: serverless
      skill: serverless
      status: active
      platform_managed_capabilities: [photon, autoscaling, capacity]
    - feature: liquid_clustering
      skill: liquid-clustering
      status: planned
      primary_dimensions: [table_physical, column_types]
    - feature: photon
      skill: photon
      status: planned
      primary_dimensions: [api_usage, format, column_types, compute_binding]
```

---

## Guidance

*(LLM reasoning hints only. Nothing here is code-checked; nothing here may override the YAML.)*

**Mapping a request to knobs.** Most requests name a feature and a rough difficulty, not a
knob set. Translate: "a realistic production job" → `node_count` 5–15, `reference_depth`
2–3, at least two reference mechanisms, `task_count` > 1, a library attached, and a handful
of distractors. "A simple positive case" → defaults, `node_count: 1`. "Something that should
fail" → `scenario_type: negative` with `signal_placement` deeper than `entry`. Always record
resolved knobs, including defaults — an unstated default is invisible in a regression report.

**Placement matters more than count.** Ten notebooks with the defect in the entry node test
nothing that one notebook doesn't. A deep graph distinguishes an analyzer that traverses
references from one that reads the first file. Vary `signal_placement` across its full
vocabulary for the *same* signal to produce a detection-rate-by-depth curve.

**Distractors are the precision half of the suite.** Recall-only suites reward naive
matching: a regex analyzer that flags `.rdd` anywhere scores perfectly while flooding
production with false positives. A good distractor is textually convincing and structurally
inert — `SparkContext` named in a docstring, `dbfs:/` in a markdown cell, a Maven coordinate
in a commented-out block, a column called `rdd_source_id`. Report precision and recall
separately; an approach that wins on one and loses on the other is not the better approach.

**Libraries are a verdict lever, not decoration.** The same dependency expressed as
`whl_in_environment` versus `maven` or `compute_scoped` flips the expected verdict. Treat
`library_type` as a primary axis for negative cases, and never inject a library defect
without asserting the feature skill recognises it.

**Interactions are where multi-feature suites go wrong.** Independent resolution will
happily emit "enable Photon" for a serverless target, where Photon is managed by the
platform and the advice is meaningless. That is why `platform_managed_capability_is_moot`
exists and why it is written structurally rather than naming Photon: the next
platform-managed capability gets the same treatment for free. When two features prescribe
different physical layouts for one table, that is `conflicts` — surface it for review, never
merge it silently.

**Scaling to harder scenarios.** Knobs compose, so difficulty grows without new code paths:
fan-in task graphs with `for_each` nesting, parameters threaded through
`dbutils.notebook.run` args and task values, a defect inside a built wheel, several signals
at different depths via `defect_count`, mixed languages where only a transitive node is
Scala, a pipeline using `APPLY CHANGES` with expectations. If a scenario cannot be expressed
by composing knobs, the missing piece is a *dimension* — add it here rather than
special-casing the generator.

**Defaults when the request is silent.** `scenario_type`: positive. `node_count`: 1.
`reference_depth`: 0. `library_type`: none. `distractor_count`: 0. `param_passing`: none.
`compute_binding`: unspecified. Missing `target_features`: infer from the intent text, and if
still ambiguous, ask rather than guess — a plan generated against the wrong feature is the
most expensive kind of wrong.

**What never belongs in a plan.** Free-text descriptions of defects ("added an unsupported
library") instead of signal ids; asset or table names carrying meaning; a field whose purpose
cannot be stated in one sentence. If a reviewer has to ask what a field is for, it is either
misnamed or unnecessary — rename it to say what it does, or drop it.

**Regeneration and lineage.** Repairing a failed plan produces a NEW `plan_id` with
`parent_plan_id` set and `plan_version` incremented; the old plan moves to `superseded`.
Never mutate a materialized plan in place — the point of stamping `plan_id` on artifacts is
that any emitted table, job, view, or wheel traces back to the exact plan that produced it.

**Honest limits.** This grammar covers what can be expressed as structure. It does not model
data *semantics* (whether generated values are realistic for a domain), runtime behaviour
(actual execution, cost, or wall-clock performance), or workspace-level state (existing
catalogs, permissions, quotas). Those are out of scope by design; if a feature needs them,
they arrive as new dimensions with explicit vocabulary, not as assumptions in the generator.
