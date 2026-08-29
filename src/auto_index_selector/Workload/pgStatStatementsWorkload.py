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
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
    """Substitutes $1, $2, ... placeholders in pg_stat_statements queries with typed dummy literals.

    Resolution is driven purely by live PostgreSQL schema inspection (information_schema & pg_proc)
    and standard SQL syntax constructs, completely independent of specific column names or workloads.
    """

    def __init__(self, schema: Optional[Dict[str, Any]] = None) -> None:
        self._schema = schema or {}
        # Build flattened column -> datatype map for fast O(1) lookup
        self._col_types: Dict[str, str] = {}
        self._func_types: Dict[str, List[str]] = {}

        for table, cols in self._schema.items():
            if table == "_functions" and isinstance(cols, dict):
                self._func_types = {k.lower(): v for k, v in cols.items()}
            elif isinstance(cols, dict):
                for col, dtype in cols.items():
                    self._col_types[col.lower()] = dtype.upper()

    def _type_to_literal(self, dtype: str, is_like: bool = False, is_array: bool = False) -> str:
        d = dtype.upper()
        if is_array or d.startswith("_") or "ARRAY" in d:
            if any(t in d for t in ("INT", "SERIAL", "NUM", "FLOAT", "DOUBLE", "REAL")):
                return "ARRAY[1]"
            return "ARRAY['A']"

        if is_like:
            return "'A%'"

        if any(t in d for t in ("UUID",)):
            return "'00000000-0000-0000-0000-000000000000'::uuid"
        if any(t in d for t in ("JSONB", "JSON")):
            return "'{}'::jsonb"
        if any(t in d for t in ("BOOL", "BOOLEAN")):
            return "TRUE"
        if any(t in d for t in ("INET", "CIDR", "MACADDR")):
            return "'127.0.0.1'"
        if any(t in d for t in ("DATE",)):
            return "DATE '1998-01-01'"
        if any(t in d for t in ("TIME", "TIMESTAMP")):
            return "TIMESTAMP '1998-01-01 00:00:00'"
        if any(t in d for t in ("INT", "INT2", "INT4", "INT8", "SERIAL", "BIGINT", "SMALLINT")):
            return "1"
        if any(t in d for t in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL", "NUMERIC", "DECIMAL", "MONEY")):
            return "1.0"
        if any(t in d for t in ("CHAR", "VARCHAR", "TEXT", "NAME", "BPCHAR", "CSTRING")):
            return "'A'"
        if any(t in d for t in ("BYTEA",)):
            return "'\\x00'::bytea"

        return "'A'"

    def resolve(self, query: str) -> str:
        """Replace all $N parameters with valid PostgreSQL literals."""
        if "$" not in query:
            return query

        resolved = query

        # -------------------------------------------------------------
        # 1. Explicit Casts: CAST($N AS <type>) and $N::<type>
        # -------------------------------------------------------------
        def _repl_cast(m):
            target_type = m.group(2).strip().lower()
            val = self._type_to_literal(target_type)
            return f"CAST({val} AS {target_type})"

        resolved = re.sub(r"CAST\s*\(\s*\$(\d+)\s+AS\s+([\w\s\[\]]+)\)", _repl_cast, resolved, flags=re.IGNORECASE)

        def _repl_colon_cast(m):
            target_type = m.group(2).strip().lower()
            val = self._type_to_literal(target_type)
            return f"{val}::{target_type}"

        resolved = re.sub(r"\$(\d+)::([\w\s\[\]]+)", _repl_colon_cast, resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 2. Date & Time Specific Syntax
        # -------------------------------------------------------------
        resolved = re.sub(r"\bdate\s+\$(\d+)", "date '1998-01-01'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\btimestamp\s+\$(\d+)", "timestamp '1998-01-01 00:00:00'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bINTERVAL\s+\$(\d+)", "INTERVAL '1 day'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bINTERVAL\s+'\$(\d+)'", "INTERVAL '1 day'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"DATE_TRUNC\s*\(\s*\$(\d+)\s*,", "DATE_TRUNC('day',", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 3. Full-Text Search Functions
        # -------------------------------------------------------------
        resolved = re.sub(r"\b(to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(\s*\$(\d+)\s*\)", r"\1('a')", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bto_tsvector\s*\(\s*\$(\d+)\s*\)", "to_tsvector('a')", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"@@\s*\$(\d+)", "@@ to_tsquery('a')", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 4. JSON / JSONB Operators (->, ->>, ?, ?|, ?&, @>, <@)
        # -------------------------------------------------------------
        resolved = re.sub(r"(->>|->|\?|\?\||\?&)\s*\$(\d+)", r"\1 'key'", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"(@>|<@)\s*\$(\d+)", r"\1 '{}'::jsonb", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 5. Array Operators (= ANY($N), $N = ANY(col), UNNEST($N), ARRAY containment)
        # -------------------------------------------------------------
        resolved = re.sub(r"(=\s*ANY\s*\(|IN\s*\(ARRAY)\s*\$(\d+)\s*\)", r"= ANY(ARRAY[1])", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\$(\d+)\s*=\s*ANY\s*\(\s*(\w+)\s*\)", r"1 = ANY(\2)", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r"\bUNNEST\s*\(\s*\$(\d+)\s*\)", "UNNEST(ARRAY[1])", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 6. Schema & Catalog Function Calls: func_name($1, $2, ...)
        # -------------------------------------------------------------
        _SQL_KEYWORDS = {"in", "exists", "any", "all", "select", "from", "where", "and", "or", "values", "not", "case", "when", "then", "else", "end", "with", "having", "group", "order"}

        def _repl_func(m):
            func_name, inside = m.group(1), m.group(2)
            if func_name.lower() in _SQL_KEYWORDS or re.search(r'\bSELECT\b', inside, re.I):
                return m.group(0)
            arg_types = self._func_types.get(func_name.lower())
            if arg_types:
                parts = [p.strip() for p in inside.split(',')]
                new_parts = []
                for i, part in enumerate(parts):
                    if '$' in part and i < len(arg_types):
                        lit = self._type_to_literal(arg_types[i])
                        new_parts.append(re.sub(r'\$\d+', lit, part))
                    else:
                        new_parts.append(part)
                return f"{func_name}({', '.join(new_parts)})"
            return m.group(0)

        resolved = re.sub(r'(\b[a-zA-Z_]\w*)\s*\(([^()]*\$\d+[^()]*)\)', _repl_func, resolved)

        # -------------------------------------------------------------
        # 7. Schema-Driven BETWEEN: (table.)col BETWEEN $1 AND $2
        # -------------------------------------------------------------
        def _repl_between(m):
            col = m.group(1)
            col_clean = col.split(".")[-1].lower()
            dtype = self._col_types.get(col_clean, "INT")
            v1 = self._type_to_literal(dtype)
            v2 = self._type_to_literal(dtype)
            return f"{col} BETWEEN {v1} AND {v2}"

        resolved = re.sub(
            r'(\w+(?:\.\w+)?)\s+BETWEEN\s+\$(\d+)\s+AND\s+\$(\d+)',
            _repl_between,
            resolved,
            flags=re.IGNORECASE
        )

        # -------------------------------------------------------------
        # 8. Pattern Matching: LIKE, ILIKE, SIMILAR TO, Regex (~, ~*)
        # -------------------------------------------------------------
        resolved = re.sub(
            r'(\bNOT\s+LIKE|\bLIKE|\bNOT\s+ILIKE|\bILIKE|\bSIMILAR\s+TO)\s*\$(\d+)',
            r"\1 'A%'",
            resolved,
            flags=re.IGNORECASE
        )
        resolved = re.sub(r'(!~|!~\*|~|~\*)\s*\$(\d+)', r"\1 '^A'", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 9. Schema-Driven Multi-item IN / NOT IN lists: (table.)col IN ($1, $2, ...) (excluding subqueries)
        # -------------------------------------------------------------
        def _repl_in(m):
            col, op, inside = m.group(1), m.group(2), m.group(3)
            # If inside parentheses is a subquery (contains SELECT), leave it to inner column resolution
            if re.search(r'\bSELECT\b', inside, re.I):
                return m.group(0)
            col_clean = col.split(".")[-1].lower()
            dtype = self._col_types.get(col_clean, "INT")
            val = self._type_to_literal(dtype)
            new_inside = re.sub(r'\$\d+', val, inside)
            return f"{col} {op} ({new_inside})"

        resolved = re.sub(
            r'(\w+(?:\.\w+)?)\s*(\bIN\b|\bNOT\s+IN\b)\s*\(([^)]+)\)',
            _repl_in,
            resolved,
            flags=re.IGNORECASE
        )

        # -------------------------------------------------------------
        # 10. Schema-Driven Direct Column Comparisons: (table.)col [=><!] $N and $N [=><!] (table.)col
        # -------------------------------------------------------------
        def _repl_schema_col_right(m):
            col, op = m.group(1), m.group(2)
            col_clean = col.split(".")[-1].lower()
            dtype = self._col_types.get(col_clean)
            if dtype:
                val = self._type_to_literal(dtype)
                return f"{col} {op} {val}"
            return m.group(0)

        resolved = re.sub(
            r'(\b[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)\s*([=><!]+|IS\s+NOT|IS)\s*\$(\d+)',
            _repl_schema_col_right,
            resolved,
            flags=re.IGNORECASE
        )

        def _repl_schema_col_left(m):
            op, col = m.group(2), m.group(3)
            col_clean = col.split(".")[-1].lower()
            dtype = self._col_types.get(col_clean)
            if dtype:
                val = self._type_to_literal(dtype)
                return f"{val} {op} {col}"
            return m.group(0)

        resolved = re.sub(
            r'\$(\d+)\s*([=><!]+)\s*(\b[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?)',
            _repl_schema_col_left,
            resolved,
            flags=re.IGNORECASE
        )

        # -------------------------------------------------------------
        # 11. Pagination & Limits
        # -------------------------------------------------------------
        resolved = re.sub(r'\bLIMIT\s*\$(\d+)', "LIMIT 10", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r'\bOFFSET\s*\$(\d+)', "OFFSET 0", resolved, flags=re.IGNORECASE)
        resolved = re.sub(r'\bFETCH\s+FIRST\s+\$(\d+)\s+ROWS\s+ONLY', "FETCH FIRST 10 ROWS ONLY", resolved, flags=re.IGNORECASE)

        # -------------------------------------------------------------
        # 12. Safe Final Fallback
        # -------------------------------------------------------------
        resolved = re.sub(r'\$\d+', '1', resolved)

        return resolved


def fetch_live_schema(conn) -> Dict[str, Any]:
    """Fetch table/column schema and function signatures from PostgreSQL catalogs."""
    schema: Dict[str, Any] = {}
    if conn is None:
        return schema

    try:
        with conn.cursor() as cur:
            # 1. Fetch all table column types
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position;
            """)
            for table_name, column_name, data_type in cur.fetchall():
                schema.setdefault(table_name, {})[column_name] = data_type.upper()

            # 2. Fetch function argument signatures from pg_proc
            cur.execute("""
                SELECT 
                    p.proname,
                    COALESCE(
                        array_to_string(
                            ARRAY(
                                SELECT t.typname 
                                FROM unnest(p.proargtypes) WITH ORDINALITY AS a(oid, ord)
                                JOIN pg_type t ON t.oid = a.oid
                                ORDER BY a.ord
                            ),
                            ','
                        ),
                        ''
                    ) AS arg_types
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname IN ('public', 'pg_catalog')
                  AND p.pronargs > 0;
            """)
            func_dict = {}
            for proname, arg_types_str in cur.fetchall():
                if arg_types_str:
                    func_dict[proname.lower()] = [t.strip().upper() for t in arg_types_str.split(",") if t.strip()]
            schema["_functions"] = func_dict
    except Exception as e:
        logger.warning("Could not fetch database schema / pg_proc: %s", e)
        if conn:
            conn.rollback()
        return {}

    return schema


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

    queries: List[str] = []
    query_weights: Dict[str, float] = {}

    # Extract all queries executed with positive call deltas during the window
    for qid, after_entry in snap_after.entries.items():
        before_entry = snap_before.entries.get(qid)
        if before_entry is None or after_entry.calls < before_entry.calls:
            delta_calls = after_entry.calls
        else:
            delta_calls = after_entry.calls - before_entry.calls

        if delta_calls > 0:
            resolved = resolver.resolve(after_entry.query)
            if resolved not in query_weights:
                queries.append(resolved)
                query_weights[resolved] = 0.0
            query_weights[resolved] += float(delta_calls)

    if queries:
        logger.info("[pg_stat_statements] Extracted %d active queries from observation window delta", len(queries))
    else:
        logger.info("[pg_stat_statements] No queries executed during observation window (0 delta queries)")

    return queries, schema, query_weights


def getWorkload(conn=None) -> Tuple[List[str], Dict[str, Dict[str, str]], Dict[str, float]]:
    """Single-call fallback interface matching the standard Workload module signature."""
    if conn is None:
        logger.warning("No live connection passed to pgStatStatementsWorkload.")
        return [], {}, {}

    snap = take_snapshot(conn)
    return get_delta_workload(conn, PgStatSnapshot(), snap)
