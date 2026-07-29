"""
dpstudio/engine/deploy_verify.py

Gate 5 -- real deploy-and-run verification. Every other gate in this codebase
(oracle, validator, dna_check, Gate 4's bundle validate) checks the ARTIFACT.
This gate checks REALITY: does the generated bundle actually deploy and run
successfully against live Databricks infrastructure. No model anywhere in this
check -- it's the strongest possible ground truth available, at the cost of
being slow (real job execution time, not seconds) and creating real side
effects (a real job, real tables) that MUST be torn down afterward.

OPT-IN ONLY. This is not wired into pipeline.run() and should not be -- running
this on every generation means real compute cost and workspace clutter on every
test. Call verify_bundle_end_to_end() explicitly, only for bundles you're about
to rely on (e.g. before sharing with a client).

UNVERIFIED AGAINST A LIVE ENDPOINT. Every function here is built from the
Databricks SDK's documented Jobs API shape and tested for correct JSON/settings
construction, but I have no live workspace to run it against. Same discipline
as DatabricksLLM: test the first real call yourself, expect the first attempt
might surface something, before trusting this in any workflow that matters.
"""
from __future__ import annotations

import time
from pathlib import Path


def build_job_settings(plan: dict, out_dir: str | Path) -> dict:
    """Translates the plan + materialized out_dir into a Jobs API create-job
    settings dict -- the SDK equivalent of what _render_bundle_yaml builds as
    YAML, but as native JSON, since we're calling the Jobs API directly rather
    than going through `databricks bundle deploy` (which needs the CLI, which
    doesn't work in a Free Edition notebook cell).
    """
    out_dir = Path(out_dir)
    pid = plan["plan_id"]
    entry = next(n["node_id"] for n in plan["code_graph"]["nodes"] if n.get("role") == "entry")

    include_seed = bool(plan.get("assets")) and any(
        a.get("table_physical") for a in plan["assets"])

    tasks = []
    if include_seed:
        tasks.append({
            "task_key": "seed_task",
            "notebook_task": {
                "notebook_path": str(out_dir / "src" / "notebooks" / "_seed.py"),
                "base_parameters": {"catalog": "main", "schema": f"synth_verify_{pid}"},
            },
        })
    main_task = {
        "task_key": "main_task",
        "notebook_task": {"notebook_path": str(out_dir / "src" / "notebooks" / f"{entry}.py")},
    }
    if include_seed:
        main_task["depends_on"] = [{"task_key": "seed_task"}]
    tasks.append(main_task)

    return {
        "name": f"[VERIFY-{pid}] synthetic_job (temporary -- torn down after check)",
        "tags": {"plan_id": pid, "purpose": "deploy_verify_temporary"},
        "tasks": tasks,
    }


def deploy_and_run(plan: dict, out_dir: str | Path, workspace_client) -> dict:
    """Creates a real, temporary job from the plan and triggers a run.
    Returns {"job_id": ..., "run_id": ...} -- both needed for polling and
    teardown. Caller is responsible for calling teardown() when done,
    success or failure, or the job will linger in Workflows indefinitely.
    """
    settings = build_job_settings(plan, out_dir)
    job = workspace_client.jobs.create(**settings)
    run = workspace_client.jobs.run_now(job_id=job.job_id)
    return {"job_id": job.job_id, "run_id": run.run_id}


def poll_run(workspace_client, run_id: int, timeout_s: int = 900, poll_seconds: int = 10) -> dict:
    """Blocks until the run reaches a terminal state or timeout_s elapses.
    Returns {"life_cycle_state": ..., "result_state": ..., "state_message": ...,
    "run_duration_s": ...}. A timeout is reported as its own outcome, not raised
    as an exception -- the caller (verify_bundle_end_to_end) still needs to run
    teardown() regardless of how this ends.
    """
    t0 = time.time()
    while True:
        run = workspace_client.jobs.get_run(run_id=run_id)
        state = run.state
        life_cycle = getattr(state, "life_cycle_state", None)
        life_cycle_str = str(life_cycle.value) if hasattr(life_cycle, "value") else str(life_cycle)

        if life_cycle_str in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
            result_state = getattr(state, "result_state", None)
            return {
                "life_cycle_state": life_cycle_str,
                "result_state": str(result_state.value) if hasattr(result_state, "value") else str(result_state),
                "state_message": getattr(state, "state_message", ""),
                "run_duration_s": round(time.time() - t0, 1),
                "timed_out": False,
            }
        if time.time() - t0 > timeout_s:
            return {
                "life_cycle_state": life_cycle_str,
                "result_state": None,
                "state_message": f"Polling timed out after {timeout_s}s -- run may still be in progress.",
                "run_duration_s": round(time.time() - t0, 1),
                "timed_out": True,
            }
        time.sleep(poll_seconds)


def teardown(workspace_client, job_id: int, catalog: str = "main", schema: str | None = None) -> dict:
    """Deletes the temporary job and drops the seeded schema (which drops its
    tables with it). ALWAYS call this after a verify run, success or failure --
    a failed run is exactly when it's tempting to skip cleanup and exactly when
    you most need it, since a job left behind after a failure is easy to forget
    about later.

    Returns a report of what succeeded/failed during teardown itself -- cleanup
    failures should be visible, not silently swallowed.
    """
    report = {"job_deleted": False, "schema_dropped": False, "errors": []}

    try:
        workspace_client.jobs.delete(job_id=job_id)
        report["job_deleted"] = True
    except Exception as e:
        report["errors"].append(f"job deletion failed: {e}")

    if schema:
        try:
            workspace_client.statement_execution.execute_statement(
                warehouse_id=None,  # caller must supply a real warehouse_id if using this path
                statement=f"DROP SCHEMA IF EXISTS {catalog}.{schema} CASCADE",
            )
            report["schema_dropped"] = True
        except Exception as e:
            report["errors"].append(f"schema drop failed: {e}")

    return report


def verify_bundle_end_to_end(plan: dict, out_dir: str | Path, workspace_client,
                             timeout_s: int = 900) -> dict:
    """The full Gate 5 check: deploy, run, poll, teardown, report. This is the
    one function most callers should use -- it guarantees teardown happens
    even if the run fails or times out.
    """
    pid = plan["plan_id"]
    include_seed = bool(plan.get("assets")) and any(
        a.get("table_physical") for a in plan["assets"])
    schema = f"synth_verify_{pid}" if include_seed else None

    ids = deploy_and_run(plan, out_dir, workspace_client)
    try:
        outcome = poll_run(workspace_client, ids["run_id"], timeout_s=timeout_s)
    finally:
        cleanup = teardown(workspace_client, ids["job_id"], schema=schema)

    outcome["job_id"] = ids["job_id"]
    outcome["run_id"] = ids["run_id"]
    outcome["cleanup"] = cleanup
    outcome["passed"] = outcome.get("result_state") == "SUCCESS"
    return outcome
