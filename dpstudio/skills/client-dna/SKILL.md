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
version: "2.0"
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
    description: >
      Job-level metadata across ~1,100 job records (combined export) plus a 182-row cumulus
      export and a 55-row dev export. NOTE: the dependent_libraries, schedules, compute, and
      parameters columns are empty or error-filled in the cumulus and dev CSVs (extraction
      bug: "'JobSettings' object has no attribute 'libraries'" appears verbatim) — metadata
      conclusions below rest on bundle code, not these columns.
  - kind: structured_job_bundles
    description: >
      ~60 fully-extracted job bundles (original export) plus 8 cumulus repo bundles
      (dps_workflow at 339 files, dps_etl_utils_lib with real setup.py + .whl) plus 40+
      dev-environment job bundles (udm-*, lf-idp-*, onecloud-*, aa-*) showing real folder
      structure, module composition, reference mechanisms, widget parameterization, and
      language mix.
  - kind: dataos_pipeline_exports
    description: >
      80 DataOS pipeline JSON exports with per-pipeline dependency trees (depth 1–6,
      median 5), cadence (100% daily in this slice), cron schedules with timezone
      (e.g. "13 0 7 * * ? (America/Los_Angeles)"), DBR versions 11.3–15.4, and
      LLM-classified pipeline types (70 pyspark / 10 python).

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
    bounds:
      max: 2
    status: reviewed
    evidence: "task-count-per-job distribution across the job metadata export; observed max is 2. Confirmed by dev-corpus job bundles: every extracted job carries a single entry notebook path (one *_wrapper per job)."

  - dimension: node_count
    applies_to: any
    default: 3
    bounds:
      max: 8
    status: pending
    evidence: >
      Module/folder count per job bundle — most real jobs carry 3+ distinct code modules
      under a single task. Dev-corpus bundles run larger than the original export suggested:
      udm-printer-model carries workflow + bat + common (2 modules) + a dependencies tree of
      9 shared notebooks; aa-supplyjournal carries 20+ modules across src/redshift,
      src/postProcess, src/mfg, src/config. Max raised 6 -> 8 to admit the observed richer
      composition; default stays 3 (typical single-purpose job). Marked pending until the
      raised bound is confirmed as generation-appropriate rather than repo-accumulation.

  - dimension: reference_depth
    applies_to: any
    default: 2
    bounds:
      max: 5
    status: pending
    evidence: >
      Originally derived from folder depth in structured bundles (max 3). DataOS
      fileDependencyTree exports now give direct chain measurements: depth 1–6, median 5
      (e.g. workflow_invoker -> workflow_params -> credentials_for_enterprise ->
      aws_secrets_manager). Max raised 3 -> 5 accordingly. Default stays 2 — deep chains
      exist but the entry->child->module shape remains the common case.

  - dimension: reference_mechanism_mix
    applies_to: any
    default: [python_import, magic_run, dbutils_notebook_run]
    status: pending
    evidence: >
      Real HP jobs follow a consistent shape, not an arbitrary mix: one orchestrator node
      navigates to child notebooks via magic_run/dbutils_notebook_run, while distinct
      internal-utility modules (shared helpers, credential-handling code) are reached via
      python_import directly from wherever they're used — not chained through the
      orchestrator. This mirrors the app/ + libraries/* + workflow/*_wrapper+*_invoker
      structure seen across all bundle corpora (Triage bundle, dps_workflow's
      pipeline_engine, dataos workflow_invoker trees). QUANTIFIED in the dev corpus:
      magic_run appears ~227 times vs dbutils_notebook_run ~4 times across three
      representative bundles — both are real, but generation should treat magic_run as
      dominant and dbutils_notebook_run as an occasional variant (roughly a 50:1 ratio,
      not 50:50). Entry notebooks follow a *_wrapper / *_invoker naming convention
      (processor_wrapper, udm_workflow_full_printer_wrapper, workflow_invoker).

  - dimension: job_trigger
    applies_to: any
    default: schedule
    bounds:
      in_scope: [manual, schedule]
    status: pending
    evidence: >
      CONFLICTING SLICES, documented honestly: the original ~1,100-row export showed manual
      as the largest single category (~40-45%). The DataOS corpus (80 pipelines) is 100%
      daily cadence, 34/80 with explicit Quartz cron + timezone (e.g. "13 0 7 * * ?
      (America/Los_Angeles)"); the cumulus CSV shows 56/182 cron; dev-corpus job names carry
      -daily/-stage suffixes implying scheduled execution. Default moved manual -> schedule
      on the strength of the newer corpora; both values stay in scope. Needs an SME call on
      which slice better represents the generation target — this is exactly the kind of
      conflict a human should resolve, not a file.

  - dimension: param_passing
    applies_to: any
    default: widgets
    bounds:
      in_scope: [none, widgets]
    status: pending
    evidence: >
      The single most consistent code pattern in the entire corpus: ~237 dbutils.widgets
      occurrences across three representative dev bundles alone. The canonical entry-notebook
      preamble is: dbutils.widgets.removeAll(), then a block of widgets.text(name, "") /
      widgets.get(name) pairs (udm-printer-model declares 13 named widgets before any logic:
      receive_date, stack_name, spark_input_read_path, redshift_iam_role, secret_name, ...).
      DataOS job exports carry matching parameters payloads (teamName, jobId, runBy).
      Widget-driven parameterization is the norm at this client, not an edge case —
      generated entry notebooks that take no parameters at all do not resemble production.

  - dimension: library_type
    applies_to: any
    default: whl_workspace_file
    bounds:
      in_scope: [none, whl_workspace_file]
    status: pending
    evidence: >
      Real bundles show internal utility modules (shared helpers, credential/secret-handling
      folders) far more often than external package declarations. Confirmed library forms in
      the corpus: a real wheel shipped inside a bundle (dataos_splunk-1.0.15-py3-none-any.whl
      in dps_workflow/notebooks/common), a full setup.py + versioned package
      (dps_etl_utils_lib with etl_utils package, requirements.txt, version.txt), and
      egg-style shared packages (common_utils_egg imported via python_import in
      pipeline_engine). CANDIDATE VALUES NOT YET IN SCOPE: egg/setup.py-installed packages
      and jar dependencies both occur in real code but are not modeled by generation yet —
      documented here so their absence is a recorded decision, not an oversight. The
      dependent_libraries CSV column is empty/error in both newer exports (extraction bug),
      so library-form PROPORTIONS remain unquantifiable; only existence is confirmed.
      "none" stays in scope: table-focused scenarios (e.g. Liquid Clustering) legitimately
      carry no library at all.

  - dimension: environment_naming
    applies_to: any
    default: null
    status: pending
    evidence: >
      Multiple inconsistent spellings of the same logical environment concept observed
      across real parameter payloads — candidate distractor/edge-case source, not a
      default. Now substantially enriched by the dev corpus: job names carry environment
      suffixes -dev / -stage / -itg / -daily (10/23/2/13 in the dev slice), workspace paths
      embed blue/green deployment slots (/stage/gbd-lf-processor/blue/workflow/...,
      /daily/.../green/workflow/...), and the same job commonly exists as parallel -stage
      and -daily variants. Blue/green slot paths and env-suffixed job-name pairs are
      first-class distractor material: they look meaningful to a naive matcher but carry
      no compatibility signal.

  - dimension: environment_version_profile
    applies_to: any
    default: unset
    status: pending
    evidence: >
      DBR runtime pinning is a loud, real dimension in this estate: 11.3.x, 13.3.x, 14.2.x,
      14.3.x, and 15.4.x all present across the DataOS corpus (14.3.x most common at 20/80).
      Directly relevant to serverless-migration realism — an 11.3-pinned job is a different
      migration story than a 15.4 one. Kept default unset (generation does not pin a runtime
      unless asked) but recorded so scenario prompts CAN exercise version-pinned cases and
      the spread of plausible values is documented.

  - dimension: language
    applies_to: any
    default: python
    bounds:
      in_scope: [python, sql]
    status: pending
    evidence: >
      Generation is Python/SQL-only today, and that matches the dominant reality (DataOS
      classification: 70 pyspark / 10 python out of 80). HOWEVER: real bundles contain
      Scala modules alongside Python — Scala source confirmed in udm-printer dependencies
      (val schema = ..., .rdd.map(r => ...) in notebooks/common/lib) and aa-supplyjournal
      (Schema_Compare with emptyRDD[Row]). Scala is deliberately out of generation scope
      for now; this entry exists so that decision is recorded and reviewable, and so the
      serverless feature's non_python_sql_language signal is understood to fire on real,
      current client code — not a hypothetical.

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

**The production shape to aim for, in one paragraph.** A typical generated job should look
like: one entry notebook named like a wrapper/orchestrator, opening with a
dbutils.widgets.removeAll() + widgets.text/get parameter preamble, navigating to one or
two child notebooks via %run (occasionally dbutils.notebook.run — roughly 1-in-50, not
1-in-2), importing one or more distinct utility modules directly via python_import where
their logic is used, backed by an internal wheel when a library is called for, scheduled
daily via cron with a timezone, and living under a path that carries an environment suffix
and possibly a blue/green slot. That composite sentence is what all three data packages
agree on.

**What this file deliberately does NOT model, as recorded decisions.** Repository-level
operational sprawl (playbooks, monitoring configs, batch lists — dps_workflow's 339 files
are repo accumulation, not per-job structure; generation models the JOB unit); Scala
modules (real, present, out of scope — see the language entry); egg/setup.py and jar
library forms (real, present, candidates — see library_type). Each absence is a decision
with an evidence trail, not a blind spot.

**When a feature has no scope entry at all.** Generate anyway, using that feature's own skill
defaults, and flag it. Absence here is a coverage gap to close later, not a reason to refuse
the request — refusing degrades usefulness for a problem (thin review coverage) that a flag
already solves honestly.

**Growing this file.** If a request needs a dimension or feature this file doesn't yet cover,
that's the moment to add an entry — via `extension_pattern` — not to force the request
through an existing entry that doesn't really fit. A slightly-wrong forced match is worse
than an honest `pending` entry with a rough first-pass value.
