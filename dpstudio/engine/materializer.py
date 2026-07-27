"""
dpstudio/engine/materializer.py

Reads ONLY the plan (plan_only_input, per the grammar's materialization_contract).
Writes real notebooks, builds a real wheel from generated source, writes a
databricks.yml, and stamps plan_id into every artifact. No LLM call.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _stamp(artifacts: list, kind: str, path: str, node_id: str | None = None,
           build_status: str | None = None):
    artifacts.append({
        "artifact_id": "art_" + hashlib.md5((kind + path).encode()).hexdigest()[:8],
        "artifact_kind": kind, "path": path, "plan_id": None,  # filled by caller
        **({"node_id": node_id} if node_id else {}),
        **({"build_status": build_status} if build_status else {}),
    })


def _render_notebook(plan: dict, node_id: str) -> str:
    pid, pver = plan["plan_id"], plan["plan_version"]
    edges = [e for e in plan["code_graph"]["edges"] if e["from_node"] == node_id]
    node_path = {n["node_id"]: f"./{n['node_id']}" for n in plan["code_graph"]["nodes"]}
    roles = {n["node_id"]: n.get("role") for n in plan["code_graph"]["nodes"]}

    lines = ["# Databricks notebook source", f"# plan_id: {pid}  plan_version: {pver}",
              "# COMMAND ----------"]

    # Widgets preamble -- the client's canonical entry-notebook opening (grammar
    # materialization_contract rule widgets_preamble). Entry nodes only; children
    # receive values via their caller, matching real bundles.
    if (roles.get(node_id) == "entry"
            and plan.get("knobs", {}).get("param_passing") == "widgets"):
        params = plan.get("knobs", {}).get("param_names") or [
            "catalog", "schema", "receive_date"]
        lines += ["dbutils.widgets.removeAll()", "# COMMAND ----------"]
        for p in params:
            lines.append(f'dbutils.widgets.text("{p}", "")')
            lines.append(f'{p} = dbutils.widgets.get("{p}")')
        lines.append("# COMMAND ----------")

    for d in plan.get("distractors", []):
        if d["node_id"] == node_id and d["surface"] == "markdown_cell":
            lines += ["# MAGIC %md", f"# MAGIC {d.get('text', d['imitates_signal'])}",
                      "# COMMAND ----------"]
    for e in edges:
        if e["reference_mechanism"] == "magic_run":
            lines += [f"# MAGIC %run {node_path[e['to_node']]}", "# COMMAND ----------"]
        elif e["reference_mechanism"] == "dbutils_notebook_run":
            lines += [f"dbutils.notebook.run('{node_path[e['to_node']]}', 600, {{}})"]
    for d in plan.get("distractors", []):
        if d["node_id"] == node_id and d["surface"] in ("line_comment", "variable_or_column_name"):
            lines.append(d.get("text", f"# {d['imitates_signal']}"))
    lines += plan["_node_code"].get(node_id, {}).get("executable", [])
    return "\n".join(lines) + "\n"


def _build_wheel(plan: dict, out_dir: Path, artifacts: list) -> Path | None:
    lib_nodes = [n for n in plan["code_graph"]["nodes"] if n.get("role") == "library_src"]
    if not lib_nodes:
        return None
    pid = plan["plan_id"]
    pkg = out_dir / "libsrc" / "dp_synth_lib"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__plan_id__ = "{pid}"\n')
    for n in lib_nodes:
        code = "\n".join(plan["_node_code"].get(n["node_id"], {}).get("executable", []))
        (pkg / f"{n['node_id']}.py").write_text(f"# plan_id: {pid}\n{code}\n")
    (out_dir / "libsrc" / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "dp_synth_lib"\nversion = "0.1.0"\ndescription = "generated; plan_id={pid}"\n'
        '\n[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["dp_synth_lib*"]\n')
    r = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir",
                        str(out_dir / "dist"), str(out_dir / "libsrc")],
                       capture_output=True, text=True)
    whl = next((out_dir / "dist").glob("*.whl"), None) if r.returncode == 0 else None
    _stamp(artifacts, "wheel", f"dist/{whl.name}" if whl else "dist/FAILED",
           node_id=lib_nodes[0]["node_id"], build_status="success" if whl else "failed")
    if not whl:
        print("WHEEL BUILD FAILED:", r.stdout[-1000:], r.stderr[-1000:])
    return whl


def _render_bundle_yaml(plan: dict, whl_name: str | None) -> str:
    pid, pver = plan["plan_id"], plan["plan_version"]
    entry = next(n["node_id"] for n in plan["code_graph"]["nodes"] if n.get("role") == "entry")
    lib_line = (f"      environments:\n        - environment_key: default\n"
                f"          spec:\n            dependencies:\n"
                f"              - /Volumes/main/synth_studio/libs/{whl_name}\n"
                if whl_name else "")

    # Schedule block (grammar materialization_contract rule schedule_block):
    # when the plan resolves job_trigger=schedule, emit a Quartz cron + timezone
    # in the client's observed form. Vary the minute/hour deterministically from
    # plan_id so generated jobs don't all share one identical schedule.
    trigger = (plan.get("knobs", {}).get("job_trigger")
               or (plan.get("assets") or [{}])[0].get("job_trigger"))
    sched_line = ""
    if trigger == "schedule":
        h = int(hashlib.md5(pid.encode()).hexdigest(), 16)
        minute, hour = h % 60, h // 60 % 24
        sched_line = (f"      schedule:\n"
                      f"        quartz_cron_expression: \"0 {minute} {hour} * * ?\"\n"
                      f"        timezone_id: America/Los_Angeles\n")

    return f"""# plan_id: {pid}  plan_version: {pver}  (generated {datetime.now(timezone.utc).isoformat()})
