---
name: liquid-clustering
description: The authoritative rule and knowledge file for Databricks Liquid Clustering, applied to generated data asset bundles. Consult this skill for ANY liquid-clustering activity on a table, bundle, or config — assessing whether a table is a good clustering candidate, explaining the impact of adopting CLUSTER BY, applying clustering characteristics to a bundle, migrating from partitioning or ZORDER, validating clustering correctness, or answering questions about Liquid Clustering constraints (Delta requirement, key limits, partition and ZORDER incompatibility, OPTIMIZE and predictive optimization, row tracking, CLUSTER BY AUTO). Use it whenever the user mentions liquid clustering, CLUSTER BY, clustering keys, ZORDER migration, over-partitioned tables, small-file problems, or table layout optimization — even if they don't say "Databricks" explicitly.
---

# Databricks Liquid Clustering — Rules & Knowledge

This file is the single authority for Liquid Clustering behavior in this system. It is
**not** a workflow. It exposes clustering knowledge as reusable, composable rules so that
*any* activity — candidacy assessment, impact explanation, applying clustering to a bundle,
validation, or ad-hoc Q&A — can be answered by selecting and combining the rules below.

**A note on polarity.** Serverless answers "can this run?" — a compatibility question where
every signal is a constraint. Liquid Clustering answers "will this help?", so signals carry
a `polarity`: `constraint` (something that blocks or complicates adoption) or `benefit`
(structural evidence that clustering would improve this table). A verdict is only
`recommended` when benefit signals are present and no constraint blocks it. This distinction
is what stops the system recommending clustering on tables that gain nothing from it.

The file has two strictly separated zones:

1. **Machine-readable zone** — structured YAML. Code validates and reasons against this.
2. **Guidance zone** — prose hints for LLM reasoning only. Never code-checked.

Invariants: config-agnostic matching (structural dimensions only, never a named table);
one definition of each signal, referenced everywhere by id; feature scope only — plan
schema, knobs, and materialization belong to the `asset-bundle-generation` grammar.

**Verification status.** Signals carry `status: verified | proposed`. This file was authored
against documented Liquid Clustering behavior but has not been SME-reviewed; treat every
`proposed` entry as live for generation and flag it in client-facing reports until signed
off. Thresholds marked `tunable` are judgement calls, not platform limits.

---

## Machine-readable zone

