"""
dpstudio/engine/data_contract.py

Closes the DATA CONTRACT failure category: generated business logic reads
tables and columns that the seed step never created.

Previously the seed step invented generic tables (synth_table_0) with generic
columns (row_id, user_id, event_ts, value), while the generated code read
business-plausible names it made up independently (orders, order_date,
amount). Those two sets never matched, so every deployment that got past name
resolution was guaranteed to fail with TABLE_OR_VIEW_NOT_FOUND, and then
UNRESOLVED_COLUMN right behind it.

Same philosophy that made the preflight gate work: don't guess, and don't
enumerate symptoms -- read the actual generated code and derive the required
data shape from it. Whatever the model invented, the seed creates exactly
that.

Two ideas do the work:
  - a table the code READS must exist before the job runs (seed it)
  - a table the code only WRITES is created by the job itself (skip it)
Column types are inferred from naming conventions, because a synthetic
`amount` that arrives as a string breaks the moment business logic multiplies
it.
"""
from __future__ import annotations

import re

# Table references. Handles f-string catalog/schema interpolation and plain
# literals, in both DataFrame and SQL form.
_READ_PATTERNS = [
    re.compile(r"spark\.read\.table\(\s*f?['\"]([^'\"]+)['\"]"),
    re.compile(r"spark\.table\(\s*f?['\"]([^'\"]+)['\"]"),
    re.compile(r"\bFROM\s+([A-Za-z_{}][\w{}.]*)", re.IGNORECASE),
    re.compile(r"\bJOIN\s+([A-Za-z_{}][\w{}.]*)", re.IGNORECASE),
]
_WRITE_PATTERNS = [
    re.compile(r"\.saveAsTable\(\s*f?['\"]([^'\"]+)['\"]"),
    re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_{}][\w{}.]*)",
               re.IGNORECASE),
    re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_{}][\w{}.]*)", re.IGNORECASE),
]

# Column references, in the forms the planner actually emits.
_COLUMN_PATTERNS = [
    re.compile(r"\bcol\(\s*['\"](\w+)['\"]\s*\)"),
    re.compile(r"\.withColumn\(\s*['\"](\w+)['\"]"),
    re.compile(r"\.filter\(\s*(?:col\()?\s*['\"](\w+)['\"]"),
    re.compile(r"\.groupBy\(\s*['\"](\w+)['\"]"),
    re.compile(r"\.orderBy\(\s*['\"](\w+)['\"]"),
    re.compile(r"\.select\(([^)]*)\)"),
    re.compile(r"\b\w*_?df\.(\w+)\b"),
]

# Names that look like columns but are DataFrame/Spark API calls, not data.
_NOT_COLUMNS = {
    "write", "read", "format", "mode", "option", "options", "save", "load",
    "saveAsTable", "select", "filter", "where", "withColumn", "groupBy",
    "orderBy", "agg", "join", "count", "collect", "show", "printSchema",
    "distinct", "drop", "dropDuplicates", "limit", "alias", "cache", "persist",
    "createOrReplaceTempView", "toPandas", "schema", "columns", "rdd", "na",
    "fillna", "dropna", "union", "unionByName", "repartition", "coalesce",
    "sort", "head", "first", "take", "isNotNull", "isNull", "cast", "sparkSession",
}


def _last_segment(name: str) -> str:
    """orders  |  {catalog}.{schema}.orders  |  main.default.orders  ->  orders"""
    return name.split(".")[-1].strip("{}` ")


def _infer_sql_type(column: str) -> str:
    """Type from naming convention. Getting this wrong is not cosmetic: a
    synthetic `amount` typed as STRING breaks the first arithmetic that
    touches it, which is exactly the kind of runtime failure this module
    exists to prevent."""
    c = column.lower()
    if c.endswith(("_ts", "_timestamp")) or c in ("event_ts", "timestamp", "created_at", "updated_at"):
        return "TIMESTAMP"
    if "date" in c:
        return "DATE"
    if c.endswith("_id") or c == "id":
        return "BIGINT"
    if any(k in c for k in ("amount", "price", "cost", "total", "value", "rate", "pct", "percent")):
        return "DOUBLE"
    if any(k in c for k in ("count", "qty", "quantity", "num", "number")):
        return "BIGINT"
    if c.startswith(("is_", "has_")) or c.endswith("_flag"):
        return "BOOLEAN"
    return "STRING"


