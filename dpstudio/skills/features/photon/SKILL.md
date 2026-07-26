---
name: photon
description: The authoritative rule and knowledge file for Databricks Photon, applied to generated data asset bundles. Consult this skill for ANY Photon activity on a workload, bundle, or config — assessing whether enabling Photon would benefit a workload, explaining the impact and cost trade-off of enabling it, applying Photon to a bundle, identifying operations that fall back to Spark, validating Photon correctness, or answering questions about Photon coverage (vectorized scans, joins and aggregations, UDF fallback, RDD incompatibility, DBU premium, serverless and SQL warehouse defaults). Use it whenever the user mentions Photon, vectorized execution, query acceleration, DBU cost of acceleration, or "should we turn Photon on" — even if they don't say "Databricks" explicitly.
---

# Databricks Photon — Rules & Knowledge

This file is the single authority for Photon behavior in this system. It is **not** a
workflow. It exposes Photon knowledge as reusable, composable rules so that *any* activity —
benefit assessment, impact and cost explanation, applying Photon to a bundle, validation, or
ad-hoc Q&A — can be answered by selecting and combining the rules below.

**A note on polarity.** Photon answers "will this help, and is it worth the DBU premium?" —
not "can this run?". Almost any workload *runs* with Photon enabled; the question is whether
enough of it executes in the vectorized engine to pay for the higher rate. Signals therefore
carry a `polarity`: `benefit` (structural evidence Photon accelerates this workload) or
`constraint` (evidence it falls back to Spark, or that the premium is not recoverable). A
verdict is only `recommended` when benefit signals are present and no constraint dominates.

**The most important rule in this file is negative.** On serverless compute and on Pro/
Serverless SQL warehouses, Photon is enabled and managed by the platform. There is no switch
to recommend. A "enable Photon" recommendation against those targets is fluent, confident,
and meaningless — the single most likely wrong answer this feature can produce. See
`suppressed_when` and `interaction_declarations`.

The file has two strictly separated zones: a machine-readable YAML zone that code reasons
against, and a guidance zone of prose hints that is never code-checked and may never
contradict the YAML.

**Verification status.** Signals carry `status: verified | proposed`. This file was authored
against documented Photon behavior but has not been SME-reviewed. Coverage of specific
operators changes across runtime versions, so operator-level signals are the ones most
likely to need correction. Thresholds marked `tunable` are judgement calls.

---

## Machine-readable zone

