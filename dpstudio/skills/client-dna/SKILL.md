---
name: client-dna
description: The client's approved default generation scope and hard bounds, derived from real production data. Consult before resolving any knob or feature scope a request doesn't specify explicitly. Composes with the asset-bundle-generation grammar and its feature skills — this file supplies WHICH defaults and bounds apply for this client; it never redefines dimensions, knobs, signals, or plan structure, which remain owned by the grammar and feature skills. Use it any time a plan is being resolved for this client, especially when the request is under-specified.
---

# Client DNA — Approved Defaults & Bounds

This file answers one question the grammar and feature skills deliberately don't: *for this
specific client, what does "typical" and "in scope" actually mean?* The grammar defines the
universe of possible dimensions and knobs; the feature skills define compatibility
judgement; this file defines which corner of that universe reflects this client's real
production, and which requests fall outside anything reviewed yet.

Two zones, same convention as every other skill in this system: a machine-readable YAML zone
that code resolves against, and a guidance zone of prose hints that is never code-checked
and may never contradict the YAML.

Three invariants:

- **Defaults only fill gaps.** An explicit value in the user's request always wins. This
  file only resolves knobs the request left unstated.
- **Bounds are enforced, not advisory.** A resolved plan that falls outside a bound here does
  not generate silently — it is routed for review, with the specific bound and margin
  recorded, per `generation_constraints`.
- **Coverage is stated, not assumed.** Every entry carries a review status. A dimension or
  feature with no entry here is not "unbounded" — it falls through to the grammar's own
  defaults, and that fallback is itself recorded on the plan.

---

## Machine-readable zone