```yaml
version: "1.0"
composes_with:
  grammar: asset-bundle-generation
  grammar_version: ">=2.0"
  primary_dimensions: [table_physical, column_types, format, storage_path]

# =====================================================================
# SIGNALS — matchable structural conditions. `polarity` distinguishes
# constraints (block/complicate adoption) from benefits (evidence that
# clustering helps). All other sections reference these by id.
# =====================================================================
signals:
  # ---------------- constraints ----------------
  - id: non_delta_format
    status: verified
    polarity: constraint
    dimension: format
    match:
      format_not_in: [delta]
    why: >
      Liquid Clustering is a Delta Lake feature. Parquet, CSV, JSON, Avro, ORC and
      Iceberg tables cannot declare CLUSTER BY without first converting to Delta.

  - id: partition_and_cluster_conflict
    status: verified
    polarity: constraint
    dimension: table_physical
    match:
      all_keys_present: [partition_by, cluster_by]
    why: >
      A table cannot be both partitioned and liquid clustered. Clustering replaces
      partitioning; declaring both is invalid and must be resolved before adoption.

  - id: zorder_present
    status: verified
    polarity: constraint
    dimension: table_physical
    match:
      any_key: [zorder_by]
    why: >
      ZORDER and Liquid Clustering are mutually exclusive strategies. Clustering
      supersedes ZORDER, but the existing ZORDER declaration must be removed rather
      than layered, and previously z-ordered data needs a full reclustering pass.

  - id: clustering_key_count_exceeded
    status: proposed
    polarity: constraint
    dimension: table_physical
    match:
      key_count_gt: { field: cluster_by, max: 4 }
    why: >
      Liquid Clustering supports a bounded number of clustering keys (4). Additional
      keys are rejected, and in practice the last keys contribute little — ordering
      matters more than breadth.

  - id: bucketed_table
    status: proposed
    polarity: constraint
    dimension: table_physical
    match:
      any_key: [bucket_by, num_buckets]
    why: >
      Bucketing is incompatible with Liquid Clustering. A bucketed table must be
      rewritten before clustering can be applied.

  - id: no_maintenance_path
    status: proposed
    polarity: constraint
    dimension: table_physical
    match:
      all_of:
        - any_key: [cluster_by]
        - key_absent: [predictive_optimization, scheduled_optimize]
    why: >
      Declaring CLUSTER BY does not itself cluster existing data. Without OPTIMIZE —
      scheduled or via predictive optimization — the table accumulates unclustered
      files and the declared layout never materializes.

  - id: clustering_on_low_cardinality_only
    status: proposed
    polarity: constraint
    tunable: true
    dimension: column_types
    match:
      cluster_keys_all_cardinality_below: 50
    why: >
      Clustering keys with very few distinct values behave like coarse partitions and
      provide little file-skipping benefit. High-cardinality predicates are the case
      clustering is designed for. Threshold is a judgement call, not a platform limit.

  # ---------------- benefits ----------------
  - id: over_partitioned
    status: proposed
    polarity: benefit
    tunable: true
    dimension: table_physical
    match:
      any_of:
        - partition_count_gt: 1000
        - avg_partition_row_count_lt: 100000
    why: >
      Many small partitions cause metadata overhead and small files. Liquid Clustering
      removes the fixed partition boundary and adapts to actual data distribution.

  - id: small_file_problem
    status: proposed
    polarity: benefit
    tunable: true
    dimension: table_physical
    match:
      any_of:
        - small_file_count_gt: 1000
        - avg_file_size_below_mb: 32
    why: >
      Clustering with regular OPTIMIZE consolidates small files, which is the dominant
      cost in scan-heavy workloads on fragmented tables.

  - id: high_cardinality_filter_column
    status: proposed
    polarity: benefit
    dimension: column_types
    match:
      any_of:
        - has_filtered_column_with_cardinality_gt: 10000
        - has_column_role_in: [event_id, user_id, order_id, device_id, session_id]
    why: >
      High-cardinality columns used in query predicates are the ideal clustering keys;
      they were poor partition keys precisely because of that cardinality.

  - id: skewed_partition_distribution
    status: proposed
    polarity: benefit
    tunable: true
    dimension: table_physical
    match:
      skew_factor_gt: 10
    why: >
      Skewed partitions produce uneven file sizes and straggler tasks. Clustering
      distributes data by value proximity rather than by a fixed key boundary.

  - id: evolving_query_pattern
    status: proposed
    polarity: benefit
    dimension: table_physical
    match:
      any_key: [query_pattern_changed, filter_columns_changed]
    why: >
      Partition layout is fixed at write time; clustering keys can be changed with
      ALTER TABLE and applied incrementally, which suits tables whose access patterns
      shift over time.

  - id: cdc_or_merge_heavy
    status: proposed
    polarity: benefit
    dimension: table_physical
    match:
      any_of:
        - any_key: [change_data_feed, apply_changes]
        - write_pattern_in: [merge, upsert, scd2]
    why: >
      MERGE-heavy tables benefit from clustering on the merge predicate columns, which
      reduces the file rewrite footprint of each operation.

  # ---------------- neutral / state ----------------
  - id: already_clustered
    status: verified
    polarity: constraint
    dimension: table_physical
    match:
      any_key: [cluster_by, liquid_clustering_enabled]
    why: >
      The table already declares Liquid Clustering. The remaining question is whether
      the keys are the right ones and whether maintenance is running, not whether to
      adopt it.

  - id: no_filter_predicates
    status: proposed
    polarity: constraint
    dimension: table_physical
    match:
      key_absent: [filter_columns, query_pattern]
    why: >
      A table that is always read in full gains nothing from data skipping. Clustering
      adds maintenance cost with no read benefit.

# =====================================================================
# ELIGIBILITY — routes a table to a verdict. Constraint signals route to
# blocked/review; benefit signals qualify a table as recommended. A table
# with no benefit signals and no constraints is not_applicable, NOT
# recommended: absence of a problem is not evidence of an opportunity.
# Strength order: blocked > review_before_clustering > recommended >
# already_optimal > not_applicable.
# =====================================================================
eligibility_signals:
  - signal: non_delta_format
    verdict: blocked
    reason: "Liquid Clustering requires Delta; convert the table format before clustering can be considered."
  - signal: bucketed_table
    verdict: blocked
    reason: "Bucketing is incompatible with clustering; the table must be rewritten without buckets."
  - signal: partition_and_cluster_conflict
    verdict: blocked
    reason: "Partitioning and clustering are mutually exclusive; remove the partition specification first."
  - signal: clustering_key_count_exceeded
    verdict: blocked
    reason: "More clustering keys declared than supported; reduce to the four most selective predicates."
  - signal: zorder_present
    verdict: review_before_clustering
    reason: "ZORDER must be removed and the table fully reclustered; plan for a one-time OPTIMIZE cost."
  - signal: no_maintenance_path
    verdict: review_before_clustering
    reason: "CLUSTER BY is declared without OPTIMIZE or predictive optimization; the layout will not materialize."
  - signal: clustering_on_low_cardinality_only
    verdict: review_before_clustering
    reason: "Declared keys are low-cardinality; confirm they match real query predicates before proceeding."
  - signal: already_clustered
    verdict: already_optimal
    reason: "Table already uses Liquid Clustering; review key selection and maintenance rather than adoption."
  - signal: no_filter_predicates
    verdict: not_applicable
    reason: "Table is read in full; data skipping provides no benefit and maintenance cost is unjustified."
  # benefit signals qualify for recommendation
  - signal: over_partitioned
    verdict: recommended
    reason: "Partition count and size distribution indicate metadata and small-file overhead clustering resolves."
  - signal: small_file_problem
    verdict: recommended
    reason: "File fragmentation is the dominant scan cost; clustering with regular OPTIMIZE consolidates it."
  - signal: high_cardinality_filter_column
    verdict: recommended
    reason: "High-cardinality predicate columns are ideal clustering keys and poor partition keys."
  - signal: skewed_partition_distribution
    verdict: recommended
    reason: "Partition skew produces stragglers; clustering distributes by value proximity instead."
  - signal: evolving_query_pattern
    verdict: recommended
    reason: "Clustering keys can be altered incrementally as access patterns change; partitioning cannot."
  - signal: cdc_or_merge_heavy
    verdict: recommended
    reason: "Clustering on merge predicates reduces file rewrite footprint for MERGE-heavy tables."

verdict_resolution:
  rule: >
    Evaluate constraints first. Any blocked constraint wins outright. If a review
    constraint is present alongside benefit signals, the verdict is
    review_before_clustering with the benefits listed as motivation. If benefits are
    present with no constraints, the verdict is recommended. If neither benefits nor
    constraints match, the verdict is not_applicable — never recommend by default.

# =====================================================================
# CLUSTERING TARGETS — maps a table (by structure, never by name) to the
# layout it should carry. Evaluate in order; first match wins.
# =====================================================================
clustering_targets:
  - target: review_before_clustering
    applies_when:
      table_has_eligibility_verdict_in: [blocked, review_before_clustering]
    note: "Overrides concrete targets until the flagged conditions are resolved."
  - target: cluster_by_auto
    applies_when:
      any_key: [query_pattern_changed]
      benefit_signal_present: evolving_query_pattern
    note: "Automatic key selection suits tables whose predicates are not stable."
  - target: cluster_by_merge_keys
    applies_when:
      benefit_signal_present: cdc_or_merge_heavy
  - target: cluster_by_filter_keys
    applies_when:
      benefit_signal_present: high_cardinality_filter_column
  - target: cluster_by_temporal_plus_dimension
    applies_when:
      has_column_type_in: [timestamp, date]
      benefit_signal_present: over_partitioned
    note: "Common migration shape: a date partition becomes the first clustering key."
  - target: no_clustering
    applies_when:
      table_has_eligibility_verdict_in: [not_applicable, already_optimal]

# =====================================================================
# SIGNAL PLACEMENT ELIGIBILITY — clustering signals live in table
# metadata and DDL, not in notebook code. Most placements are therefore
# invalid; the grammar's place_signal rule consults this before choosing.
# =====================================================================
placement_eligibility:
  - signal: non_delta_format
    placements: [table_property, task_config, entry, referenced, transitive]
  - signal: partition_and_cluster_conflict
    placements: [table_property]
  - signal: zorder_present
    placements: [table_property, entry, referenced, transitive]
  - signal: clustering_key_count_exceeded
    placements: [table_property]
  - signal: bucketed_table
    placements: [table_property]
  - signal: no_maintenance_path
    placements: [table_property, task_config]
  - signal: clustering_on_low_cardinality_only
    placements: [table_property]
  - signal: already_clustered
    placements: [table_property]
  - signal: over_partitioned
    placements: [table_property]
  - signal: small_file_problem
    placements: [table_property]
  - signal: high_cardinality_filter_column
    placements: [table_property]
  - signal: skewed_partition_distribution
    placements: [table_property]
  - signal: evolving_query_pattern
    placements: [table_property]
  - signal: cdc_or_merge_heavy
    placements: [table_property, pipeline_config]
  - signal: no_filter_predicates
    placements: [table_property]

# =====================================================================
# DISTRACTOR TEMPLATES — near-misses for the grammar's plant_distractors
# rule. Clustering distractors mostly live in DDL comments and in
# properties that LOOK like clustering but are not.
# =====================================================================
distractor_templates:
  - imitates: zorder_present
    surface: line_comment
    text: "-- previously maintained with ZORDER BY (order_ts); replaced by clustering"
  - imitates: zorder_present
    surface: markdown_cell
    text: "Legacy layout used OPTIMIZE ... ZORDER BY (region) before the migration."
  - imitates: partition_and_cluster_conflict
    surface: disabled_cell
    text: "-- PARTITIONED BY (order_date)  -- removed when we adopted CLUSTER BY"
  - imitates: already_clustered
    surface: string_literal
    text: "MIGRATION_TARGET = 'CLUSTER BY (order_ts, region)'"
  - imitates: bucketed_table
    surface: block_comment_docstring
    text: '"""Source system exports are bucketed; we un-bucket on ingest."""'
  - imitates: non_delta_format
    surface: log_message
    text: "logger.info('converted source parquet to delta before clustering')"
  - imitates: high_cardinality_filter_column
    surface: variable_or_column_name
    text: "cluster_by_candidate_note = 'user_id considered but rejected'"
  - imitates: no_maintenance_path
    surface: line_comment
    text: "# OPTIMIZE is handled by predictive optimization, not this notebook"

# =====================================================================
# APPLY RULES — how clustering is overlaid onto an arbitrary bundle.
# Positive scenarios produce correctly clustered tables; negative and
# edge scenarios inject clustering-relevant defects via signal ids.
# =====================================================================
apply_rules:
  # ---- normalization (positive) ----
  - id: set_clustering_target
    match: { asset: any }
    effect:
      set_field: clustering_target
      value_from: clustering_targets

  - id: force_delta_format
    match: { scenario_type: positive, asset_has: table_physical }
    effect:
      set_field: format
      value: delta

  - id: apply_cluster_keys
    match: { scenario_type: positive, clustering_target_in: [cluster_by_filter_keys, cluster_by_merge_keys, cluster_by_temporal_plus_dimension] }
    effect:
      set_table_physical:
        cluster_by: from_target_resolution
        liquid_clustering_enabled: true
      and_remove_keys: [partition_by, zorder_by, bucket_by, num_buckets]

  - id: enable_maintenance
    match: { scenario_type: positive, table_physical_has: cluster_by }
    effect:
      set_table_physical:
        predictive_optimization: true
    note: "A positive bundle must have a path by which the declared layout actually materializes."

  - id: enable_row_tracking
    match: { scenario_type: positive, table_physical_has: cluster_by }
    effect:
      set_table_physical:
        row_tracking: true
        deletion_vectors: true

  # ---- defect injection (negative) ----
  - id: inject_partition_conflict
    match: { scenario_type: negative, table_physical_has: cluster_by }
    effect:
      inject_signal: partition_and_cluster_conflict
      via: { set_table_physical: { partition_by: [order_date] } }
      at_placement: table_property

  - id: inject_zorder_overlap
    match: { scenario_type: negative, table_physical_has: cluster_by }
    effect:
      inject_signal: zorder_present
      via: { set_table_physical: { zorder_by: [region] } }
      at_placement: table_property

  - id: inject_non_delta
    match: { scenario_type: negative, asset_has: format }
    effect:
      inject_signal: non_delta_format
      via: { set_field: format, value: parquet }

  - id: inject_too_many_keys
    match: { scenario_type: negative, table_physical_has: cluster_by }
    effect:
      inject_signal: clustering_key_count_exceeded
      via: { set_table_physical: { cluster_by: [c1, c2, c3, c4, c5, c6] } }
      at_placement: table_property

  - id: inject_missing_maintenance
    match: { scenario_type: negative, table_physical_has: cluster_by }
    effect:
      inject_signal: no_maintenance_path
      via: { remove_table_physical_keys: [predictive_optimization, scheduled_optimize] }

  # ---- edge patterns ----
  - id: edge_key_limit_boundary
    match: { scenario_type: edge, table_physical_has: cluster_by }
    effect:
      set_table_physical: { cluster_by: [c1, c2, c3, c4] }
    note: "Exactly at the supported key limit — valid, but the boundary case."

  - id: edge_single_low_cardinality_key
    match: { scenario_type: edge, table_physical_has: cluster_by }
    effect:
      inject_signal: clustering_on_low_cardinality_only
      via: { set_column_cardinality: { field: cluster_by, value: 3 } }

  - id: edge_empty_table_clustered
    match: { scenario_type: edge, asset_has: row_count }
    effect:
      set_field: row_count
      value: 0
    note: "Clustering declared on an empty table — valid but nothing to cluster."

  # ---- distractor planting ----
  - id: plant_clustering_distractors
    match: { scenario_type_in: [distractor, mixed] }
    effect:
      plant_from: distractor_templates
      count_from_knob: distractor_count
      surfaces_from_knob: distractor_surface_mix

# =====================================================================
# IMPACT RULES — powers "what happens if clustering is applied?".
# =====================================================================
impact_rules:
  - signal: non_delta_format
    consequence: blocked
    impact: "CLUSTER BY cannot be declared; the statement is rejected on a non-Delta table."
    remediation: "Convert to Delta, then evaluate clustering candidacy again."
  - signal: bucketed_table
    consequence: blocked
    impact: "Clustering cannot be applied alongside bucketing."
    remediation: "Rewrite the table without buckets, then apply clustering."
  - signal: partition_and_cluster_conflict
    consequence: blocked
    impact: "The table definition is invalid; partitioning and clustering cannot coexist."
    remediation: "Drop the partition specification and recreate or ALTER the table with CLUSTER BY only."
  - signal: clustering_key_count_exceeded
    consequence: blocked
    impact: "Key declaration is rejected."
    remediation: "Keep the four most selective predicate columns, ordered by filter frequency."
  - signal: zorder_present
    consequence: requires_change
    impact: "Existing ZORDER layout is superseded but not automatically replaced; skipping stats stay stale until a full reclustering pass runs."
    remediation: "Remove the ZORDER declaration, declare CLUSTER BY, and run a one-time full OPTIMIZE."
  - signal: no_maintenance_path
    consequence: degraded
    impact: "The table declares clustering but data never gets clustered; queries see no improvement while write cost is unchanged."
    remediation: "Enable predictive optimization or schedule OPTIMIZE."
  - signal: clustering_on_low_cardinality_only
    consequence: degraded
    impact: "File skipping is coarse; benefit approximates partitioning without its predictability."
    remediation: "Re-select keys against actual query predicates, favouring higher-cardinality filter columns."
  - signal: already_clustered
    consequence: safe
    impact: "No adoption change needed."
    remediation: "Review key selection against current query patterns and confirm maintenance is running."
  - signal: no_filter_predicates
    consequence: safe
    impact: "Clustering would add OPTIMIZE cost with no read-side benefit."
    remediation: "Leave layout unchanged."
  - signal: over_partitioned
    consequence: benefit
    impact: "Removing fixed partition boundaries reduces metadata overhead and small-file count."
    remediation: "Migrate the partition key into the clustering key list."
  - signal: small_file_problem
    consequence: benefit
    impact: "Regular OPTIMIZE under clustering consolidates files and improves scan throughput."
    remediation: "Adopt clustering with predictive optimization enabled."
  - signal: high_cardinality_filter_column
    consequence: benefit
    impact: "Data skipping improves markedly on predicates that previously scanned every partition."
    remediation: "Order clustering keys by filter frequency, most selective first."
  - signal: skewed_partition_distribution
    consequence: benefit
    impact: "File sizes even out; straggler tasks from oversized partitions are reduced."
    remediation: "Adopt clustering and re-measure task duration distribution."
  - signal: evolving_query_pattern
    consequence: benefit
    impact: "Keys can be changed with ALTER TABLE and applied incrementally rather than by rewriting the table."
    remediation: "Adopt clustering, or CLUSTER BY AUTO where predicates are unstable."
  - signal: cdc_or_merge_heavy
    consequence: benefit
    impact: "Merge predicates align with file layout, reducing rewrite amplification."
    remediation: "Cluster on the merge join keys."

# =====================================================================
# INTERACTION DECLARATIONS — how this feature relates to others.
# =====================================================================
interaction_declarations:
  - with: serverless
    kind: independent
    rationale: "Clustering is a table-layout concern; serverless eligibility is a compute concern. Both verdicts stand."
  - with: photon
    kind: independent
    rationale: >
      Clustering reduces bytes scanned; Photon accelerates the scan itself. They compound
      rather than conflict, and neither suppresses the other.
  - with: any
    kind: conflicts
    when: { other_feature_targets_dimension: [table_physical], recommendations_disagree: true }
    rationale: "Two features prescribing different physical layouts for one table is a genuine conflict; surface it for review rather than merging."
recommends_capability: table_layout_optimization

# =====================================================================
# VALIDATION RULES — deterministic checks for clustering correctness.
# =====================================================================
validation_rules:
  - id: format_is_delta_when_clustered
    check:
      for_each: { asset_where: { table_physical_has: cluster_by } }
      assert_field_equals: { field: format, value: delta }
  - id: no_partition_with_cluster
    check:
      for_each: { asset_where: { table_physical_has: cluster_by } }
      assert_keys_absent: [partition_by, bucket_by, num_buckets]
  - id: no_zorder_with_cluster
    check:
      for_each: { asset_where: { table_physical_has: cluster_by } }
      assert_keys_absent: [zorder_by]
  - id: cluster_key_count_within_limit
    check:
      for_each: { asset_where: { table_physical_has: cluster_by } }
      assert_key_count_lte: { field: cluster_by, max: 4 }
  - id: maintenance_declared
    check:
      when: { scenario_type: positive }
      for_each: { asset_where: { table_physical_has: cluster_by } }
      assert_any_key_present: [predictive_optimization, scheduled_optimize]
  - id: clustering_target_in_vocabulary
    check:
      for_each: asset
      assert_field_in_vocabulary: { field: clustering_target, vocabulary: clustering_targets }
  - id: negative_scenario_has_injected_signal
    check:
      when: { knob_equals: { scenario_type: negative } }
      assert_present_any_signal_from: eligibility_signals
  - id: distractor_scenario_stays_neutral
    check:
      when: { knob_equals: { scenario_type: distractor } }
      assert: "no constraint signal matched; verdict resolves to recommended or not_applicable on benefit evidence alone"
  - id: recommendation_requires_benefit
    check:
      assert: "verdict=recommended only when at least one benefit-polarity signal matched"
    note: "Guards the most common failure mode: recommending clustering because nothing blocked it."
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
  clustering_targets:
    - cluster_by_filter_keys
    - cluster_by_merge_keys
    - cluster_by_temporal_plus_dimension
    - cluster_by_auto
    - no_clustering
    - review_before_clustering
  verdicts: [recommended, review_before_clustering, blocked, already_optimal, not_applicable]
  verdict_strength_order: [not_applicable, already_optimal, recommended, review_before_clustering, blocked]
  consequences: [blocked, requires_change, degraded, benefit, safe]
  polarities: [constraint, benefit]
  formats_supported: [delta]
  max_cluster_keys: 4
constrains_grammar_vocabulary:
  formats: [delta]
  note: "Positive clustering scenarios must be Delta; negative scenarios deliberately violate this."

# =====================================================================
# PLATFORM FACTS — grounding statements for Q&A.
# =====================================================================
platform_facts:
  - id: delta_only
    fact: "Liquid Clustering is a Delta Lake capability; non-Delta tables must be converted first."
  - id: replaces_partitioning
    fact: "Clustering replaces partitioning and ZORDER rather than layering on them; the three are mutually exclusive."
  - id: keys_are_alterable
    fact: "Clustering keys can be changed with ALTER TABLE and take effect incrementally, unlike partition keys which are fixed at write time."
    status: proposed
  - id: optimize_required
    fact: "Declaring CLUSTER BY does not cluster existing data; OPTIMIZE — scheduled or via predictive optimization — is what materializes the layout."
    status: proposed
  - id: bounded_keys
    fact: "A bounded number of clustering keys is supported (4); ordering by predicate selectivity matters more than adding keys."
    status: proposed
  - id: auto_mode
    fact: "CLUSTER BY AUTO lets the platform select and adjust clustering keys from observed query patterns."
    status: proposed
```

