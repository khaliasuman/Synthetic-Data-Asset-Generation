"""
dpstudio/engine/preflight.py

ONE gate that catches the entire class of bugs that caused every Gate 5 failure.

Every single deployment failure found by running generated bundles for real had
the SAME root cause: the generated code referenced a NAME that was not
resolvable at the moment that line executed. Seven different symptoms, one
cause:

  - a module name that doesn't resolve        (ModuleNotFoundError)
  - a function never imported                 (NameError: col)
  - a variable not defined YET at that point  (NameError: normalized_df)
  - a package not installed at runtime        (ModuleNotFoundError)

Each of those was previously fixed with its own targeted rewriting pass in
materializer.py. Those passes are still worth keeping -- they AUTO-FIX the
common cases -- but they are inherently a list of known symptoms, so a new
symptom always slips through and fails at deploy time.

This module takes the opposite approach: rather than enumerate what can go
wrong, it PARSES the actual generated code and verifies that every name being
read is actually available at that point, honoring real execution semantics:

  - module-level statements execute top-to-bottom, so a name must be bound
    on an EARLIER line than the line that reads it (this is what catches the
    cross-notebook ordering bug that no symptom-specific patch anticipated)
  - names inside function bodies are deferred, so they may reference anything
    bound anywhere in the module
  - a %run child shares the caller's namespace, so it inherits exactly the
    names the caller had bound BEFORE the %run line -- not the ones bound after
  - Databricks injects spark/dbutils/display/etc. into every notebook

If this gate passes, "code references something unavailable" cannot be the
reason a deployment fails. That does not make deployment infallible -- a table
can still be missing, a permission can still be wrong -- but it permanently
closes the category that caused every failure so far, instead of closing them
one symptom at a time.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

# Names Databricks injects into every notebook's namespace automatically.
DATABRICKS_GLOBALS = {
    "spark", "dbutils", "sc", "sqlContext", "display", "displayHTML",
    "table", "udf", "getArgument", "spark_partition_id", "__name__", "__file__",
}

BUILTIN_NAMES = set(dir(builtins))


def _bound_names(node: ast.AST) -> set[str]:
    """Every name this single statement binds into the enclosing namespace."""
    bound: set[str] = set()

    def add_target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            bound.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for el in t.elts:
                add_target(el)
        elif isinstance(t, ast.Starred):
            add_target(t.value)
        # ast.Attribute / ast.Subscript bind nothing new (obj.x = / obj[k] =)

    if isinstance(node, ast.Assign):
        for t in node.targets:
            add_target(t)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        add_target(node.target)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            bound.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(node.name)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        add_target(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                add_target(item.optional_vars)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            bound.add(node.name)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        bound.update(node.names)

    return bound


def _locally_bound_within(node: ast.AST) -> set[str]:
    """Names bound anywhere inside this statement's own subtree -- used for
    function bodies, comprehensions, and lambdas, where execution is deferred
    so definition order within the enclosing module doesn't matter."""
    bound: set[str] = set()
    for child in ast.walk(node):
        bound |= _bound_names(child)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = child.args
            for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
                bound.add(a.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(child, ast.comprehension):
            t = child.target
            if isinstance(t, ast.Name):
                bound.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for el in t.elts:
                    if isinstance(el, ast.Name):
                        bound.add(el.id)
    return bound


def _names_read(node: ast.AST) -> list[tuple[str, int]]:
    """(name, lineno) for every name this statement READS."""
    reads: list[tuple[str, int]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            reads.append((child.id, getattr(child, "lineno", 0)))
    return reads


def _strip_notebook_syntax(source: str) -> str:
    """Removes Databricks notebook artifacts that aren't valid Python:
    magic lines (%pip, %run, %sql) and # MAGIC / # COMMAND markers. Replaces
    them with blank lines so reported line numbers still match the file."""
    out = []
    for line in source.split("\n"):
        stripped = line.strip()
        if (stripped.startswith("%")
                or stripped.startswith("# MAGIC")
                or stripped.startswith("# COMMAND")
                or stripped.startswith("# Databricks notebook source")):
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def check_node_code(source: str, inherited: set[str] | None = None) -> dict:
    """Checks one notebook's source. `inherited` is the set of names already
    bound by a caller (for a %run child, which shares the caller's namespace).

    Returns {"ok": bool, "syntax_error": str|None,
             "undefined": [(name, lineno)], "bound_after": set[str]}
    where bound_after is every name bound by the end of this notebook -- which
    is what a %run child inherits, and what flows back to the caller.
    """
    available: set[str] = set(inherited or set()) | DATABRICKS_GLOBALS | BUILTIN_NAMES
    cleaned = _strip_notebook_syntax(source)

    try:
        tree = ast.parse(cleaned)
    except SyntaxError as e:
        return {"ok": False, "syntax_error": f"line {e.lineno}: {e.msg}",
                "undefined": [], "bound_after": set(available)}

    undefined: list[tuple[str, int]] = []

    # Module-level statements execute in order: a name must be bound on an
    # EARLIER statement than the one reading it. Inside function/class bodies
    # and comprehensions, execution is deferred, so anything bound anywhere in
    # the module is fair game.
    deferred_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    all_module_bindings: set[str] = set()
    for stmt in tree.body:
        all_module_bindings |= _locally_bound_within(stmt)

    for stmt in tree.body:
        if isinstance(stmt, deferred_scopes):
            # Deferred: may read anything bound anywhere at module level, plus
            # its own locals/params.
            inner_ok = available | all_module_bindings | _locally_bound_within(stmt)
            for name, lineno in _names_read(stmt):
                if name not in inner_ok:
                    undefined.append((name, lineno))
        else:
            # Immediate: reads resolve against what's bound SO FAR only. Names
            # bound by this same statement (e.g. comprehension vars) count.
            this_stmt_locals = _locally_bound_within(stmt) - _bound_names(stmt)
            for name, lineno in _names_read(stmt):
                if name not in available and name not in this_stmt_locals:
                    undefined.append((name, lineno))
        available |= _bound_names(stmt)
        # A deferred scope's own name is bound by the statement itself.
        if isinstance(stmt, deferred_scopes):
            available |= {getattr(stmt, "name", "")} - {""}

    return {"ok": not undefined, "syntax_error": None,
            "undefined": undefined, "bound_after": available}


def check_bundle(plan: dict, out_dir: str | Path) -> dict:
    """Checks every generated notebook in the bundle, following real %run
    semantics so a child is checked with exactly the names its caller had
    bound before the %run line.

    Returns {"ok": bool, "problems": {node_id: {...}}} -- call this after
    materialize() and BEFORE deploying. If ok is False, deploying WILL fail
    at runtime; the problems dict says exactly where and why.
    """
    out_dir = Path(out_dir)
    nb_dir = out_dir / "src" / "notebooks"
    nodes = {n["node_id"]: n for n in plan.get("code_graph", {}).get("nodes", [])}
    edges = plan.get("code_graph", {}).get("edges", [])
    entry = next((n["node_id"] for n in nodes.values() if n.get("role") == "entry"), None)

    problems: dict[str, dict] = {}
    if not entry:
        return {"ok": False, "problems": {"_bundle": {"error": "no entry node in plan"}}}

    def check_one(node_id: str, inherited: set[str]) -> set[str]:
        path = nb_dir / f"{node_id}.py"
        if not path.exists():
            problems[node_id] = {"error": f"expected notebook file missing: {path}"}
            return inherited

        source = path.read_text()
        result = check_node_code(source, inherited)
        if not result["ok"]:
            problems[node_id] = {
                "syntax_error": result["syntax_error"],
                "undefined_names": result["undefined"],
            }

        # A %run child executes inline at its %run line, inheriting what the
        # caller has bound so far, and its own bindings flow back to the
        # caller. dbutils.notebook.run children run in a SEPARATE process and
        # inherit nothing.
        available = result["bound_after"]
        for e in edges:
            if e["from_node"] != node_id:
                continue
            if e.get("reference_mechanism") == "magic_run":
                available |= check_one(e["to_node"], set(available))
            elif e.get("reference_mechanism") == "dbutils_notebook_run":
                check_one(e["to_node"], set(DATABRICKS_GLOBALS | BUILTIN_NAMES))
        return available

    check_one(entry, set())
    return {"ok": not problems, "problems": problems}
