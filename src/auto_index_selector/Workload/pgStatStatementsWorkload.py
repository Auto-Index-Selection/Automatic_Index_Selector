"""
Workload/pgStatStatementsWorkload.py
------------------------------------
Live Workload Loader & Snapshot Differencing using PostgreSQL pg_stat_statements.

Extracts analytical SELECT queries executed on the live database across an
observation window (snap_before -> snap_after), and uses an automated
PlaceholderResolver to substitute normalized query placeholders ($1, $2, ...)
with typed dummy literals so that HypoPG and EXPLAIN (FORMAT JSON) plan the
queries without errors.

Usage in config.toml::

    [workload]
    module = "pgStatStatementsWorkload"
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fallback default schema for standard TPC-H tables
_DEFAULT_TPCH_SCHEMA = {
    "region": {"r_regionkey": "INT", "r_name": "VARCHAR", "r_comment": "VARCHAR"},
    "nation": {"n_nationkey": "INT", "n_name": "VARCHAR", "n_regionkey": "INT", "n_comment": "VARCHAR"},
    "part": {"p_partkey": "INT", "p_name": "VARCHAR", "p_mfgr": "VARCHAR", "p_brand": "VARCHAR", "p_type": "VARCHAR", "p_size": "INT", "p_container": "VARCHAR", "p_retailprice": "DOUBLE", "p_comment": "VARCHAR"},
    "supplier": {"s_suppkey": "INT", "s_name": "VARCHAR", "s_address": "VARCHAR", "s_nationkey": "INT", "s_phone": "VARCHAR", "s_acctbal": "DOUBLE", "s_comment": "VARCHAR"},
    "partsupp": {"ps_partkey": "INT", "ps_suppkey": "INT", "ps_availqty": "INT", "ps_supplycost": "DOUBLE", "ps_comment": "VARCHAR"},
    "customer": {"c_custkey": "INT", "c_name": "VARCHAR", "c_address": "VARCHAR", "c_nationkey": "INT", "c_phone": "VARCHAR", "c_acctbal": "DOUBLE", "c_mktsegment": "VARCHAR", "c_comment": "VARCHAR"},
    "orders": {"o_orderkey": "INT", "o_custkey": "INT", "o_orderstatus": "VARCHAR", "o_totalprice": "DOUBLE", "o_orderdate": "DATE", "o_orderpriority": "VARCHAR", "o_clerk": "VARCHAR", "o_shippriority": "INT", "o_comment": "VARCHAR"},
    "lineitem": {"l_orderkey": "INT", "l_partkey": "INT", "l_suppkey": "INT", "l_linenumber": "INT", "l_quantity": "DOUBLE", "l_extendedprice": "DOUBLE", "l_discount": "DOUBLE", "l_tax": "DOUBLE", "l_returnflag": "VARCHAR", "l_linestatus": "VARCHAR", "l_shipdate": "DATE", "l_commitdate": "DATE", "l_receiptdate": "DATE", "l_shipinstruct": "VARCHAR", "l_shipmode": "VARCHAR", "l_comment": "VARCHAR"},
}


@dataclass
class PgStatEntry:
    queryid: int
    query: str
    calls: int
    total_exec_time: float
    rows: int


@dataclass
class PgStatSnapshot:
    """Snapshot of pg_stat_statements state at a point in time."""
    timestamp: float = field(default_factory=time.time)
    entries: Dict[int, PgStatEntry] = field(default_factory=dict)


class PlaceholderResolver:
    """Substitutes $1, $2, ... placeholders in pg_stat_statements queries with typed dummy literals."""

    def __init__(self, schema: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._schema = schema or _DEFAULT_TPCH_SCHEMA

    def resolve(self, query: str) -> str:
        """Replace all $N parameters with valid PostgreSQL literals."""
        if "$" not in query:
            return query

        resolved = query

        # 1. CAST($N AS date) or CAST($N AS varchar) or CAST($N AS char)
        resolved = re.sub(r"CAST\s*\(\s*\$(\d+)\s+AS\s+date\s*\)", "CAST('1998-01-01' AS date)", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"CAST\s*\(\s*\$(\d+)\s+AS\s+varchar\w*\s*\)", "CAST('A' AS varchar)", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"CAST\s*\(\s*\$(\d+)\s+AS\s+char\w*\s*\)", "CAST('A' AS char)", resolved, flags=re.IGNORECASE)

        # 2. date $N, INTERVAL $N, INTERVAL '$N'
        resolved = re.sub(r"\bdate\s+\$(\d+)", "date '1998-01-01'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bINTERVAL\s+\$(\d+)", "INTERVAL '1 day'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bINTERVAL\s+'\$(\d+)'", "INTERVAL '1 day'", resolved, flags=re.IGNORECASE)

        # 3. LIKE / NOT LIKE / ILIKE / NOT ILIKE is ALWAYS followed by a string pattern
        resolved = re.sub(
            r'(\bNOT\s+LIKE|\bLIKE|\bNOT\s+ILIKE|\bILIKE)\s*\$(\d+)',
            r"\1 'A%'",
            resolved,
            flags=re.IGNORECASE
        )

        # 4. Resolve string/varchar expressions e.g. col = $1 or col <> $1
        resolved = re.sub(
            r'(\w*(?:name|mode|flag|status|comment|segment|priority|type|instruct|address|phone|brand|container|clerk)\w*)\s*([=><!]+)\s*\$(\d+)',
            r"\1 \2 'A'",
            resolved,
            flags=re.IGNORECASE
        )

        # 5. Resolve date expressions e.g. col <= $1 or col >= $1 or col = $1
        resolved = re.sub(
            r'(\w*(?:date|time)\w*)\s*([=><!]+|BETWEEN|\bIS\b)\s*\$(\d+)',
            r"\1 \2 '1998-01-01'",
            resolved,
            flags=re.IGNORECASE
        )

        # 6. Handle multi-item IN ($1, $2, ...) and NOT IN ($1, $2, ...)
        def _repl_in(m):
            col, op, inside = m.group(1), m.group(2), m.group(3)
            is_str = bool(re.search(r'(name|mode|flag|status|comment|segment|priority|type|instruct|address|phone|brand|container|clerk)', col, re.I))
            is_date = bool(re.search(r'(date|time)', col, re.I))
            val = "'A'" if is_str else ("'1998-01-01'" if is_date else "1")
            new_inside = re.sub(r'\$\d+', val, inside)
            return f"{col} {op} ({new_inside})"

        resolved = re.sub(
            r'(\w+)\s*(\bIN\b|\bNOT\s+IN\b)\s*\(([^)]+)\)',
            _repl_in,
            resolved,
            flags=re.IGNORECASE
        )

        # 7. Resolve float/price/discount/tax/quantity/balance expressions
        resolved = re.sub(
            r'(\w*(?:price|discount|tax|qty|quantity|balance|acctbal|cost)\w*)\s*([=><!]+)\s*\$(\d+)',
            r"\1 \2 1.0",
            resolved,
            flags=re.IGNORECASE
        )

        # 8. Resolve integer/key/id expressions
        resolved = re.sub(
            r'(\w*(?:key|id|num|number|size|count|days)\w*)\s*([=><!]+)\s*\$(\d+)',
            r"\1 \2 1",
            resolved,
            flags=re.IGNORECASE
        )

        # 9. Resolve LIMIT / OFFSET $N
        resolved = re.sub(
            r'(LIMIT|OFFSET)\s*\$(\d+)',
            r"\1 10",
            resolved,
            flags=re.IGNORECASE
        )

        # 10. Fallback: replace any remaining $N with integer 1
        resolved = re.sub(r'\$\d+', '1', resolved)

        return resolved


def fetch_live_schema(conn) -> Dict[str, Dict[str, str]]:
    """Fetch table and column schema mapping from PostgreSQL information_schema."""
    schema: Dict[str, Dict[str, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position;
            """)
            for table_name, column_name, data_type in cur.fetchall():
                schema.setdefault(table_name, {})[column_name] = data_type.upper()
    except Exception as e:
        logger.warning("Could not fetch information_schema: %s. Using default schema.", e)
        if conn:
            conn.rollback()
        return _DEFAULT_TPCH_SCHEMA

    return schema if schema else _DEFAULT_TPCH_SCHEMA


