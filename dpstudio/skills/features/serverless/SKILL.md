---
name: serverless
description: The authoritative rule and knowledge file for Databricks Serverless compute, applied to generated data asset bundles. Consult this skill for ANY serverless-related activity on a bundle or config — assessing serverless eligibility/readiness, explaining the impact of moving a workload to serverless, applying serverless characteristics (compute targets, triggers, formats, defect patterns) to a bundle, verifying or validating a bundle for serverless correctness, planting serverless near-miss distractors, or answering any question about Databricks Serverless constraints (Spark Connect, RDD/SparkContext, DBFS, streaming triggers, AvailableNow, Unity Catalog Volumes, libraries, ANSI mode, environments, egress). Use it whenever the user mentions serverless, serverless SQL/jobs/pipelines, "will this run on serverless", "make this serverless", or serverless migration/compatibility — even if they don't say "Databricks" explicitly.
---

# Databricks Serverless — Rules & Knowledge

This file is the single authority for serverless behavior in this system. It is **not** a
workflow. It exposes serverless knowledge as reusable, composable rules so that *any*
activity — eligibility assessment, impact explanation, applying serverless to a bundle,
validation, or ad-hoc Q&A — can be answered by selecting and combining the rules below.
A future activity nobody has thought of yet should also be answerable this way; if it
isn't, extend the rule data, don't add a workflow.

The file has two strictly separated zones:

1. **Machine-readable zone** — structured YAML. Code validates and reasons against this.
   Every checkable statement about serverless lives here as data.
2. **Guidance zone** — prose hints for LLM reasoning only. Never code-checked. Nothing in
   it may contradict the YAML; when in doubt, the YAML wins.

Three invariants to preserve when reading or editing this file:

- **Config-agnostic matching.** Every rule matches on *structural signals and dimensions*
  (asset types, modes, formats, API usage patterns, path shapes, library types, column
  types) — never on a named asset, table, or job.
- **One definition of "incompatible."** The `signals` block is the single source of truth.
  Eligibility, impact, apply (defect injection), distractors, and validation all reference
  signals by `id`. This is what guarantees that "assess", "explain", and "validate" can
  never disagree about what serverless-incompatible means.
- **Feature scope only.** This file supplies *judgement* — what counts as a defect and what
  verdict follows. It never defines plan fields, artifact kinds, composition knobs, or
  materialization behavior. Those belong to the `asset-bundle-generation` grammar.

Dimension names themselves (e.g. `workload_type`, `streaming_trigger`, `code_graph`,
`signal_placement`) are defined by the archetype-agnostic grammar in
`asset-bundle-generation`; this file only supplies the serverless-specific values and rules
for those dimensions. **Both files must be loaded together** — this one alone cannot
generate a bundle, and that one alone cannot judge one.

Signals carry a `status`: `verified` (confirmed against platform behavior) or `proposed`
(added for coverage, pending SME sign-off). Treat `proposed` signals as live for generation
but flag them in any client-facing report until verified.

---

## Machine-readable zone