bundle:
  name: synthetic_{pid}

resources:
  jobs:
    synthetic_job:
      name: "[{pid}] synthetic_job"
      tags:
        plan_id: {pid}
        plan_version: "{pver}"
        scenario_type: {plan.get('scenario_type', 'positive')}
{sched_line}      tasks:
        - task_key: main_task
          notebook_task:
            notebook_path: ./src/notebooks/{entry}.py
{lib_line}
"""


def _strip_duplicated_reference_lines(plan: dict) -> dict:
    """Deterministically removes any literal magic_run/dbutils_notebook_run line
    from a node's executable code. Those mechanisms are declared once via
    code_graph.edges and rendered by _render_notebook -- a literal copy in
    executable code is always a planner duplication, never intentional content.

    Promoted from instruction-only (planner.py) to a code guarantee after the
    instruction failed a second time on a different scenario shape (a
    library-backed pipeline) than the one it was originally fixed against.
    Same escalation pattern as normalize_table_physical.
    """
    for node_id, node_code in plan.get("_node_code", {}).items():
        executable = node_code.get("executable", [])
        cleaned = [line for line in executable
                  if "%run " not in line and "dbutils.notebook.run(" not in line]
        if len(cleaned) != len(executable):
            removed = [l for l in executable if l not in cleaned]
            plan.setdefault("plan_notes", "")
            plan["plan_notes"] = (
                f"{plan['plan_notes']} strip_duplicated_reference_lines: removed "
                f"{removed} from node {node_id} -- already rendered via a "
                f"code_graph edge, duplicate literal line discarded."
            ).strip()
            node_code["executable"] = cleaned
    return plan


def materialize(plan: dict, out_dir: str | Path) -> dict:
    plan = _strip_duplicated_reference_lines(plan)
    out_dir = Path(out_dir)
    (out_dir / "src" / "notebooks").mkdir(parents=True, exist_ok=True)
    (out_dir / "src" / "sql").mkdir(parents=True, exist_ok=True)
    artifacts: list = []
    pid = plan["plan_id"]

    for n in plan["code_graph"]["nodes"]:
        if n.get("role") in ("entry", "child"):
            rel = f"src/notebooks/{n['node_id']}.py"
            (out_dir / rel).write_text(_render_notebook(plan, n["node_id"]))
            _stamp(artifacts, "notebook", rel, node_id=n["node_id"])
        elif n.get("role") == "module":
            rel = f"src/notebooks/{n['node_id']}.py"
            code = "\n".join(plan["_node_code"].get(n["node_id"], {}).get("executable", []))
            (out_dir / rel).write_text(f"# plan_id: {pid}\n{code}\n")
            _stamp(artifacts, "python_module", rel, node_id=n["node_id"])

    whl = _build_wheel(plan, out_dir, artifacts)

    yml = _render_bundle_yaml(plan, whl.name if whl else None)
    (out_dir / "databricks.yml").write_text(yml)
    _stamp(artifacts, "bundle_yaml", "databricks.yml")

    for i, asset in enumerate(plan["assets"]):
        tp = asset.get("table_physical", {})
        props = [f"'plan_id'='{pid}'", f"'plan_version'='{plan['plan_version']}'"]
        if tp.get("change_data_feed"):
            props.append("'delta.enableChangeDataFeed'='true'")
        cluster = f"CLUSTER BY ({', '.join(tp['cluster_by'])})\n" if tp.get("cluster_by") else ""
        ddl = (f"-- plan_id: {pid}\nCREATE TABLE IF NOT EXISTS main.synth_studio.asset_{i} "
               f"(id BIGINT)\n{cluster}TBLPROPERTIES ({', '.join(props)});\n")
        rel = f"src/sql/asset_{i}.sql"
        (out_dir / rel).write_text(ddl)
        _stamp(artifacts, "table", rel)

    for a in artifacts:
        a["plan_id"] = pid

    plan["artifacts"] = artifacts
    plan["plan_status"] = "materialized" if plan["plan_status"] not in ("failed",) else "failed"
    plan["plan_materialized_at"] = datetime.now(timezone.utc).isoformat()
    return plan