```yaml
version: "1.0"
composes_with:
  grammar: asset-bundle-generation
  grammar_version: ">=2.0"
  primary_dimensions: [api_usage, format, column_types, compute_binding, udf_kind, workload_type]

# Declared so the grammar's platform_managed_capability_is_moot rule can suppress this
# feature when another feature already provides the capability.
recommends_capability: photon

suppressed_when:
  - compute_binding_in: [serverless]
    by_feature: serverless
    reason: "Photon is enabled and managed by the platform on serverless; there is no enablement decision."
  - compute_binding_in: [sql_warehouse]
    warehouse_tier_in: [pro, serverless]
    reason: "Pro and Serverless SQL warehouses run Photon by default."

# =====================================================================
# SIGNALS — matchable structural conditions. `polarity` distinguishes
# benefits (Photon accelerates this) from constraints (falls back to
# Spark, or premium not recoverable).
# =====================================================================
signals:
  # ---------------- constraints ----------------
  - id: rdd_usage
    status: verified
    polarity: constraint
    dimension: api_usage
    match:
      any_pattern:
        - "SparkContext"
        - "\\.rdd\\b"
        - "\\bparallelize\\("
        - "mapPartitions|foreachPartition"
    why: >
      Photon accelerates the DataFrame/SQL execution path only. RDD operations execute
      in the JVM engine with no vectorization, so the DBU premium buys nothing.

  - id: python_udf_heavy
    status: verified
    polarity: constraint
    dimension: udf_kind
    match:
      udf_kind_in: [python_udf, applyInPandas]
      or_any_pattern:
        - "@udf|udf\\("
        - "applyInPandas|mapInPandas"
    why: >
      Python UDFs execute outside the vectorized engine; the query falls back to Spark
      for those stages and pays serialization cost across the boundary. A pipeline
      dominated by Python UDFs sees little Photon benefit.

  - id: scala_java_udf
    status: proposed
    polarity: constraint
    dimension: udf_kind
    match:
      udf_kind_in: [scala_udf, java_udf]
    why: >
      JVM UDFs are not vectorized. The surrounding plan may still benefit, but the UDF
      stages themselves fall back.

  - id: ml_training_workload
    status: proposed
    polarity: constraint
    dimension: workload_type
    match:
      workload_type_in: [ml]
      or_any_pattern:
        - "\\.fit\\(|TorchDistributor|HorovodRunner|sklearn|xgboost"
    why: >
      Model training is not a vectorized SQL workload. Photon does not accelerate the
      training loop; only any DataFrame preparation stages around it.

  - id: small_data_volume
    status: proposed
    polarity: constraint
    tunable: true
    dimension: table_physical
    match:
      any_of:
        - total_row_count_lt: 1000000
        - scanned_bytes_below_gb: 1
    why: >
      On small scans, engine startup and the DBU premium outweigh the vectorization
      benefit. Photon pays off on scan-heavy work, not on lookups.

  - id: unsupported_complex_types
    status: proposed
    polarity: constraint
    dimension: column_types
    match:
      column_types_include_any: [map, struct_nested_deep, udt, variant_legacy]
    why: >
      Some complex and nested types fall back to the Spark engine for parts of the plan.
      Coverage varies by runtime version and must be verified rather than assumed.

  - id: already_photon_enabled
    status: verified
    polarity: constraint
    dimension: runtime_config
    match:
      any_key: [photon_enabled, runtime_engine_photon]
    why: >
      Photon is already on for this workload; the remaining question is whether it is
      actually being used (fallback rate), not whether to enable it.

  - id: platform_managed_photon
    status: verified
    polarity: constraint
    dimension: compute_binding
    match:
      compute_binding_in: [serverless, sql_warehouse]
    why: >
      The compute target manages Photon itself. Any enablement recommendation is
      not_applicable here regardless of how beneficial the workload profile looks.

  # ---------------- benefits ----------------
  - id: large_delta_scan
    status: proposed
    polarity: benefit
    tunable: true
    dimension: table_physical
    match:
      all_of:
        - format_in: [delta, parquet]
        - any_of:
            - total_row_count_gt: 100000000
            - scanned_bytes_above_gb: 100
    why: >
      Large columnar scans are the canonical Photon case: vectorized reads and
      predicate evaluation dominate runtime.

  - id: wide_aggregation
    status: proposed
    polarity: benefit
    dimension: api_usage
    match:
      any_pattern:
        - "\\.groupBy\\(|GROUP BY"
        - "\\.agg\\(|SUM\\(|AVG\\(|COUNT\\(|APPROX_"
        - "\\.rollup\\(|\\.cube\\("
    why: >
      Aggregation is heavily vectorized in Photon; hash aggregation over wide inputs is
      one of the largest observed speedups.

  - id: large_join
    status: proposed
    polarity: benefit
    dimension: api_usage
    match:
      any_pattern:
        - "\\.join\\(|JOIN\\b"
        - "broadcast\\("
    why: >
      Vectorized hash joins and improved shuffle handling are a primary Photon benefit
      on join-heavy plans.

  - id: sql_only_workload
    status: proposed
    polarity: benefit
    dimension: language
    match:
      language_in: [sql]
      no_signal_present: [python_udf_heavy, rdd_usage]
    why: >
      Pure SQL with no UDF boundary runs entirely inside the vectorized engine — the
      highest-coverage case.

  - id: delta_write_heavy
    status: proposed
    polarity: benefit
    dimension: api_usage
    match:
      any_pattern:
        - "\\.write\\.|INSERT INTO|MERGE INTO|CREATE TABLE AS"
    why: >
      Photon accelerates Delta and Parquet writes as well as reads; write-heavy ETL
      benefits on both sides of the plan.

  - id: repeated_scheduled_workload
    status: proposed
    polarity: benefit
    dimension: job_trigger
    match:
      job_trigger_in: [schedule]
      run_frequency_per_day_gt: 4
    why: >
      Frequently repeated jobs amortize the evaluation effort and make a modest
      per-run improvement worth the premium.

# =====================================================================
# ELIGIBILITY — routes a workload to a verdict. Constraint signals can
# make Photon not_applicable or not worthwhile; benefit signals qualify
# it as recommended. Absence of constraints is NOT a recommendation.
# Strength order: not_applicable > review > recommended > neutral.
# =====================================================================
eligibility_signals:
  - signal: platform_managed_photon
    verdict: not_applicable
    reason: "The compute target manages Photon; there is no enablement action to take."
  - signal: already_photon_enabled
    verdict: already_optimal
    reason: "Photon is already enabled; assess fallback rate rather than enablement."
  - signal: rdd_usage
    verdict: not_recommended
    reason: "RDD stages bypass the vectorized engine entirely; the DBU premium buys no acceleration."
  - signal: ml_training_workload
    verdict: not_recommended
    reason: "Training loops are not vectorized SQL work; Photon accelerates only surrounding data preparation."
  - signal: small_data_volume
    verdict: not_recommended
    reason: "Scan volume is too small for vectorization to recover the DBU premium."
  - signal: python_udf_heavy
    verdict: review_before_photon
    reason: "Python UDF stages fall back to Spark; measure the fallback share before committing to the premium."
  - signal: scala_java_udf
    verdict: review_before_photon
    reason: "JVM UDF stages are not vectorized; surrounding plan may still benefit — verify by measurement."
  - signal: unsupported_complex_types
    verdict: review_before_photon
    reason: "Nested and complex type coverage varies by runtime; confirm the plan is not partially falling back."
  - signal: large_delta_scan
    verdict: recommended
    reason: "Large columnar scans are the primary Photon acceleration case."
  - signal: wide_aggregation
    verdict: recommended
    reason: "Vectorized hash aggregation is among the largest observed speedups."
  - signal: large_join
    verdict: recommended
    reason: "Join-heavy plans benefit from vectorized hash joins and improved shuffle handling."
  - signal: sql_only_workload
    verdict: recommended
    reason: "Pure SQL with no UDF boundary achieves the highest Photon coverage."
  - signal: delta_write_heavy
    verdict: recommended
    reason: "Photon accelerates Delta and Parquet writes as well as reads."
  - signal: repeated_scheduled_workload
    verdict: recommended
    reason: "Frequent repetition amortizes evaluation effort and compounds per-run savings."

verdict_resolution:
  rule: >
    platform_managed_photon always wins outright — it is a fact about the target, not a
    trade-off. Otherwise: if a not_recommended constraint dominates the plan, that verdict
    stands. If a review constraint is present alongside benefits, the verdict is
    review_before_photon with the benefits listed as motivation. Benefits with no
    constraints yield recommended. Neither present yields neutral — never recommend by
    default, and never present an estimated speedup as a measured one.

# =====================================================================
# PHOTON TARGETS — maps a workload (by structure, never by name) to the
# Photon configuration it should carry. First match wins.
# =====================================================================
photon_targets:
  - target: platform_managed
    applies_when:
      compute_binding_in: [serverless, sql_warehouse]
    note: "No configuration to emit; Photon is inherent to the target."
  - target: review_before_photon
    applies_when:
      workload_has_eligibility_verdict_in: [review_before_photon]
  - target: photon_enabled_job_cluster
    applies_when:
      compute_binding_in: [new_job_cluster]
      workload_has_eligibility_verdict_in: [recommended]
  - target: photon_not_recommended
    applies_when:
      workload_has_eligibility_verdict_in: [not_recommended]
  - target: no_change
    applies_when:
      workload_has_eligibility_verdict_in: [already_optimal, neutral]

# =====================================================================
# SIGNAL PLACEMENT ELIGIBILITY — Photon signals live in code (operators,
# UDFs) and in compute configuration. Table properties carry the volume
# signals.
# =====================================================================
placement_eligibility:
  - signal: rdd_usage
    placements: [entry, referenced, transitive, inside_library]
  - signal: python_udf_heavy
    placements: [entry, referenced, transitive, inside_library]
  - signal: scala_java_udf
    placements: [entry, referenced, transitive, inside_library]
  - signal: wide_aggregation
    placements: [entry, referenced, transitive, inside_library]
  - signal: large_join
    placements: [entry, referenced, transitive, inside_library]
  - signal: delta_write_heavy
    placements: [entry, referenced, transitive, inside_library]
  - signal: ml_training_workload
    placements: [entry, referenced, transitive]
  - signal: sql_only_workload
    placements: [entry, referenced, transitive]
  - signal: unsupported_complex_types
    placements: [table_property]
  - signal: large_delta_scan
    placements: [table_property]
  - signal: small_data_volume
    placements: [table_property]
  - signal: already_photon_enabled
    placements: [task_config]
  - signal: platform_managed_photon
    placements: [task_config]
  - signal: repeated_scheduled_workload
    placements: [task_config]

# =====================================================================
# DISTRACTOR TEMPLATES — near-misses for the grammar's plant_distractors
# rule. Photon distractors mostly imitate UDF and RDD usage.
# =====================================================================
distractor_templates:
  - imitates: python_udf_heavy
    surface: line_comment
    text: "# normalize_region used to be a @udf; rewritten as a native expression for Photon"
  - imitates: python_udf_heavy
    surface: disabled_cell
    text: "# @udf('string')\n# def normalize_region(r): return r.upper()"
  - imitates: rdd_usage
    surface: markdown_cell
    text: "The original implementation used .rdd.mapPartitions before the DataFrame rewrite."
  - imitates: rdd_usage
    surface: variable_or_column_name
    text: "rdd_migration_status = 'complete'"
  - imitates: scala_java_udf
    surface: string_literal
    text: "LEGACY_UDF_CLASS = 'com.acme.udf.NormalizeRegion'"
  - imitates: ml_training_workload
    surface: block_comment_docstring
    text: '"""Feature prep only — model .fit() runs in a separate training job."""'
  - imitates: already_photon_enabled
    surface: line_comment
    text: "# photon_enabled was set here before we moved to the shared cluster policy"
  - imitates: wide_aggregation
    surface: log_message
    text: "logger.info('skipping groupBy rollup; handled downstream')"

# =====================================================================
# APPLY RULES — how Photon is overlaid onto an arbitrary bundle.
# =====================================================================
apply_rules:
  - id: set_photon_target
    match: { asset: any }
    effect:
      set_field: photon_target
      value_from: photon_targets

  - id: skip_when_platform_managed
    match: { compute_binding_in: [serverless, sql_warehouse] }
    effect:
      set_field: photon_recommendation
      value: not_applicable
      and_suppress_output: true
    note: "Emit nothing. A recommendation here is the canonical wrong answer."

  - id: enable_photon_on_job_cluster
    match: { scenario_type: positive, compute_binding_in: [new_job_cluster] }
    effect:
      set_runtime_config:
        runtime_engine: PHOTON

  - id: normalize_udfs_for_coverage
    match: { scenario_type: positive, signal_present: python_udf_heavy }
    effect:
      rewrite_api_usage:
        from: python_udf
        to: native_sql_expression
    note: "Positive scenarios express logic natively so the plan stays inside the vectorized engine."

  # ---- defect injection (negative) ----
  - id: inject_python_udf
    match: { scenario_type: negative, workload_type_in: [batch, sql] }
    effect:
      inject_signal: python_udf_heavy
      via: { add_api_usage: "@udf('string')\ndef norm(r): return r.upper()" }
      at_placement_from_knob: signal_placement

  - id: inject_rdd_stage
    match: { scenario_type: negative, workload_type_in: [batch] }
    effect:
      inject_signal: rdd_usage
      via: { add_api_usage: "df.rdd.mapPartitions(f)" }
      at_placement_from_knob: signal_placement

  - id: inject_small_volume
    match: { scenario_type: negative, asset_has: row_count }
    effect:
      inject_signal: small_data_volume
      via: { set_field: row_count, value: 5000 }

  - id: inject_platform_managed_trap
    match: { scenario_type: negative }
    effect:
      inject_signal: platform_managed_photon
      via: { set_field: compute_binding, value: serverless }
      at_placement: task_config
    note: >
      The trap case: a workload whose profile screams "enable Photon" running on a target
      that already manages it. Correct output is not_applicable, not a recommendation.

  # ---- edge patterns ----
  - id: edge_mixed_udf_and_aggregation
    match: { scenario_type: edge, workload_type_in: [batch] }
    effect:
      inject_signal: python_udf_heavy
      and_inject_signal: wide_aggregation
    note: "Benefit and constraint in one plan — partial coverage, the case that needs measurement."

  - id: edge_volume_boundary
    match: { scenario_type: edge, asset_has: row_count }
    effect:
      set_field: row_count
      value: 1000000
    note: "Exactly at the small-volume threshold."

  - id: plant_photon_distractors
    match: { scenario_type_in: [distractor, mixed] }
    effect:
      plant_from: distractor_templates
      count_from_knob: distractor_count
      surfaces_from_knob: distractor_surface_mix

# =====================================================================
# IMPACT RULES — powers "what happens if Photon is enabled?".
# =====================================================================
impact_rules:
  - signal: platform_managed_photon
    consequence: not_applicable
    impact: "No change is possible or needed; the platform already runs Photon."
    remediation: null
  - signal: already_photon_enabled
    consequence: safe
    impact: "No enablement change; observed benefit depends on how much of the plan actually vectorizes."
    remediation: "Inspect the query profile for fallback stages rather than re-enabling."
  - signal: rdd_usage
    consequence: degraded
    impact: "RDD stages run in the JVM engine; the workload pays the DBU premium for stages Photon cannot touch."
    remediation: "Rewrite RDD logic to DataFrame operations, then re-evaluate."
  - signal: python_udf_heavy
    consequence: degraded
    impact: "Plan alternates between vectorized and Spark execution with serialization cost at each boundary."
    remediation: "Replace UDFs with native SQL expressions or built-in functions where possible."
  - signal: scala_java_udf
    consequence: degraded
    impact: "JVM UDF stages are not vectorized, though surrounding scans and joins may still be."
    remediation: "Express the logic natively, or accept partial coverage after measuring."
  - signal: ml_training_workload
    consequence: degraded
    impact: "The training loop is unaffected; only data preparation stages benefit."
    remediation: "Enable Photon on the preparation job, not the training job."
  - signal: small_data_volume
    consequence: degraded
    impact: "Runtime improvement is small in absolute terms while the DBU rate is higher; net cost usually increases."
    remediation: "Leave Photon off for small workloads; revisit if volume grows."
  - signal: unsupported_complex_types
    consequence: behavior_change
    impact: "Parts of the plan silently fall back; measured benefit is lower than the workload profile suggests."
    remediation: "Check the query profile for fallback stages before committing."
  - signal: large_delta_scan
    consequence: benefit
    impact: "Vectorized columnar reads and predicate evaluation dominate; this is the strongest Photon case."
    remediation: "Enable and measure wall-clock against DBU rate to confirm net saving."
  - signal: wide_aggregation
    consequence: benefit
    impact: "Hash aggregation is heavily vectorized; among the largest observed speedups."
    remediation: "Enable and measure."
  - signal: large_join
    consequence: benefit
    impact: "Vectorized hash joins and improved shuffle handling reduce join-stage time."
    remediation: "Enable and measure."
  - signal: sql_only_workload
    consequence: benefit
    impact: "The entire plan stays inside the vectorized engine; highest achievable coverage."
    remediation: "Enable."
  - signal: delta_write_heavy
    consequence: benefit
    impact: "Write path is accelerated alongside reads, benefiting ETL on both sides of the plan."
    remediation: "Enable and measure."
  - signal: repeated_scheduled_workload
    consequence: benefit
    impact: "A modest per-run improvement compounds across frequent runs."
    remediation: "Enable and track cumulative DBU against wall-clock."

# =====================================================================
# INTERACTION DECLARATIONS — how this feature relates to others.
# =====================================================================
interaction_declarations:
  - with: serverless
    kind: suppressed_by
    when: { compute_binding_in: [serverless] }
    rationale: >
      Serverless declares photon in its platform_managed_capabilities. Any Photon
      recommendation against a serverless target resolves to not_applicable.
  - with: serverless
    kind: precedes_me
    when: { serverless_verdict_in: [blocked] }
    rationale: "A workload that cannot run at all should not receive acceleration advice."
  - with: liquid_clustering
    kind: independent
    rationale: >
      Clustering reduces bytes scanned; Photon accelerates the scan. They compound and
      should be reported together when both are recommended.

# =====================================================================
# VALIDATION RULES — deterministic checks for Photon correctness.
# =====================================================================
validation_rules:
  - id: no_recommendation_on_managed_target
    check:
      for_each: { asset_where: { compute_binding_in: [serverless, sql_warehouse] } }
      assert_field_equals: { field: photon_recommendation, value: not_applicable }
    note: "The highest-value check in this file — guards the canonical wrong answer."
  - id: photon_target_in_vocabulary
    check:
      for_each: asset
      assert_field_in_vocabulary: { field: photon_target, vocabulary: photon_targets }
  - id: recommendation_requires_benefit
    check:
      assert: "verdict=recommended only when at least one benefit-polarity signal matched"
  - id: no_speedup_claim_without_measurement
    check:
      assert: "no numeric speedup or cost saving is asserted unless sourced from a measured run"
    note: "Photon benefit is workload-specific; estimated figures must not be presented as measured."
  - id: negative_scenario_has_injected_signal
    check:
      when: { knob_equals: { scenario_type: negative } }
      assert_present_any_signal_from: eligibility_signals
  - id: distractor_scenario_stays_neutral
    check:
      when: { knob_equals: { scenario_type: distractor } }
      assert: "no constraint signal matched from a non-executable surface"
  - id: placement_is_eligible
    check:
      for_each: injected_signal
      assert_placement_allowed_by: placement_eligibility
  - id: proposed_signals_flagged
    check:
      for_each: matched_signal
      assert_status_recorded: true

# =====================================================================
# VOCABULARY — closed value sets this feature owns or constrains.
# =====================================================================
vocabulary:
  photon_targets:
    - platform_managed
    - photon_enabled_job_cluster
    - photon_not_recommended
    - review_before_photon
    - no_change
  verdicts: [recommended, review_before_photon, not_recommended, already_optimal, not_applicable, neutral]
  verdict_strength_order: [neutral, already_optimal, recommended, review_before_photon, not_recommended, not_applicable]
  consequences: [not_applicable, degraded, behavior_change, benefit, safe]
  polarities: [constraint, benefit]
  accelerated_operations: [scan, filter, project, hash_aggregate, hash_join, sort, delta_write, parquet_write]
  fallback_operations: [python_udf, scala_udf, java_udf, rdd_operation, some_complex_types]

# =====================================================================
# PLATFORM FACTS — grounding statements for Q&A.
# =====================================================================
platform_facts:
  - id: vectorized_engine
    fact: "Photon is a native vectorized query engine that accelerates the DataFrame and SQL execution path."
  - id: managed_on_serverless
    fact: "Photon is enabled and managed by the platform on serverless compute; there is no enablement setting to configure."
  - id: default_on_sql_warehouses
    fact: "Pro and Serverless SQL warehouses run Photon by default."
    status: proposed
  - id: partial_coverage
    fact: "Unsupported operations fall back to the Spark engine within the same query; coverage is per-operator, not per-query."
  - id: dbu_premium
    fact: "Photon-enabled compute is billed at a higher DBU rate; net cost depends on whether the runtime reduction exceeds the rate increase."
    status: proposed
  - id: no_code_change
    fact: "Enabling Photon requires no code change; the same SQL and DataFrame code runs on either engine."
```