```yaml
version: "2.0"
composes_with:
  grammar: asset-bundle-generation
  grammar_version: ">=2.0"
  note: >
    This file supplies signals, eligibility, impact, apply rules, distractor templates,
    interaction declarations, and platform facts. The grammar supplies dimensions,
    composition knobs, plan schema, oracle, materialization, and structural validation.

# =====================================================================
# SIGNALS — the single, shared definitions of serverless-incompatible
# (or serverless-sensitive) conditions. All other sections reference
# these by id. A signal is a matchable structural condition, never a
# named asset. Patterns are case-sensitive regex over the indicated
# dimension of the bundle.
#
# IMPORTANT: patterns match EXECUTABLE surfaces only. The grammar's
# oracle excludes comments, docstrings, markdown, string literals, and
# unreachable code before matching (see oracle.executable_surface_only).
# =====================================================================
signals:
  - id: rdd_or_sparkcontext
    status: verified
    dimension: api_usage
    match:
      any_pattern:
        - "SparkContext"
        - "\\bsc\\.\\w"
        - "\\.rdd\\b"
        - "\\bparallelize\\("
        - "mapPartitions|foreachPartition"
        - "hadoopConfiguration"
        - "setJobGroup|setLocalProperty"
    why: >
      Serverless runs Spark Connect exclusively. There is no client-visible
      SparkContext and no RDD API; only the DataFrame / Spark SQL surface exists.

  - id: dbfs_root_path
    status: verified
    dimension: storage_path
    match:
      any_pattern:
        - "^dbfs:/(?!Volumes/)"
        - "^/dbfs/(?!Volumes/)"
    why: >
      DBFS root (including /FileStore and mounts) is not accessible from
      serverless. File access goes through Unity Catalog Volumes
      (/Volumes/<catalog>/<schema>/<volume>/...) or governed tables.

  - id: trigger_processing_time
    status: verified
    dimension: streaming_trigger
    match:
      equals: "processing_time"
    why: >
      Trigger.ProcessingTime (interval micro-batching) is not supported on
      serverless. The only valid streaming trigger is AvailableNow
      (incremental batch: process everything available, then stop).

  - id: trigger_continuous
    status: verified
    dimension: streaming_trigger
    match:
      equals: "continuous"
    why: >
      Trigger.Continuous is not supported on serverless. Use AvailableNow.

  - id: maven_or_compute_scoped_library
    status: verified
    dimension: library
    match:
      library_type_in: [maven, jar, compute_scoped, init_script]
    why: >
      Serverless has no user-managed cluster, so compute-scoped libraries,
      Maven/JAR coordinates, and init scripts cannot be installed.
      Dependencies must be pip/wheel packages declared in a serverless
      environment specification.

  - id: env_var_dependency
    status: verified
    dimension: runtime_config
    match:
      any_key: ["spark_env_vars"]
      any_pattern:
        - "os\\.environ\\["
        - "os\\.getenv\\("
        - "System\\.getenv"
    why: >
      There is no cluster configuration surface on serverless, so custom
      environment variables cannot be set. Configuration must move to
      widgets/job parameters, Unity Catalog, or secret scopes.

  - id: global_temp_view
    status: verified
    dimension: api_usage
    match:
      any_pattern:
        - "createGlobalTempView|createOrReplaceGlobalTempView"
        - "\\bglobal_temp\\."
    why: >
      Global temporary views are not supported under Spark Connect /
      serverless session isolation. Use session-scoped temp views or
      Unity Catalog tables to share state.

  - id: distributed_training_api
    status: verified
    dimension: api_usage
    match:
      any_pattern:
        - "TorchDistributor"
        - "HorovodRunner"
        - "spark_tensorflow_distributor"
        - "DeepspeedTorchDistributor"
        - "pyspark\\.ml\\.connect\\.distributed|sparkdl"
    why: >
      Distributed ML training APIs require direct executor/GPU control,
      which serverless does not expose (no GPUs, no SparkContext-based
      task scheduling). Single-node training or Mosaic/managed training
      services are the alternatives.

  - id: non_python_sql_language
    status: verified
    dimension: language
    match:
      language_in: [scala, java, r]
      or_udf_kind_in: [scala_udf, java_udf]
    why: >
      Serverless notebooks and jobs execute Python and SQL only. Scala,
      Java, and R workloads (including Scala UDFs) cannot run.

  - id: spark_conf_override
    status: verified
    dimension: runtime_config
    match:
      any_key: ["spark_conf"]
      any_pattern:
        - "spark\\.conf\\.set\\("
    why: >
      Most Spark configuration is fixed on serverless (a small allow-list
      of session confs is settable). Cluster sizing, shuffle, and memory
      confs are ignored or rejected; behavior must be verified, not assumed.

  - id: lax_sql_semantics
    status: verified
    dimension: sql_semantics
    match:
      any_pattern:
        - "spark\\.sql\\.ansi\\.enabled\\s*=?\\s*false"
      relies_on: [implicit_lossy_cast, silent_overflow, division_by_zero_null]
    why: >
      Serverless runs with ANSI SQL mode ON by default. Code that relies on
      lax semantics (silent overflow, invalid casts returning NULL, /0
      returning NULL) will start raising errors or must use try_* functions.

  # ---------------- added in v2.0 (pending SME verification) ----------------
  - id: existing_cluster_binding
    status: proposed
    dimension: compute_binding
    match:
      compute_binding_in: [existing_cluster, instance_pool]
      or_any_key: ["existing_cluster_id", "instance_pool_id"]
    why: >
      A task pinned to an existing cluster or instance pool is by definition not
      running serverless. The binding must be removed before a serverless target
      can be assigned.

  - id: local_filesystem_dependency
    status: proposed
    dimension: local_filesystem
    match:
      any_pattern:
        - "^/tmp/"
        - "^/local_disk0"
        - "open\\(\\s*['\"]\\/(?!Volumes)"
    why: >
      Driver-local disk on serverless is ephemeral and not shared across tasks or
      retries. Work that persists to local paths silently loses data between runs.
      Durable output belongs in UC Volumes or tables.

  - id: checkpoint_not_uc_shaped
    status: proposed
    dimension: checkpoint_location
    match:
      any_pattern:
        - "^dbfs:/(?!Volumes/)"
        - "^/dbfs/(?!Volumes/)"
        - "^/tmp/"
    why: >
      Streaming checkpoints must live somewhere serverless can reach and retain.
      A DBFS-root or local checkpoint path fails or silently resets stream state.

  - id: network_egress_dependency
    status: proposed
    dimension: network_egress
    match:
      egress_kind_in: [http_api, jdbc, odbc, external_service, private_endpoint]
    why: >
      Serverless egress is governed by network policies / NCC rather than cluster-level
      networking. External calls that worked on classic compute must be re-authorized;
      private endpoints need explicit configuration.

  - id: notebook_scoped_pip
    status: proposed
    dimension: library
    match:
      library_type_in: [pip_magic_inline]
      any_pattern:
        - "%pip install"
    why: >
      %pip install works on serverless but is session-scoped and re-resolved on every
      run, adding startup latency and making dependency versions non-deterministic.
      Declaring dependencies in the serverless environment is the durable form.

  - id: environment_version_pinned_old
    status: proposed
    dimension: environment_version
    match:
      any_key: ["client", "base_environment"]
      relies_on: [pinned_older_client]
    why: >
      Serverless environment/client versions gate which APIs are available. A pinned
      older client can silently lack features the code assumes; a mismatch between
      declared environment and code requirements surfaces only at runtime.

  - id: foreach_batch_nested_logic
    status: proposed
    dimension: state_management
    match:
      any_key: ["foreach_batch"]
      any_pattern:
        - "foreachBatch"
    why: >
      foreachBatch itself is supported, but code inside the batch function executes under
      the same Spark Connect constraints. RDD/SparkContext usage hidden inside the
      function is a common source of late failures.

# =====================================================================
# ELIGIBILITY — routes a bundle to a verdict. Each entry binds a signal
# to a verdict and a reason. The strongest verdict present wins
# (blocked > review_before_serverless > eligible). A bundle with no
# matching entries is eligible.
# =====================================================================
eligibility_signals:
  - signal: rdd_or_sparkcontext
    verdict: blocked
    reason: "RDD/SparkContext usage cannot run on Spark Connect; code rewrite to DataFrame API required first."
  - signal: non_python_sql_language
    verdict: blocked
    reason: "Only Python and SQL execute on serverless; the workload language is unsupported."
  - signal: trigger_continuous
    verdict: blocked
    reason: "Continuous trigger has no serverless equivalent semantics; stream must be redesigned around AvailableNow."
  - signal: distributed_training_api
    verdict: blocked
    reason: "Distributed training needs executor/GPU control that serverless does not expose."
  - signal: existing_cluster_binding
    verdict: blocked
    reason: "The task is pinned to a specific cluster or pool; serverless cannot be assigned until that binding is removed."
  - signal: dbfs_root_path
    verdict: review_before_serverless
    reason: "Paths must be migrated from DBFS root to Unity Catalog Volumes; mechanical but must be reviewed for mounts and external locations."
  - signal: trigger_processing_time
    verdict: review_before_serverless
    reason: "Trigger must change to AvailableNow; latency profile changes from near-continuous to scheduled incremental batch — confirm acceptable."
  - signal: maven_or_compute_scoped_library
    verdict: review_before_serverless
    reason: "Dependencies must be re-declared as pip/wheel in a serverless environment; JVM-only libraries have no path forward."
  - signal: env_var_dependency
    verdict: review_before_serverless
    reason: "Environment variables must be replaced with job parameters, secrets, or UC-managed configuration."
  - signal: global_temp_view
    verdict: review_before_serverless
    reason: "Global temp views must become session temp views or UC tables; verify no cross-session sharing was intended."
  - signal: spark_conf_override
    verdict: review_before_serverless
    reason: "Custom Spark confs are mostly ignored/rejected on serverless; each override must be checked against the settable allow-list."
  - signal: lax_sql_semantics
    verdict: review_before_serverless
    reason: "ANSI mode is on by default; queries relying on lax casts/overflow need try_cast/try_divide or explicit review."
  - signal: local_filesystem_dependency
    verdict: review_before_serverless
    reason: "Local disk is ephemeral on serverless; durable writes must move to UC Volumes or tables."
  - signal: checkpoint_not_uc_shaped
    verdict: review_before_serverless
    reason: "Checkpoint location must be reachable and durable from serverless; migrate to a UC Volume path."
  - signal: network_egress_dependency
    verdict: review_before_serverless
    reason: "External calls must be re-authorized under serverless network policy; confirm endpoints and credentials."
  - signal: environment_version_pinned_old
    verdict: review_before_serverless
    reason: "Pinned environment/client version may not expose APIs the code assumes; verify against the declared environment."
  - signal: foreach_batch_nested_logic
    verdict: review_before_serverless
    reason: "Inspect the batch function body — Connect constraints apply inside it just as they do outside."
  - signal: notebook_scoped_pip
    verdict: review_before_serverless
    reason: "Works, but non-deterministic and slow per run; prefer environment-declared dependencies for scheduled jobs."

# =====================================================================
# COMPUTE TARGETS — maps any asset (by structure, never by name) to the
# serverless compute value it should carry. Evaluate in order; first
# match wins.
# =====================================================================
compute_targets:
  - target: review_before_serverless
    applies_when:
      bundle_has_eligibility_verdict_in: [blocked, review_before_serverless]
    note: "Overrides all concrete targets until the flagged signals are resolved or waived."

  - target: serverless_pipeline_with_checks
    applies_when:
      workload_type_in: [streaming, pipeline]
      has_quality_checks: true

  - target: serverless_pipeline
    applies_when:
      workload_type_in: [streaming, pipeline]

  - target: serverless_sql
    applies_when:
      workload_type_in: [sql]
      procedural_code: false

  - target: serverless_job_with_checks
    applies_when:
      workload_type_in: [batch, ml]
      has_quality_checks: true

  - target: serverless_job
    applies_when:
      workload_type_in: [batch, ml, sql]   # sql with procedural code falls through to job

# =====================================================================
# SIGNAL PLACEMENT ELIGIBILITY — which grammar signal_placements each
# signal can physically occupy. The grammar's place_signal rule consults
# this before selecting a node; asking for an impossible combination
# (e.g. a library signal placed inside notebook code) is a plan error,
# not something to silently relocate.
# =====================================================================
placement_eligibility:
  - signal: rdd_or_sparkcontext
    placements: [entry, referenced, transitive, inside_library]
  - signal: global_temp_view
    placements: [entry, referenced, transitive, inside_library]
  - signal: distributed_training_api
    placements: [entry, referenced, transitive, inside_library]
  - signal: env_var_dependency
    placements: [entry, referenced, transitive, inside_library, task_config]
  - signal: lax_sql_semantics
    placements: [entry, referenced, transitive]
  - signal: dbfs_root_path
    placements: [entry, referenced, transitive, inside_library, task_config, pipeline_config]
  - signal: local_filesystem_dependency
    placements: [entry, referenced, transitive, inside_library]
  - signal: foreach_batch_nested_logic
    placements: [entry, referenced, transitive]
  - signal: non_python_sql_language
    placements: [entry, referenced, transitive, task_config]
  - signal: maven_or_compute_scoped_library
    placements: [task_config]
  - signal: notebook_scoped_pip
    placements: [entry, referenced, transitive]
  - signal: existing_cluster_binding
    placements: [task_config]
  - signal: environment_version_pinned_old
    placements: [task_config]
  - signal: network_egress_dependency
    placements: [entry, referenced, transitive, inside_library]
  - signal: trigger_processing_time
    placements: [entry, referenced, transitive, task_config, pipeline_config]
  - signal: trigger_continuous
    placements: [entry, referenced, transitive, task_config, pipeline_config]
  - signal: checkpoint_not_uc_shaped
    placements: [entry, referenced, transitive, task_config]
  - signal: spark_conf_override
    placements: [entry, referenced, transitive, task_config]

# =====================================================================
# DISTRACTOR TEMPLATES — near-miss material for the grammar's
# plant_distractors rule. Each template is textually convincing but
# structurally inert; the oracle must never match it. These are what
# make PRECISION measurable: an analyzer that regexes raw text will
# fire on every one of these.
# =====================================================================
distractor_templates:
  - imitates: rdd_or_sparkcontext
    surface: line_comment
    text: "# legacy path used sc.parallelize + SparkContext broadcast; removed during Connect migration"
  - imitates: rdd_or_sparkcontext
    surface: variable_or_column_name
    text: "rdd_source_id = df.columns[0]"
  - imitates: rdd_or_sparkcontext
    surface: unreachable_code
    text: "if False:\n    legacy = spark.sparkContext.parallelize(rows)"
  - imitates: rdd_or_sparkcontext
    surface: string_literal
    text: "MIGRATION_NOTE = 'replaced .rdd.map() with DataFrame select'"
  - imitates: dbfs_root_path
    surface: markdown_cell
    text: "Data previously lived at dbfs:/mnt/legacy_orders before the UC migration."
  - imitates: dbfs_root_path
    surface: log_message
    text: "logger.info('source migrated from dbfs:/mnt/raw to UC volume')"
  - imitates: maven_or_compute_scoped_library
    surface: disabled_cell
    text: "# %pip install --index-url ... com.databricks:spark-xml_2.12:0.14.0  # removed, JVM-only"
  - imitates: global_temp_view
    surface: block_comment_docstring
    text: '"""Historically exposed via global_temp.session_cache; now a UC table."""'
  - imitates: env_var_dependency
    surface: line_comment
    text: "# threshold previously read from os.environ['DQ_FAIL_THRESHOLD']; now a job parameter"
  - imitates: trigger_processing_time
    surface: markdown_cell
    text: "Original design used Trigger.ProcessingTime('5 minutes'); cadence now comes from the job schedule."
  - imitates: non_python_sql_language
    surface: string_literal
    text: "PORTED_FROM = 'scala udf: com.acme.udf.NormalizeRegion'"
  - imitates: local_filesystem_dependency
    surface: line_comment
    text: "# staging used to write to /tmp/orders_stage before switching to a Volume"

# =====================================================================
# APPLY RULES — how serverless is overlaid onto an arbitrary bundle.
# Each rule: match (structural condition, may include knob values) →
# effect. Positive scenarios normalize the bundle to serverless-correct
# form; negative/edge scenarios deliberately inject serverless-relevant
# defects, always expressed via a signal id so validation can find them.
# Injection respects the grammar's signal_placement knob and this
# file's placement_eligibility table.
# =====================================================================
apply_rules:
  # ---- normalization (all scenario types) ----
  - id: set_compute_target
    match: { asset: any }
    effect:
      set_field: compute_target
      value_from: compute_targets

  - id: force_valid_streaming_trigger
    match: { workload_type_in: [streaming, pipeline], scenario_type: positive }
    effect:
      set_field: streaming_trigger
      value: available_now

  - id: default_storage_format
    match: { asset: any, field_absent: format }
    effect:
      set_field: format
      value: delta

  - id: migrate_dbfs_paths
    match: { signal_present: dbfs_root_path, scenario_type: positive }
    effect:
      rewrite_field: storage_path
      from_pattern: "^(dbfs:/|/dbfs/)"
      to_template: "/Volumes/{catalog}/{schema}/{volume}/"

  - id: normalize_dependencies
    match: { scenario_type: positive, asset_has: library }
    effect:
      set_library_type: whl_in_environment
      and_emit: environment_spec
    note: "Positive bundles declare deps in the serverless environment, never compute-scoped."

  - id: normalize_checkpoint
    match: { scenario_type: positive, workload_type_in: [streaming] }
    effect:
      rewrite_field: checkpoint_location
      to_template: "/Volumes/{catalog}/{schema}/{volume}/_checkpoints/{asset_id}"

  - id: clear_compute_binding
    match: { scenario_type: positive }
    effect:
      set_field: compute_binding
      value: serverless
      and_remove_keys: [existing_cluster_id, instance_pool_id, node_type, num_workers, autoscale]

  # ---- defect injection (negative scenarios) ----
  - id: inject_invalid_trigger
    match: { scenario_type: negative, workload_type_in: [streaming, pipeline] }
    effect:
      inject_signal: trigger_processing_time
      via: { set_field: streaming_trigger, value: processing_time }

  - id: inject_dbfs_path
    match: { scenario_type: negative, asset_has: storage_path }
    effect:
      inject_signal: dbfs_root_path
      via: { rewrite_field: storage_path, to_prefix: "dbfs:/tmp/" }

  - id: inject_rdd_usage
    match: { scenario_type: negative, workload_type_in: [batch, ml] }
    effect:
      inject_signal: rdd_or_sparkcontext
      via: { add_api_usage: "spark.sparkContext.parallelize" }
      at_placement_from_knob: signal_placement

  - id: inject_forbidden_library
    match: { scenario_type: negative, asset_has: library }
    effect:
      inject_signal: maven_or_compute_scoped_library
      via: { add_library: { type: maven } }
      at_placement: task_config

  - id: inject_env_var_in_library
    match: { scenario_type: negative, knob_equals: { signal_placement: inside_library } }
    effect:
      inject_signal: env_var_dependency
      via: { add_api_usage: "os.environ['DQ_FAIL_THRESHOLD']" }
      at_placement: inside_library
    note: "Exercises detection inside a built wheel rather than a notebook."

  - id: inject_local_fs_write
    match: { scenario_type: negative, workload_type_in: [batch] }
    effect:
      inject_signal: local_filesystem_dependency
      via: { add_api_usage: "open('/tmp/orders_stage.csv','w')" }

  - id: inject_bad_checkpoint
    match: { scenario_type: negative, workload_type_in: [streaming] }
    effect:
      inject_signal: checkpoint_not_uc_shaped
      via: { rewrite_field: checkpoint_location, to_prefix: "dbfs:/checkpoints/" }

  - id: inject_cluster_binding
    match: { scenario_type: negative, knob_equals: { signal_placement: task_config } }
    effect:
      inject_signal: existing_cluster_binding
      via: { set_field: compute_binding, value: existing_cluster }

  # ---- edge patterns (edge scenarios) ----
  - id: edge_empty_increment
    match: { scenario_type: edge, workload_type_in: [streaming, pipeline] }
    effect:
      set_field: streaming_trigger
      value: available_now
      and_set: { source_new_rows: 0 }

  - id: edge_row_bounds
    match: { scenario_type: edge, asset_has: row_count }
    effect:
      set_knob_to_bound: { knob: row_multiplier, bound: max }

  - id: edge_ansi_boundary
    match: { scenario_type: edge, column_types_include: [int, decimal] }
    effect:
      inject_signal: lax_sql_semantics
      via: { add_values: [int_max_overflow_candidate, non_numeric_cast_candidate] }

  - id: edge_nested_foreach_batch
    match: { scenario_type: edge, workload_type_in: [streaming] }
    effect:
      inject_signal: foreach_batch_nested_logic
      via: { add_api_usage: "def _batch(df, epoch): df.rdd.getNumPartitions()" }
    note: "Connect-illegal code hidden one level down inside foreachBatch."

  # ---- distractor planting (distractor scenarios) ----
  - id: plant_serverless_distractors
    match: { scenario_type_in: [distractor, mixed] }
    effect:
      plant_from: distractor_templates
      count_from_knob: distractor_count
      surfaces_from_knob: distractor_surface_mix
    note: "No real signal is injected for scenario_type=distractor; expected verdict is eligible."

# =====================================================================
# IMPACT RULES — powers "what happens if serverless is applied?".
# One entry per signal plus structure-only patterns.
# =====================================================================
impact_rules:
  - signal: rdd_or_sparkcontext
    consequence: blocked
    impact: "Code fails at import/attach time — SparkContext does not exist on Spark Connect."
    remediation: "Rewrite RDD logic to DataFrame/Spark SQL operations."
  - signal: non_python_sql_language
    consequence: blocked
    impact: "The task cannot be scheduled on serverless at all."
    remediation: "Port to Python/SQL or keep on classic compute."
  - signal: trigger_continuous
    consequence: blocked
    impact: "Stream fails to start; continuous processing mode is rejected."
    remediation: "Redesign around Trigger.AvailableNow with scheduled runs."
  - signal: distributed_training_api
    consequence: blocked
    impact: "Training job fails — no executor barrier mode or GPUs on serverless."
    remediation: "Single-node training on serverless, or managed/classic GPU compute."
  - signal: existing_cluster_binding
    consequence: blocked
    impact: "The task runs on the pinned cluster, not serverless; the serverless target is never applied."
    remediation: "Remove existing_cluster_id / instance_pool_id and sizing keys, then assign a serverless environment."
  - signal: trigger_processing_time
    consequence: requires_change
    impact: "Trigger is rejected; also latency semantics change from interval micro-batches to run-to-completion increments."
    remediation: "Switch to AvailableNow and drive cadence from the job schedule."
  - signal: dbfs_root_path
    consequence: requires_change
    impact: "Reads/writes to dbfs:/ fail with access errors."
    remediation: "Move data to Unity Catalog Volumes or managed tables and rewrite paths."
  - signal: maven_or_compute_scoped_library
    consequence: requires_change
    impact: "Libraries are silently absent or the job is rejected; imports fail at runtime."
    remediation: "Declare pip/wheel dependencies in the serverless environment; JVM-only libraries cannot be carried over."
  - signal: env_var_dependency
    consequence: requires_change
    impact: "os.environ lookups return nothing; behavior silently diverges."
    remediation: "Pass values as job/task parameters or read from secrets/UC."
  - signal: global_temp_view
    consequence: requires_change
    impact: "createGlobalTempView calls fail; downstream readers of global_temp.* break."
    remediation: "Use session temp views within one task, or persist to a UC table."
  - signal: local_filesystem_dependency
    consequence: requires_change
    impact: "Writes appear to succeed but vanish between runs and are invisible to other tasks."
    remediation: "Write to a UC Volume or a managed table instead of local disk."
  - signal: checkpoint_not_uc_shaped
    consequence: requires_change
    impact: "Stream fails to start, or silently restarts from scratch and reprocesses the source."
    remediation: "Point checkpointLocation at a UC Volume path and re-baseline the stream."
  - signal: network_egress_dependency
    consequence: requires_change
    impact: "Outbound calls are blocked or time out until serverless network policy allows the destination."
    remediation: "Register the destination under the workspace network policy / NCC and re-test credentials."
  - signal: environment_version_pinned_old
    consequence: requires_change
    impact: "APIs the code expects may be missing; failures surface only at execution."
    remediation: "Align the declared environment/client version with what the code requires."
  - signal: foreach_batch_nested_logic
    consequence: requires_change
    impact: "The stream starts, then fails inside the batch function where Connect-illegal calls live."
    remediation: "Audit the batch function body under the same rules as top-level code."
  - signal: spark_conf_override
    consequence: degraded
    impact: "Most confs are ignored or rejected; tuning assumptions (shuffle partitions, memory) no longer hold."
    remediation: "Drop sizing confs — serverless autoscales — and verify the few allow-listed session confs individually."
  - signal: notebook_scoped_pip
    consequence: degraded
    impact: "Every run re-resolves dependencies, adding startup time and version drift."
    remediation: "Move dependencies into the serverless environment specification."
  - signal: lax_sql_semantics
    consequence: behavior_change
    impact: "ANSI mode is on: overflows and invalid casts raise errors instead of returning NULL; some rows that flowed through before now fail the query."
    remediation: "Use try_cast/try_divide/try_add or clean inputs; do not disable ANSI globally."
  # structure-only patterns (no incompatibility signal, still worth stating)
  - pattern: { config_has_keys: [node_type, num_workers, autoscale, instance_pool] }
    consequence: safe
    impact: "Cluster sizing settings are ignored — serverless manages capacity, autoscaling, and Photon automatically."
    remediation: "Remove the settings for clarity; no functional change."
  - pattern: { workload_type_in: [batch, sql], no_eligibility_signals: true }
    consequence: safe
    impact: "Runs unchanged; typically faster startup (no cluster spin-up) and per-use billing."
    remediation: null

# =====================================================================
# PLATFORM-MANAGED CAPABILITIES — read by the grammar's
# platform_managed_capability_is_moot interaction rule. Any feature that
# recommends one of these against a serverless target is suppressed:
# the platform already handles it, so the recommendation is meaningless.
# =====================================================================
platform_managed_capabilities:
  - capability: photon
    fact_ref: managed_capacity
    note: "Photon is enabled and managed by the platform on serverless; there is no enablement switch to recommend."
  - capability: autoscaling
    fact_ref: managed_capacity
  - capability: capacity_sizing
    fact_ref: managed_capacity
  - capability: cluster_configuration
    fact_ref: managed_capacity

# =====================================================================
# INTERACTION DECLARATIONS — how this feature relates to others when a
# plan targets more than one. Resolved by the grammar's
# feature_interactions table; this file only declares its side.
# =====================================================================
interaction_declarations:
  - with: photon
    kind: suppresses
    when: { compute_binding_in: [serverless] }
    rationale: "Photon is platform-managed on serverless; a Photon-enablement recommendation is not_applicable."
  - with: liquid_clustering
    kind: independent
    rationale: "Clustering is a table-layout concern; serverless eligibility is a compute/runtime concern. Both verdicts stand."
  - with: any
    kind: precedes
    when: { self_verdict_in: [blocked] }
    rationale: "A workload that cannot run at all makes layout and performance advice premature."
  - with: any
    kind: requires
    when: { other_feature_targets_dimension: [compute_binding] }
    rationale: "Any recommendation that assumes serverless execution is only valid if the serverless verdict is eligible."

# =====================================================================
# VALIDATION RULES — the deterministic validator's checklist for
# "is this bundle serverless-correct?". Structural/graph validation is
# delegated to the grammar; these are serverless-specific.
# =====================================================================
validation_rules:
  - id: no_blocking_signals
    check:
      assert_absent_signals:
        - rdd_or_sparkcontext
        - non_python_sql_language
        - trigger_continuous
        - distributed_training_api
        - existing_cluster_binding
  - id: no_unresolved_review_signals
    check:
      assert_absent_signals:
        - dbfs_root_path
        - trigger_processing_time
        - maven_or_compute_scoped_library
        - env_var_dependency
        - global_temp_view
        - local_filesystem_dependency
        - checkpoint_not_uc_shaped
    note: "Applies to bundles claiming serverless-ready; a bundle routed to review_before_serverless is exempt by definition."
  - id: compute_target_in_vocabulary
    check:
      for_each: asset
      assert_field_in_vocabulary: { field: compute_target, vocabulary: compute_targets }
  - id: compute_target_matches_structure
    check:
      for_each: asset
      assert_field_resolves_via: { field: compute_target, table: compute_targets }
  - id: streaming_trigger_valid
    check:
      for_each: { asset_where: { workload_type_in: [streaming, pipeline] } }
      assert_field_in_vocabulary: { field: streaming_trigger, vocabulary: streaming_triggers_valid }
  - id: format_in_vocabulary
    check:
      for_each: { asset_where: { field_present: format } }
      assert_field_in_vocabulary: { field: format, vocabulary: formats }
  - id: paths_unity_catalog_shaped
    check:
      for_each: { field: storage_path }
      assert_pattern: "^(/Volumes/[^/]+/[^/]+/[^/]+/|[A-Za-z0-9_]+\\.[A-Za-z0-9_]+\\.[A-Za-z0-9_]+$|s3://|abfss://|gs://)"
  - id: checkpoint_unity_catalog_shaped
    check:
      for_each: { asset_where: { workload_type_in: [streaming] } }
      assert_pattern_on_field: { field: checkpoint_location, pattern: "^/Volumes/[^/]+/[^/]+/[^/]+/" }
  - id: dependencies_environment_declared
    check:
      when: { scenario_type: positive }
      for_each: { field: library }
      assert_field_in: { field: library_type, values: [none, whl_in_environment, pip_requirements, pypi_coordinate] }
  - id: negative_scenario_has_injected_signal
    check:
      when: { knob_equals: { scenario_type: negative } }
      assert_present_any_signal_from: eligibility_signals
    note: "A 'negative' bundle that contains no incompatibility signal is itself invalid."
  - id: distractor_scenario_stays_eligible
    check:
      when: { knob_equals: { scenario_type: distractor } }
      assert: "no signal from eligibility_signals matched; verdict resolves to eligible"
    note: "If a distractor bundle produces a verdict, either a template is wrong or the oracle is reading a non-executable surface."
  - id: placement_is_eligible
    check:
      for_each: injected_signal
      assert_placement_allowed_by: placement_eligibility
    note: "Injecting a library-dimension signal into notebook code is a plan error, not something to relocate silently."
  - id: proposed_signals_flagged
    check:
      for_each: matched_signal
      assert_status_recorded: true
    note: "Reports must distinguish verified signals from proposed ones pending SME sign-off."

# =====================================================================
# VOCABULARY — closed value sets this feature constrains or owns.
# Composition knobs and scenario types are owned by the grammar; this
# file only narrows grammar vocabularies where serverless is stricter.
# =====================================================================
vocabulary:
  compute_targets:
    - serverless_job
    - serverless_job_with_checks
    - serverless_pipeline
    - serverless_pipeline_with_checks
    - serverless_sql
    - review_before_serverless
  streaming_triggers_valid: [available_now]
  streaming_triggers_invalid: [processing_time, continuous]
  formats: [delta, parquet, json, csv]
  verdicts: [eligible, review_before_serverless, blocked, not_applicable]
  consequences: [blocked, requires_change, degraded, behavior_change, safe]
  languages_supported: [python, sql]
  udf_kinds_supported: [python_udf, pandas_udf, python_uc_udf, sql_udf, applyInPandas]
  library_types_supported: [none, whl_in_environment, whl_workspace_file, pip_requirements, pypi_coordinate]
constrains_grammar_vocabulary:
  languages: [python, sql]
  streaming_triggers: [available_now]
  compute_bindings: [serverless]
  note: >
    These are the values a POSITIVE serverless bundle may carry. Negative and edge
    scenarios deliberately violate them; the grammar's vocabulary_closed check still
    applies, but these narrower sets are what positive-scenario validation asserts.

# =====================================================================
# PLATFORM FACTS — grounding statements for Q&A activities. Reference
# knowledge, not validator input; cite by id when explaining.
# =====================================================================
platform_facts:
  - id: spark_connect_only
    fact: "Serverless executes exclusively over Spark Connect; there is no driver-attached SparkContext and no RDD API."
  - id: ansi_default_on
    fact: "ANSI SQL mode is enabled by default on serverless."
  - id: uc_volumes_for_files
    fact: "File access uses Unity Catalog Volumes; DBFS root and mounts are unavailable."
  - id: available_now_only
    fact: "Trigger.AvailableNow is the only supported Structured Streaming trigger; ProcessingTime and Continuous are rejected."
  - id: managed_capacity
    fact: "Capacity, autoscaling, and Photon are managed by the platform; there is no cluster sizing surface and billing is per-use."
  - id: environments_for_deps
    fact: "Python dependencies are declared via serverless environments (pip/wheel); no init scripts, Maven coordinates, or compute-scoped libraries."
  - id: ephemeral_local_disk
    fact: "Driver-local disk is ephemeral and not shared across tasks or retries; durable output belongs in UC Volumes or tables."
    status: proposed
  - id: governed_egress
    fact: "Outbound network access is governed by workspace network policy rather than cluster networking."
    status: proposed
```