def _synthetic_value(column: str, sql_type: str) -> str:
    """A generator expression producing plausible non-null data of that type.
    Non-null matters: generated code frequently filters on isNotNull, and an
    all-null column silently produces an empty DataFrame and a confusing
    downstream failure rather than an obvious one."""
    if sql_type == "TIMESTAMP":
        return "current_timestamp() - make_interval(0, 0, 0, cast(rand() * 90 as int), 0, 0, 0)"
    if sql_type == "DATE":
        return "current_date() - cast(rand() * 90 as int)"
    if sql_type == "BIGINT":
        return "cast(rand() * 100000 as bigint)"
    if sql_type == "DOUBLE":
        return "round(cast(rand() * 1000 as double), 2)"
    if sql_type == "BOOLEAN":
        return "rand() > 0.5"
    return f"concat('{column}_', cast(cast(rand() * 1000 as int) as string))"


def extract_data_contract(plan: dict) -> dict:
    """Scans every node's generated code and returns:
        {"read_tables": [names], "write_tables": [names], "columns": [names]}

    read_tables are what the seed must create. write_tables are produced by
    the job itself, so seeding them would be wrong -- it would mask a real
    failure by pre-creating something the job was supposed to create.
    """
    all_code_lines: list[str] = []
    for node_code in plan.get("_node_code", {}).values():
        all_code_lines.extend(node_code.get("executable", []))

    # Python import statements must be excluded BEFORE SQL pattern matching:
    # `from pyspark.sql.functions import col` otherwise matches the SQL
    # "FROM <table>" pattern (case-insensitively) and invents a phantom table
    # named `functions`, which the seed would then dutifully create. Confirmed
    # by this module's own test before it ever shipped.
    sql_scannable = [
        l for l in all_code_lines
        if not re.match(r"^\s*(from\s+[\w.]+\s+import|import\s+)", l)
    ]
    blob = "\n".join(sql_scannable)
    column_blob = "\n".join(all_code_lines)

    read_tables, write_tables = set(), set()
    for pat in _READ_PATTERNS:
        for m in pat.findall(blob):
            read_tables.add(_last_segment(m))
    for pat in _WRITE_PATTERNS:
        for m in pat.findall(blob):
            write_tables.add(_last_segment(m))

    columns: set[str] = set()
    for pat in _COLUMN_PATTERNS:
        for m in pat.findall(column_blob):
            for candidate in re.findall(r"['\"]?(\w+)['\"]?", m):
                if (candidate and candidate not in _NOT_COLUMNS
                        and not candidate.isdigit() and len(candidate) > 1):
                    columns.add(candidate)

    # A table that is both read and written (read-modify-write) still needs
    # seeding -- the read happens first.
    read_tables -= {t for t in write_tables if t not in read_tables}
    read_tables = {t for t in read_tables if t and not t.startswith("{")}

    return {
        "read_tables": sorted(read_tables),
        "write_tables": sorted(write_tables - read_tables),
        "columns": sorted(columns),
    }


def render_seed_sql(contract: dict, row_count: int = 10000) -> list[str]:
    """Emits the CREATE TABLE ... AS SELECT statements that make the contract
    real: every table the code reads, with every column the code references,
    typed plausibly and populated with non-null synthetic rows."""
    columns = contract["columns"] or ["id", "value"]
    statements: list[str] = []

    for table in contract["read_tables"]:
        select_parts = []
        for c in columns:
            t = _infer_sql_type(c)
            select_parts.append(f"    cast({_synthetic_value(c, t)} as {t}) AS {c}")
        select_body = ",\n".join(select_parts)
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.{table} AS\n"
            f"  SELECT\n{select_body}\n"
            f"  FROM range({row_count})"
        )
    return statements