```yaml
version: "1.0"
composes_with:
  grammar: asset-bundle-generation
  grammar_version: ">=2.0"
  note: >
    This file never defines a dimension, knob, signal, or plan field that doesn't already
    exist in the grammar or a feature skill. It only supplies client-specific values for
    dimensions the grammar already declares, plus scope flags for which features are
    currently reviewed for this client.

review_state: partial
evidence_sources:
  - kind: job_metadata_export
    description: "job-level metadata across ~1,100 job records, including compute/cluster shape, trigger type, and parameter payloads"
  - kind: structured_job_bundles
    description: "~60 fully-extracted job bundles showing real folder structure, module composition, and reference depth"

# =====================================================================
# DIMENSION PROFILES — extensible registry. Each entry supplies a
# default and/or bounds for ONE dimension already declared in the
# grammar. Adding a new dimension means adding an entry here, never
# changing this file's structure. `applies_to` scopes an entry to a
# specific feature, or "any" for structural dimensions the grammar
# owns regardless of feature.
# =====================================================================
dimension_profiles:
  - dimension: task_count
    applies_to: any
    default: 1
    bounds: { max: 2 }
    status: reviewed
    evidence: "task-count-per-job distribution across the job metadata export; observed max is 2"

  - dimension: node_count
    applies_to: any
    default: 3
    bounds: { max: 6 }
    status: reviewed
    evidence: "module/folder count per job bundle — most real jobs carry 3+ distinct code modules under a single task"

  - dimension: reference_depth
    applies_to: any
    default: 2
    bounds: { max: 3 }
    status: pending
    evidence: "derived from folder depth in structured bundles; not yet confirmed against actual %run/import chains"

  - dimension: reference_mechanism_mix
    applies_to: any
    default: [python_import, magic_run]
    status: pending
    evidence: "inferred from module layout, not directly observed in source"

  - dimension: job_trigger
    applies_to: any
    default: manual
    status: reviewed
    evidence: "manual trigger share observed at roughly 40-45% across the job metadata export — the largest single category"

  - dimension: library_type
    applies_to: any
    default: whl_workspace_file
    bounds:
      in_scope: [internal_utils_module, whl_workspace_file]
    status: pending
    evidence: "real bundles show internal utility modules (e.g. shared helpers, credential/secret-handling folders) far more often than external package declarations; external dependency field in one export source was found unreliable and excluded"

  - dimension: environment_naming
    applies_to: any
    default: null
    status: pending
    evidence: "multiple inconsistent spellings of the same logical environment concept observed across real parameter payloads; candidate distractor/edge-case source, not yet a default"

# =====================================================================
# FEATURE SCOPE — which registered features (from the grammar's
# feature_skills registry) are currently reviewed for this client.
# A feature absent here is NOT blocked; it simply has no client-DNA
# override yet and falls through entirely to that feature skill's own
# defaults, flagged accordingly.
# =====================================================================
feature_scope:
  - feature: serverless
    status: reviewed
  - feature: liquid_clustering
    status: not_yet_reviewed
  - feature: photon
    status: not_yet_reviewed
  # New features register here the same way new features register in
  # the grammar's feature_skills block — add an entry, nothing else
  # in this file changes.

# =====================================================================
# GENERATION CONSTRAINTS — the enforcement layer.
# =====================================================================
generation_constraints:
  - id: defaults_fill_gaps_only
    rule: "A dimension_profiles default is applied only when the request and the grammar's own knob resolution leave that dimension unset. An explicit user-specified value always wins."

  - id: bounds_route_to_review
    rule: >
      If a resolved value for a dimension with declared bounds falls outside those bounds,
      do not generate silently. Set plan_status to needs_review and record the dimension,
      the resolved value, the bound, and the margin.

  - id: pending_status_is_visible
    rule: >
      Any plan that resolves a value from a dimension_profiles entry with status: pending
      must carry a visible note on the plan that this value is not yet fully reviewed.

  - id: unreviewed_feature_is_flagged_not_blocked
    rule: >
      A target_features entry with feature_scope status not_yet_reviewed (or absent from
      feature_scope entirely) still generates, using that feature skill's own defaults, but
      the plan is flagged: this feature has no client-DNA override yet.

  - id: dimension_absent_falls_through
    rule: >
      Any dimension the grammar declares that has no entry here resolves entirely from the
      grammar's own knob defaults. This file narrows the grammar's defaults; it never
      removes or replaces the grammar's fallback behavior.

# =====================================================================
# EXTENSION PATTERN — how this file grows over time.
# =====================================================================
extension_pattern:
  new_dimension: >
    Add one entry to dimension_profiles with {dimension, applies_to, default and/or bounds,
    status: pending, evidence}. Never add a new top-level structure for a new kind of
    dimension — the same five fields cover any dimension the grammar can declare.
  new_feature: >
    Add one entry to feature_scope with {feature, status: not_yet_reviewed}. The feature
    generates immediately using its own skill's defaults; client-DNA values arrive later as
    dimension_profiles entries get added and reviewed for that feature specifically via
    applies_to.
  moving_pending_to_reviewed: >
    Change status in place on the existing entry once the evidence for that dimension has
    been confirmed against the client's actual environment. No other field needs to change.
```

---

## Guidance

*(LLM reasoning hints only. Nothing here is code-checked; nothing here may override the
YAML.)*

**Resolution order for any unspecified knob.** First, use an explicit value from the user's
request if one exists. Second, check `dimension_profiles` here for an entry matching that
dimension and the relevant feature scope. Third, if no entry exists, fall through entirely to
the grammar's own declared default for that knob. Never skip a step, and record which step
resolved each value — that record is what makes a later dispute answerable with a specific
line rather than a general defense of the whole system.

**Treat `status: pending` as a real signal, not a formality.** A pending entry is a working
hypothesis, not a confirmed fact. When generating a report or explaining a plan to someone
outside the system, say plainly which values are reviewed and which are still provisional —
the distinction is the entire point of tracking it.

**A bound is not a target.** `bounds.max` on `node_count` marks the edge of what's been
observed, not a value to aim for by default — the `default` field is the typical case,
`bounds` exists only to catch requests that would generate something never seen in this
client's real estate.

**When a feature has no scope entry at all.** Generate anyway, using that feature's own skill
defaults, and flag it. Absence here is a coverage gap to close later, not a reason to refuse
the request — refusing degrades usefulness for a problem (thin review coverage) that a flag
already solves honestly.

**Growing this file.** If a request needs a dimension or feature this file doesn't yet cover,
that's the moment to add an entry — via `extension_pattern` — not to force the request
through an existing entry that doesn't really fit. A slightly-wrong forced match is worse
than an honest `pending` entry with a rough first-pass value.