---

## Guidance

*(LLM reasoning hints only. Nothing here is code-checked; nothing here may override the YAML.)*

**Mapping vague requests to activities.** "Should we cluster this table?" → evaluate
`eligibility_signals` with polarity in mind and report the verdict. "Is partitioning still
right?" → look for `over_partitioned` and `skewed_partition_distribution`. "How do we move
off ZORDER?" → `zorder_present` in `impact_rules`, and note the one-time full OPTIMIZE.
"Make this table clustered" → `apply_rules` with `scenario_type: positive`. "Generate a
clustering test case" → the grammar owns composition; this file supplies which signal and
where it may live.

**The failure mode to guard against.** The most common wrong answer is recommending
clustering because nothing blocked it. Absence of a constraint is not evidence of benefit.
A table with no filter predicates, no skew, no small-file problem and no partition pressure
is `not_applicable`, and saying so is a better answer than a confident recommendation nobody
can justify. The `recommendation_requires_benefit` validation rule exists for this.

**Mapping vague phrasing to signals.** "Too many partitions" / "partition explosion" →
`over_partitioned`. "Queries scan everything" → check whether `high_cardinality_filter_column`
is present; if there are no predicates at all, it's `no_filter_predicates`. "Lots of tiny
files" → `small_file_problem`. "Some partitions are huge" → `skewed_partition_distribution`.
"We keep changing how we query it" → `evolving_query_pattern`, and consider CLUSTER BY AUTO.
"It's a MERGE target" / "CDC table" → `cdc_or_merge_heavy`.

**Thresholds are judgement, not platform limits.** Every signal marked `tunable` — partition
counts, file sizes, cardinality floors, skew factors — encodes an opinion about where benefit
starts. They are starting points to be calibrated against real workloads, and they should be
stated as such in any report rather than presented as platform rules. The one genuine limit
is the clustering key count.

**Phrasing impact explanations.** Lead with the verdict, then separate what *blocks* adoption
from what *motivates* it — readers conflate the two otherwise. For a `recommended` verdict,
name the specific structural evidence ("roughly four thousand partitions averaging under
fifty thousand rows"), never a guessed table name, and state the maintenance requirement in
the same breath: clustering without OPTIMIZE is a declaration, not a layout. For
`review_before_clustering`, lead with the one-time migration cost, since that is usually the
decision-relevant fact.

**Multi-feature reports.** Clustering compounds with Photon rather than competing with it —
fewer bytes scanned, and the scan itself vectorized. Say so when both are recommended. If
another feature prescribes a different physical layout for the same table, that is a genuine
conflict and must be surfaced for human review rather than silently merged.