---

## Guidance

*(LLM reasoning hints only. Nothing here is code-checked; nothing here may override the YAML.)*

**Check the compute target before anything else.** If `compute_binding` is serverless, or a
Pro/Serverless SQL warehouse, the answer is `not_applicable` and the analysis stops there —
no matter how strongly the workload profile suggests acceleration. Producing a confident
"enable Photon for an estimated 2-3x speedup" against a target that already runs Photon is
the single most damaging output this feature can produce, because it is fluent, specific, and
wrong in a way the reader cannot detect. The `no_recommendation_on_managed_target` validation
rule exists solely for this.

**Mapping vague requests to activities.** "Should we turn Photon on?" → check the target,
then evaluate `eligibility_signals` with polarity. "Why isn't Photon helping?" → look for
constraint signals, especially `python_udf_heavy` and `rdd_usage`, and explain per-operator
fallback. "Is it worth the cost?" → `impact_rules` plus the honest answer that net saving
depends on measured wall-clock against the DBU rate. "Generate a Photon test case" → the
grammar owns composition; this file supplies which signal and where.

**Coverage is per-operator, not per-query.** The most common misunderstanding is that Photon
is on or off for a workload. A single query can have vectorized scans, a Spark-engine UDF
stage, and a vectorized aggregation. When explaining a disappointing result, describe which
stages fell back rather than saying Photon "didn't work".

**Never state a speedup you did not measure.** Benefit is entirely workload-shaped: a wide
aggregation over a hundred million rows and a UDF-heavy pipeline over the same data land in
completely different places. Give the mechanism and the direction, name the structural
evidence, and say what to measure. A specific multiplier quoted from general knowledge will
be wrong often enough to cost credibility, and `no_speedup_claim_without_measurement` treats
it as a validation failure.

**Mapping vague phrasing to signals.** "Lots of custom functions" → `python_udf_heavy`.
"Legacy Spark code" → `rdd_usage`. "Big scans" / "full table reads" → `large_delta_scan`.
"Dashboards run slowly" → likely SQL, check `sql_only_workload` and `wide_aggregation`.
"Training job is slow" → `ml_training_workload`, and Photon is not the answer. "It's already
on the SQL warehouse" → `platform_managed_photon`.

**Multi-feature reports.** Photon and Liquid Clustering compound — clustering cuts bytes
scanned, Photon accelerates the scan that remains — so present them together when both are
recommended. If serverless is in scope, defer to it entirely: either Photon is
platform-managed there, or the serverless verdict is `blocked` and acceleration advice is
premature.