---

## Guidance

*(LLM reasoning hints only. Nothing here is code-checked; nothing here may override the YAML.)*

**Mapping vague requests to activities.** "Can this run serverless?" / "is this ready?" →
evaluate `eligibility_signals`, report strongest verdict. "What breaks?" / "what changes?" /
"is it safe to switch?" → walk the matched signals through `impact_rules`, grouped by
consequence. "Make it serverless" / "convert this" → run `apply_rules` with
`scenario_type: positive`. "Check my bundle" / "did I do this right?" → run
`validation_rules`. "Generate a test case" → the grammar owns composition; this file supplies
which signal to inject and where it may live. Anything else: decompose the question into
which signals, targets, facts, or knobs it touches, and compose the answer from those —
never invent a constraint that isn't in the YAML, and never contradict one that is.

**Mapping vague phrasing to signals.** "Legacy Spark code" or "old-style Spark" usually
means `rdd_or_sparkcontext`. "Files on DBFS", "FileStore", "mounted storage" →
`dbfs_root_path`. "Runs every 5 minutes" on a stream → `trigger_processing_time` (but if
achieved via job schedule + AvailableNow, it's fine — check which). "Real-time" /
"sub-second" → likely `trigger_continuous`, a blocker worth confirming. "Custom JARs",
"cluster libraries", "init scripts" → `maven_or_compute_scoped_library`. "It reads config
from the cluster" → `env_var_dependency` or `spark_conf_override`. "It writes a temp file" →
`local_filesystem_dependency`. "It calls an external API" → `network_egress_dependency`.
"It's pinned to our shared cluster" → `existing_cluster_binding`.

**Where a signal can hide matters as much as which signal it is.** The same
`rdd_or_sparkcontext` defect at `entry` and at `transitive` are different test cases: the
first tests whether an analyzer reads code, the second whether it traverses references.
`inside_library` is the hardest — the defect is inside a built wheel and invisible to any
notebook scan. Consult `placement_eligibility` before choosing; a library-dimension signal
cannot live in notebook code, and asking for that combination is a plan error worth
surfacing rather than quietly relocating.

**Distractors are the other half of the test.** Every entry in `distractor_templates` is
text that *looks* like a violation but cannot execute — a commented-out `sc.parallelize`, a
column named `rdd_source_id`, a markdown note mentioning `dbfs:/`. A naive analyzer flags all
of them. When reporting bake-off results, always show precision alongside recall; an approach
that catches every real defect but also fires on every migration comment is not the better
approach, it is the noisier one.

**Defaults when the bundle is silent.** Missing `workload_type`: assume `batch` for
Python assets, `sql` for pure SQL. Missing `format`: `delta`. Missing trigger on a
streaming asset: `available_now`. Missing `compute_binding`: `unspecified` — do not assume
serverless, since that is the question being asked. Missing knobs: their declared defaults.
Missing paths: assume Unity Catalog tables (no path signal fires). State every assumption you
make; a silent default that turns out wrong is worse than one sentence of hedging.

**Phrasing impact explanations.** Lead with the verdict in one sentence, then group
findings by consequence in severity order (blocked → requires_change → degraded →
behavior_change → safe). For each, give the impact and remediation from `impact_rules` in
plain language and name the structural evidence ("a streaming asset using a
processing-time trigger"), never a guessed asset name. If nothing matched, say so
positively: the bundle is eligible, and note the `safe` impacts (faster startup, ignored
sizing settings) so the user knows what *will* change. Keep the ANSI-mode
`behavior_change` visible even in otherwise-clean reports — it's the one that bites
silently. Where a matched signal has `status: proposed`, say so — the finding is real but
the rule has not yet been SME-verified.

**Multi-feature reports.** When a plan targets serverless alongside another feature, never
emit a recommendation for anything in `platform_managed_capabilities` against a serverless
target — Photon enablement on serverless is the canonical example of advice that is
technically fluent and practically meaningless. If the serverless verdict is `blocked`, lead
with that and defer the other feature's advice rather than presenting them as peers.
