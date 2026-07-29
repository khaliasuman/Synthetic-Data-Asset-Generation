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
    YAML, but as native SDK objects, since we're calling the Jobs API directly
    rather than going through `databricks bundle deploy` (which needs the CLI,
    which doesn't work in a Free Edition notebook cell).

    IMPORTANT: jobs.create() requires typed SDK objects (jobs.Task,
    jobs.NotebookTask, jobs.TaskDependency), not plain dicts -- confirmed live
    (AttributeError: 'dict' object has no attribute 'as_dict') when this first
    passed raw dicts. Fixed to construct the real SDK dataclasses.
    """
    from databricks.sdk.service import jobs as sdk_jobs

    out_dir = Path(out_dir)
    pid = plan["plan_id"]
    entry = next(n["node_id"] for n in plan["code_graph"]["nodes"] if n.get("role") == "entry")

    include_seed = bool(plan.get("assets")) and any(
        a.get("table_physical") for a in plan["assets"])

    tasks = []
    if include_seed:
        tasks.append(sdk_jobs.Task(
            task_key="seed_task",
            notebook_task=sdk_jobs.NotebookTask(
                notebook_path=str(out_dir / "src" / "notebooks" / "_seed.py"),
                base_parameters={"catalog": "main", "schema": f"synth_verify_{pid}"},
            ),
        ))
    main_task = sdk_jobs.Task(
        task_key="main_task",
        notebook_task=sdk_jobs.NotebookTask(
            notebook_path=str(out_dir / "src" / "notebooks" / f"{entry}.py"),
        ),
        depends_on=[sdk_jobs.TaskDependency(task_key="seed_task")] if include_seed else None,
    )
    tasks.append(main_task)

    return {
        "name": f"[VERIFY-{pid}] synthetic_job (temporary -- torn down after check)",
        "tags": {"plan_id": pid, "purpose": "deploy_verify_temporary"},
        "tasks": tasks,
    }


def _ensure_registered(workspace_client, path: str) -> None:
    """Explicitly (re-)registers a file as a proper workspace object via the
    Workspace API, rather than relying on a raw filesystem write (what
    materialize() does) to also be visible to the Jobs API.

    Root cause found live: materialize() writes files via plain Python
    open().write_text() to a /Workspace/... path. That write is genuinely
    readable via plain Python file I/O immediately (confirmed: print(open(
    path).read()) always worked). But workspace_client.workspace.get_status()
    on that SAME exact path consistently failed with "doesn't exist", even
    after 10s of retrying -- too long for a mere propagation lag. This points
    at two DIFFERENT systems: the FUSE-mounted filesystem (what a raw write
    touches) and the workspace's own object registry (what the Jobs API's
    notebook_path resolution actually queries). A raw write may never appear
    in the latter without an explicit import.

    This function reads the already-written file's real content and uploads
    it via workspace.upload(), which uses the correct object-registration API
    -- guaranteeing the Jobs API can find it, rather than hoping a filesystem
    write eventually becomes visible to a different index.
    """
    from databricks.sdk.service.workspace import ImportFormat, Language
    content = Path(path).read_bytes()
    workspace_client.workspace.upload(
        path, content, format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True,
    )


def deploy_and_run(plan: dict, out_dir: str | Path, workspace_client) -> dict:
    """Creates a real, temporary job from the plan and triggers a run.
    Returns {"job_id": ..., "run_id": ...} -- both needed for polling and
    teardown. Caller is responsible for calling teardown() when done,
    success or failure, or the job will linger in Workflows indefinitely.
    """
    settings = build_job_settings(plan, out_dir)

    # Explicitly register every referenced notebook via the proper Workspace
    # API before job creation, rather than hoping materialize()'s raw file
    # write is independently visible to the Jobs API's own object registry.
    registration_errors = []
    for task in settings["tasks"]:
        path = task.notebook_task.notebook_path
        try:
            _ensure_registered(workspace_client, path)
        except Exception as e:
            registration_errors.append((path, str(e)))
    if registration_errors:
        raise RuntimeError(
            f"Failed to register one or more notebooks via the Workspace API "
            f"before job creation: {registration_errors}."
        )

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
            result_state_str = str(result_state.value) if hasattr(result_state, "value") else str(result_state)

            # The top-level run state_message is often just "Workload failed,
            # see run output for details" -- genuinely useless on its own.
            # Fetch the actual per-task output (the real traceback/error) so a
            # failure is diagnosable rather than a dead end. Best-effort: if
            # this fetch itself fails, don't let that mask the original result.
            task_outputs = {}
            if result_state_str != "SUCCESS":
                try:
                    for task_run in getattr(run, "tasks", None) or []:
                        try:
                            out = workspace_client.jobs.get_run_output(run_id=task_run.run_id)
                            task_outputs[task_run.task_key] = {
                                "error": getattr(out, "error", None),
                                "error_trace": getattr(out, "error_trace", None),
                                "notebook_output_result": getattr(
                                    getattr(out, "notebook_output", None), "result", None),
                            }
                        except Exception as e:
                            task_outputs[task_run.task_key] = {"fetch_failed": str(e)}
                except Exception:
                    pass

            return {
                "life_cycle_state": life_cycle_str,
                "result_state": result_state_str,
                "state_message": getattr(state, "state_message", ""),
                "run_duration_s": round(time.time() - t0, 1),
                "timed_out": False,
                "task_outputs": task_outputs,
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

    Auto-discovers an available SQL warehouse for the schema-drop statement
    rather than requiring the caller to know a specific warehouse_id -- this
    was previously a silent gap: nothing ever supplied one, so schema_dropped
    was always False and every seeded schema was left behind uncleaned.

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
        warehouse_id = None
        try:
            warehouses = list(workspace_client.warehouses.list())
            running = [w for w in warehouses if str(getattr(w.state, "value", w.state)) == "RUNNING"]
            chosen = running[0] if running else (warehouses[0] if warehouses else None)
            warehouse_id = chosen.id if chosen else None
        except Exception as e:
            report["errors"].append(f"warehouse discovery failed: {e}")

        if not warehouse_id:
            report["errors"].append(
                "no SQL warehouse available to drop the seeded schema -- "
                f"{catalog}.{schema} was left behind and needs manual cleanup, "
                "or pass warehouse_id explicitly once one exists in this workspace."
            )
        else:
            try:
                workspace_client.statement_execution.execute_statement(
                    warehouse_id=warehouse_id,
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
