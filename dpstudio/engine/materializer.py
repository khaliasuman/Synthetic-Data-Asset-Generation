"""
dpstudio/engine/materializer.py

Reads ONLY the plan (plan_only_input, per the grammar's materialization_contract).
Writes real notebooks, builds a real wheel from generated source, writes a
databricks.yml, and stamps plan_id into every artifact. No LLM call.
"""
from __future__ import annotations

import hashlib
import re
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


def _render_seed_notebook(plan: dict) -> str:
    """Generates a real setup notebook that creates and populates every table
    the plan's business logic references, using catalog/schema JOB PARAMETERS
    (not hardcoded names) -- resolved at deploy time to whatever real catalog/
    schema the deployer actually has, not baked in here. Runs as a prerequisite
    task so the main job's reads/writes hit real data instead of tables that
    were only ever a name in a plausible-looking string.

    This closes the "generated bundles fail immediately if actually run"
    problem: previously nothing ever executed the DDL sitting in asset_0.sql,
    so any real deployment hit 'table not found' on the first read.
    """
    pid = plan["plan_id"]
    lines = [
        "# Databricks notebook source",
        f"# plan_id: {pid}  plan_version: {plan.get('plan_version', 1)}",
        "# Seed step -- creates and populates every table this plan's business",
        "# logic reads or writes, using real job parameters (not hardcoded names).",
        "# This task must run BEFORE the main task (wired via depends_on in",
        "# databricks.yml) so downstream reads hit real data.",
        "# COMMAND ----------",
        'dbutils.widgets.text("catalog", "main")',
        'dbutils.widgets.text("schema", "synth_studio")',
        'catalog = dbutils.widgets.get("catalog")',
        'schema = dbutils.widgets.get("schema")',
        "spark.sql(f'CREATE CATALOG IF NOT EXISTS {catalog}')",
        "spark.sql(f'CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}')",
        "# COMMAND ----------",
    ]

    for i, asset in enumerate(plan.get("assets", [])):
        tp = asset.get("table_physical", {}) or {}
        table_name = f"synth_table_{i}"
        row_count = tp.get("total_row_count") or tp.get("avg_partition_row_count") or 10000
        row_count = min(int(row_count), 1_000_000)  # cap synthetic seed volume, not the plan's claimed scale
        cluster_clause = ""
        if tp.get("cluster_by"):
            cols = ", ".join(tp["cluster_by"])
            cluster_clause = f" CLUSTER BY ({cols})"

        lines += [
            f"# --- {table_name} ---",
            f"spark.sql(f'''",
            f"  CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.{table_name}{cluster_clause}",
            f"  USING DELTA AS",
            f"  SELECT id AS row_id, ",
            f"         cast(rand() * 1000000 as int) AS user_id,",
            f"         current_timestamp() AS event_ts,",
            f"         cast(rand() * 100 as double) AS value",
            f"  FROM range({row_count})",
            f"''')",
            "# COMMAND ----------",
        ]

    lines.append("print(f'Seed complete: tables created under {catalog}.{schema}')")
    return "\n".join(lines)


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