def take_snapshot(conn) -> PgStatSnapshot:
    """Capture a point-in-time snapshot of pg_stat_statements entries."""
    snapshot = PgStatSnapshot()
    if conn is None:
        return snapshot

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT queryid, query, calls, total_exec_time, rows
                FROM pg_stat_statements
                WHERE query NOT ILIKE '%pg_stat%'
                  AND query NOT ILIKE '%hypopg%'
                  AND query NOT ILIKE '%advisor%'
                  AND query NOT ILIKE '%information_schema%'
                  AND query NOT ILIKE '%pg_settings%'
                  AND query NOT ILIKE '%pg_catalog%'
                  AND query NOT ILIKE '%current_setting%'
                  AND query ILIKE 'SELECT%'
                  AND query NOT ILIKE 'SELECT 1%';
            """)
            for qid, qtext, calls, total_time, rows in cur.fetchall():
                snapshot.entries[int(qid)] = PgStatEntry(
                    queryid=int(qid),
                    query=qtext.strip(),
                    calls=int(calls),
                    total_exec_time=float(total_time),
                    rows=int(rows)
                )
    except Exception as e:
        logger.warning(
            "[pg_stat_statements] Could not query pg_stat_statements: %s\n"
            "  -> Ensure 'pg_stat_statements' is in shared_preload_libraries in postgresql.conf and PostgreSQL was restarted.",
            e
        )
        conn.rollback()

    return snapshot


def get_delta_workload(
    conn,
    snap_before: PgStatSnapshot,
    snap_after: PgStatSnapshot,
) -> Tuple[List[str], Dict[str, Dict[str, str]], Dict[str, float]]:
    """Compute query deltas across the observation window and return active queries.

    Queries are ordered by execution time delta (or call count delta) occurring strictly
    during the observation window. Returns ALL queries with non-zero execution deltas.
    """
    schema = fetch_live_schema(conn)
    resolver = PlaceholderResolver(schema=schema)

    # Calculate delta for each query
    deltas: List[Tuple[float, int, str]] = []  # (delta_time, delta_calls, query)

    for qid, after_entry in snap_after.entries.items():
        before_entry = snap_before.entries.get(qid)
        delta_calls = after_entry.calls - (before_entry.calls if before_entry else 0)
        delta_time = after_entry.total_exec_time - (before_entry.total_exec_time if before_entry else 0.0)

        if delta_calls > 0:
            deltas.append((delta_time, delta_calls, after_entry.query))

    # If queries were executed during window, rank them by delta_time DESC
    # Extract queries and attach execution frequency weights (delta_calls)
    queries: List[str] = []
    query_weights: Dict[str, float] = {}

    if deltas:
        deltas.sort(key=lambda x: -x[0])
        for _, calls, q in deltas:
            resolved = resolver.resolve(q)
            queries.append(resolved)
            query_weights[resolved] = float(max(1, calls))
        logger.info("[pg_stat_statements] Extracted %d active queries from observation window delta", len(queries))
    else:
        # Fallback: if no new calls occurred during window, extract cumulative queries from after-snapshot
        all_entries = sorted(snap_after.entries.values(), key=lambda e: -e.total_exec_time)
        for entry in all_entries:
            resolved = resolver.resolve(entry.query)
            queries.append(resolved)
            query_weights[resolved] = float(max(1, entry.calls))
        logger.info("[pg_stat_statements] Window was quiet; loaded %d cumulative baseline queries", len(queries))

    return queries, schema, query_weights


def getWorkload(conn=None) -> Tuple[List[str], Dict[str, Dict[str, str]], Dict[str, float]]:
    """Single-call fallback interface matching the standard Workload module signature."""
    if conn is None:
        logger.warning("No live connection passed to pgStatStatementsWorkload. Returning default TPC-H schema.")
        return [], _DEFAULT_TPCH_SCHEMA, {}

    snap = take_snapshot(conn)
    return get_delta_workload(conn, PgStatSnapshot(), snap)