def _render_bundle_yaml(plan: dict, whl_name: str | None, include_seed_task: bool = False) -> str:
    pid, pver = plan["plan_id"], plan["plan_version"]
    entry = next(n["node_id"] for n in plan["code_graph"]["nodes"] if n.get("role") == "entry")

    # KNOWN PLACEHOLDER, not a real path in any workspace: parameter substitution
    # for job-cluster library dependency paths is not confirmed safe across all
    # Databricks bundle schema versions, so this is left explicit and flagged
    # rather than silently guessed at. Replace with a real Unity Catalog Volume
    # path in YOUR workspace before deploying live -- this is the one piece of
    # this bundle that still requires a manual edit.
    lib_line = (f"      environments:\n        - environment_key: default\n"
                f"          spec:\n            dependencies:\n"
                f"              # PLACEHOLDER -- replace with a real Volume path in your workspace\n"
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

    # Job-level parameters (real Databricks Asset Bundle feature): the deployer
    # supplies actual catalog/schema at deploy or run time, referenced via
    # {{job.parameters.<name>}} in each task's base_parameters. Nothing here is
    # a fictional hardcoded name -- "main"/"synth_studio" are only DEFAULTS,
    # fully overridable, never assumed to exist.
    params_block = ("      parameters:\n"
                    "        - name: catalog\n          default: main\n"
                    "        - name: schema\n          default: synth_studio\n") if include_seed_task else ""

    seed_task_block = ""
    depends_on_block = ""
    if include_seed_task:
        seed_task_block = (
            "        - task_key: seed_task\n"
            "          notebook_task:\n"
            "            notebook_path: ./src/notebooks/_seed.py\n"
            "            base_parameters:\n"
            "              catalog: \"{{job.parameters.catalog}}\"\n"
            "              schema: \"{{job.parameters.schema}}\"\n"
        )
        depends_on_block = "          depends_on:\n            - task_key: seed_task\n"

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
{params_block}{sched_line}      tasks:
{seed_task_block}        - task_key: main_task
{depends_on_block}          notebook_task:
            notebook_path: ./src/notebooks/{entry}.py
{lib_line}
"""


def _fix_python_imports(plan: dict, out_dir: Path) -> dict:
    """Fixes THREE distinct import/environment problems, each found via actual
    live execution -- none of these were ever visible to any static check,
    since none of them execute Python:

    1. library_src nodes: built into a real wheel by _build_wheel(), always
       nested inside a fixed `dp_synth_lib` package. `from {node_id} import X`
       needs the dp_synth_lib. prefix to resolve.

    2. module nodes: plain .py files, never wheel-packaged, never
       automatically on sys.path. Needs sys.path.append() before the import.

    3. Missing pyspark.sql.functions imports: confirmed live -- generated code
       called col(...) without importing it. The planner writes plausible
       PySpark code but doesn't always import every function it uses. Scanned
       and auto-injected here rather than trusted to the model, since this is
       a purely mechanical check (does the code call a name from a known
       function list without importing it) that code can verify perfectly and
       a model can silently miss.

    ADDITIONALLY: the wheel for a library_src import is only ever installed
    when the bundle runs as an actual JOB (via databricks.yml's environment/
    dependencies block) -- confirmed live that opening and running the SAME
    notebook interactively in the editor never installs it at all, so even a
    correctly-prefixed dp_synth_lib import fails outside a job run. Fixed by
    ALSO injecting a %pip install of the wheel's real local materialized path
    (not the still-unresolved placeholder Volume path) -- this works in BOTH
    interactive and job contexts, and doesn't depend on any Volume existing.
    """
    all_nodes = {n["node_id"]: n for n in plan.get("code_graph", {}).get("nodes", [])}
    lib_node_ids = {nid for nid, n in all_nodes.items() if n.get("role") == "library_src"}
    module_node_ids = {nid for nid, n in all_nodes.items() if n.get("role") == "module"}

    notebooks_dir = str(Path(out_dir) / "src" / "notebooks")
    wheel_glob = list((Path(out_dir) / "dist").glob("*.whl")) if (Path(out_dir) / "dist").exists() else []
    wheel_path = str(wheel_glob[0]) if wheel_glob else None

    import_re = re.compile(r"^(\s*from\s+)(\w+)(\s+import\s+.*)$")

    def _resolve_real_node_id(imported_name: str) -> tuple[str, str] | None:
        """Returns (real_node_id, role) if imported_name resolves to a real
        node, either exactly or via the common _lib/_module suffix pattern.
        Confirmed live: a library_src node named 'transformation_utils_lib'
        was imported as 'from transformation_utils import X' (missing the
        suffix) -- exact matching silently skipped this entirely, since
        'transformation_utils' != 'transformation_utils_lib'. The model
        commonly imports using the shorter, more natural-sounding base name
        even when the actual node carries a _lib/_module suffix.
        """
        if imported_name in lib_node_ids:
            return imported_name, "library_src"
        if imported_name in module_node_ids:
            return imported_name, "module"
        for suffix in ("_lib", "_module"):
            candidate = imported_name + suffix
            if candidate in lib_node_ids:
                return candidate, "library_src"
            if candidate in module_node_ids:
                return candidate, "module"
        return None

    # Common pyspark.sql.functions names worth checking for bare, unimported use.
    PYSPARK_FUNCTIONS = {
        "col", "sum", "avg", "count", "max", "min", "when", "lit", "window",
        "current_timestamp", "current_date", "date_format", "to_date", "concat",
        "coalesce", "round", "cast", "row_number", "rank", "dense_rank",
        "first", "last", "collect_list", "collect_set", "explode", "isnull",
        "isnotnull", "regexp_replace", "trim", "upper", "lower", "split",
    }

    for node_id, node_code in plan.get("_node_code", {}).items():
        executable = node_code.get("executable", [])
        fixed = []
        needs_syspath = False
        needs_pip_install = False
        changed = False

        already_imports_functions = any(
            "from pyspark.sql.functions import" in l or "import pyspark.sql.functions" in l
            for l in executable
        )

        for line in executable:
            m = import_re.match(line)
            resolved = _resolve_real_node_id(m.group(2)) if m else None
            if resolved:
                real_node_id, role = resolved
                if role == "library_src":
                    fixed.append(f"{m.group(1)}dp_synth_lib.{real_node_id}{m.group(3)}")
                    needs_pip_install = True
                else:
                    fixed.append(f"{m.group(1)}{real_node_id}{m.group(3)}"
                                 if real_node_id != m.group(2) else line)
                    needs_syspath = True
                changed = True
            else:
                fixed.append(line)

        # Detect bare pyspark function calls not already imported.
        if not already_imports_functions:
            used = set()
            for line in fixed:
                for fn in PYSPARK_FUNCTIONS:
                    if re.search(rf"(?<![\.\w]){fn}\s*\(", line):
                        used.add(fn)
            if used:
                fixed = [f"from pyspark.sql.functions import {', '.join(sorted(used))}"] + fixed
                changed = True

        if needs_pip_install and wheel_path:
            # No forced restartPython() here: in a real job run the Python
            # process already starts fresh for that task, so there's nothing
            # stale to restart away. In the entry node specifically, a restart
            # would also wipe the catalog/schema/etc. variables just read from
            # the widgets preamble (which runs before this code), breaking
            # everything downstream. %pip install alone is correct and safe
            # in both job and interactive-testing contexts.
            fixed = [f"%pip install {wheel_path}"] + fixed
            changed = True

        if needs_syspath:
            fixed = ["import sys", f"sys.path.append({notebooks_dir!r})"] + fixed

        if changed:
            plan.setdefault("plan_notes", "")
            plan["plan_notes"] = (
                f"{plan['plan_notes']} fix_python_imports: corrected node {node_id} -- "
                f"dp_synth_lib prefix / sys.path.append / missing pyspark.sql.functions "
                f"import / %pip install wheel, as applicable."
            ).strip()
            node_code["executable"] = fixed
    return plan


def _strip_duplicated_reference_lines(plan: dict) -> dict:
    """Deterministically removes any literal magic_run/dbutils_notebook_run line
    from a node's executable code, AND any literal widget-declaration line when
    the materializer already auto-injects the widgets preamble. Both mechanisms
    are declared once via the plan itself (code_graph.edges for references,
    knobs.param_passing for widgets) and rendered separately -- a literal copy
    in executable code is always a planner duplication, never intentional
    content.

    Promoted from instruction-only (planner.py) to a code guarantee after the
    reference-mechanism instruction failed a second time on a different
    scenario shape, and the widget variant was confirmed live comparing Haiku
    vs Sonnet output on an identical prompt -- Haiku wrote its own widget block
    in addition to the materializer's auto-injected one, and the duplicate was
    itself broken code (assigning .text()'s return instead of calling .get()).
    """
    widgets_mode = plan.get("knobs", {}).get("param_passing") == "widgets"
    for node_id, node_code in plan.get("_node_code", {}).items():
        executable = node_code.get("executable", [])
        cleaned = [line for line in executable
                  if "%run " not in line and "dbutils.notebook.run(" not in line
                  and not (widgets_mode and ("widgets.removeAll" in line or "widgets.text(" in line))]

        # Guard against stripping leaving a `try:` with nothing inside it --
        # confirmed live: the realism-floor instruction (planner.py 2i) told the
        # model to wrap its "core action" in try/except, and it sometimes wraps
        # the %run/dbutils.notebook.run line itself. Once that line is correctly
        # stripped (it's rendered separately via the edge), an empty try: block
        # is a straight SyntaxError -- worse than the duplication it replaced.
        # Insert a harmless `pass` rather than leave invalid Python.
        fixed = []
        for i, line in enumerate(cleaned):
            fixed.append(line)
            stripped = line.strip()
            if stripped.endswith("try:"):
                nxt = cleaned[i + 1].strip() if i + 1 < len(cleaned) else ""
                if nxt.startswith("except") or nxt.startswith("finally") or not nxt:
                    indent = line[:len(line) - len(line.lstrip())] + "    "
                    fixed.append(f"{indent}pass")

        if fixed != executable:
            removed = [l for l in executable if l not in fixed]
            plan.setdefault("plan_notes", "")
            plan["plan_notes"] = (
                f"{plan['plan_notes']} strip_duplicated_reference_lines: removed "
                f"{removed} from node {node_id} -- already rendered via a "
                f"code_graph edge or the widgets preamble, duplicate literal "
                f"line discarded (inserted a placeholder pass if this left an "
                f"empty try block)."
            ).strip()
            node_code["executable"] = fixed
    return plan


def _connect_orphaned_nodes(plan: dict) -> dict:
    """Deterministically wires an edge from the entry node to any node the graph
    left unreachable, rather than only flagging it via graph_well_formed.

    Confirmed as a real, live bug: under multi-feature complexity (3 features
    targeted in one prompt), the planner created a library_src node alongside a
    similarly-named module node (pipeline_utils / pipeline_utils_lib) and only
    wired one of them in. The orphan is real, intended content -- not dead
    weight to discard -- so the fix is to connect it (python_import, since that's
    always a safe/valid mechanism regardless of node role) rather than delete it.
    """
    graph = plan.get("code_graph", {})
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return plan

    entry = next((n["node_id"] for n in nodes if n.get("role") == "entry"), None)
    if not entry:
        return plan

    reachable = {entry}
    frontier = [entry]
    while frontier:
        cur = frontier.pop()
        for e in edges:
            if e["from_node"] == cur and e["to_node"] not in reachable:
                reachable.add(e["to_node"])
                frontier.append(e["to_node"])

    orphaned = [n["node_id"] for n in nodes if n["node_id"] not in reachable]
    if orphaned:
        for node_id in orphaned:
            edges.append({"from_node": entry, "to_node": node_id,
                          "reference_mechanism": "python_import"})
        plan.setdefault("plan_notes", "")
        plan["plan_notes"] = (
            f"{plan['plan_notes']} connect_orphaned_nodes: added python_import edges "
            f"from {entry} to {orphaned} -- these nodes existed in the graph but had "
            f"no edge reaching them."
        ).strip()
        graph["edges"] = edges

    return plan


def materialize(plan: dict, out_dir: str | Path) -> dict:
    out_dir = Path(out_dir)
    (out_dir / "src" / "notebooks").mkdir(parents=True, exist_ok=True)
    (out_dir / "src" / "sql").mkdir(parents=True, exist_ok=True)
    artifacts: list = []
    pid = plan["plan_id"]

    # Build the wheel FIRST, before fixing imports -- _fix_python_imports needs
    # to find the REAL wheel file on disk to write a correct %pip install path.
    # Confirmed as a real ordering bug: when this ran in the opposite order,
    # dist/*.whl didn't exist yet, so the glob found nothing and the
    # %pip install line silently never got added at all.
    whl = _build_wheel(plan, out_dir, artifacts)

    plan = _fix_python_imports(plan, out_dir)
    plan = _strip_duplicated_reference_lines(plan)
    plan = _connect_orphaned_nodes(plan)

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

    # Seed step: only worth generating when there's actually something to
    # seed (assets with real table_physical data) -- a plan with no assets
    # has nothing for a seed step to create.
    include_seed = bool(plan.get("assets")) and any(
        a.get("table_physical") for a in plan["assets"])
    if include_seed:
        seed_rel = "src/notebooks/_seed.py"
        (out_dir / seed_rel).write_text(_render_seed_notebook(plan))
        _stamp(artifacts, "notebook", seed_rel, node_id="_seed")

    yml = _render_bundle_yaml(plan, whl.name if whl else None, include_seed_task=include_seed)
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
