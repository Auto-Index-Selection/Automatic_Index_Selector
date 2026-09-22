"""
Workload/queryLoggerWorkload.py
-------------------------------
Extracts read workload from PostgreSQL ``query_logger`` extension JSONL logs.

The ``query_logger`` C extension hooks ``ExecutorStart``/``ExecutorEnd`` and
captures every executed query with its **actual runtime parameter values and
type OIDs** into a lock-free shared-memory MPSC ring buffer. A dedicated
background worker flushes events to a JSONL log file.

Key features:
1. Auto-detection of log file path from PostgreSQL GUCs, with override option.
2. In-place log truncation before observation (with fallback to offset tracking).
3. Resilient reading (direct file access or ``pg_read_file`` SQL fallback).
4. `CapturedQuery` subclasses `str` for 100% compatibility with SQL parsers,
   candidate generators, and dictionary keys while retaining parameter metadata.
5. Live schema introspection for candidate generation.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .pg_types import convert_param_value, resolve_type_oid

logger = logging.getLogger(__name__)

# Internal / system query prefixes to exclude from the read workload.
_EXCLUDE_PREFIXES = (
    "SELECT hypopg",
    "SELECT * FROM hypopg",
    "SELECT pg_stat_clear_snapshot",
    "SELECT query_logger",
    "SELECT advisor",
    "SELECT * FROM advisor",
)

# Query text substrings that mark internal catalog queries.
_EXCLUDE_SUBSTRINGS = (
    "pg_stat_statements",
    "pg_stat_user_tables",
    "information_schema",
    "pg_catalog",
    "pg_class",
    "pg_type",
    "pg_proc",
    "pg_index",
    "pg_attribute",
    "pg_stats",
    "pg_namespace",
    "pg_database",
    "advisor_get_column_set_stats",
    "advisor_write_stats",
    "pg_stat_file",
    "query_logger_stats",
    "query_logger_reset_stats",
    "copy (",
    "query_logger.log",
)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class QueryParam:
    """A single resolved query parameter."""
    position: int       # 1-based ($1, $2, ...)
    pg_type: str        # canonical type name ("int4", "float8", "text", ...)
    value: Any          # Python-native value (int, float, str, bool, None)


class CapturedQuery(str):
    """
    A SQL query string with attached query_logger execution metadata.

    Subclasses str so it can be passed directly to SQL parsers (sqlglot),
    candidate generators, and dictionary keys without any type errors.
    """
    params: List[QueryParam]
    duration_ms: float
    rows: int

    def __new__(
        cls,
        query: str,
        params: Optional[List[QueryParam]] = None,
        duration_ms: float = 0.0,
        rows: int = 0,
    ):
        inst = super().__new__(cls, query)
        inst.params = params or []
        inst.duration_ms = duration_ms
        inst.rows = rows
        return inst

    def _param_signature(self) -> tuple:
        return tuple(
            (getattr(p, "position", idx), getattr(p, "pg_type", ""), getattr(p, "value", None))
            for idx, p in enumerate(self.params, start=1)
        )

    def __eq__(self, other) -> bool:
        if isinstance(other, CapturedQuery):
            return str(self) == str(other) and self._param_signature() == other._param_signature()
        if isinstance(other, str):
            return not self.params and str(self) == other
        return False

    def __hash__(self) -> int:
        if not self.params:
            return super().__hash__()
        return hash((super().__hash__(), self._param_signature()))

    def __repr__(self) -> str:
        return f"CapturedQuery({super().__repr__()}, params={self.params})"


@dataclass
class QueryLoggerSnapshot:
    """Point-in-time snapshot of the query_logger state."""
    file_offset: int = 0
    total_events: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExtractedWorkload:
    """Full workload extracted from query_logger log."""
    queries: List[CapturedQuery] = field(default_factory=list)
    query_weights: Dict[str, float] = field(default_factory=dict)
    schema: Dict[str, Dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path Detection & Extension Setup
# ---------------------------------------------------------------------------

def validate_log_path(log_path: Optional[str] = None, conn=None) -> str:
    """
    Validate that the configured query_logger log file exists.

    No default path is assumed and no autodetection is performed.
    The path must be explicitly provided via config.toml under [workload].

    Raises ValueError if log_path is missing or empty.
    Raises FileNotFoundError if the file cannot be verified at the given path.
    """
    if not log_path or not str(log_path).strip():
        raise ValueError(
            "query_logger 'log_file' path must be configured under [workload] in config.toml. "
            "No default path is assumed."
        )

    path_obj = Path(str(log_path).strip()).expanduser()
    resolved_path = str(path_obj.resolve())

    # 1. Direct OS file check
    try:
        if path_obj.is_file():
            return resolved_path
    except OSError:
        pass

    # 2. Database superuser check via pg_stat_file
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT (pg_stat_file(%s)).size;", (resolved_path,))
                row = cur.fetchone()
                if row is not None and row[0] is not None:
                    return resolved_path
        except Exception:
            if conn:
                conn.rollback()

    raise FileNotFoundError(
        f"query_logger log file not found at: '{resolved_path}'. "
        "Please verify that the query_logger extension is running and that "
        "'log_file' in config.toml points to the correct location."
    )


# Backward compatibility alias
detect_log_path = validate_log_path


def setup_query_logger(conn, db_name: str) -> None:
    """Enable query_logger extension specifically for the target database."""
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS query_logger;")
        cur.execute(f"ALTER DATABASE {db_name} SET query_logger.enabled = on;")
    conn.commit()


def truncate_log(log_path: str, conn=None) -> bool:
    """
    Truncate query_logger.log to 0 bytes in-place.
    Tries direct file truncation first; if inaccessible (e.g. file inside $PGDATA
    with restricted permissions), falls back to PostgreSQL's superuser COPY command.
    Returns True if successfully truncated, False if both fail.
    """
    # 1. Try direct OS file truncation
    try:
        with open(log_path, "r+") as f:
            f.truncate(0)
        return True
    except (PermissionError, FileNotFoundError, OSError) as exc:
        logger.debug("Direct file truncation not available for %s: %s", log_path, exc)

    # 2. Database superuser fallback: COPY (SELECT 1 WHERE false) TO '<log_path>'
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("COPY (SELECT 1 WHERE false) TO %s;", (log_path,))
            conn.commit()
            return True
        except Exception as exc:
            if conn:
                conn.rollback()
            logger.warning("Database COPY truncation failed for %s: %s", log_path, exc)

    return False


def reset_stats(conn) -> None:
    """Reset query_logger shared memory telemetry counters."""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT query_logger_reset_stats();")
        conn.commit()
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("Could not reset query_logger stats: %s", exc)


def flush_and_wait(delay: float = 0.3) -> None:
    """Wait for the background worker to drain its ring buffer and flush to disk."""
    time.sleep(delay)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def take_snapshot(conn, log_path: Optional[str] = None) -> QueryLoggerSnapshot:
    """Take a snapshot of current log file position and event telemetry."""
    if conn is None:
        return QueryLoggerSnapshot()

    file_offset = 0
    total_events = 0

    if log_path and os.path.exists(log_path):
        try:
            file_offset = os.path.getsize(log_path)
        except OSError:
            pass

    if file_offset == 0:
        # Try checking size via pg_stat_file
        filename = Path(log_path).name if log_path else "query_logger.log"
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT (pg_stat_file(%s)).size;", (filename,))
                row = cur.fetchone()
                if row and row[0]:
                    file_offset = int(row[0])
        except Exception:
            if conn:
                conn.rollback()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT total_events FROM query_logger_stats();")
            row = cur.fetchone()
            if row:
                total_events = int(row[0])
    except Exception:
        if conn:
            conn.rollback()

    return QueryLoggerSnapshot(file_offset=file_offset, total_events=total_events)


# ---------------------------------------------------------------------------
# Schema Introspection
# ---------------------------------------------------------------------------

def fetch_live_schema(conn) -> Dict[str, Dict[str, str]]:
    """
    Fetch table/column/type metadata from information_schema.

    Returns {table_name: {column_name: DATA_TYPE, ...}, "_functions": {...}}.
    """
    schema: Dict[str, Dict[str, str]] = {}
    if conn is None:
        return schema
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name, UPPER(data_type)
              FROM information_schema.columns
             WHERE table_schema = 'public'
             ORDER BY table_name, ordinal_position;
        """)
        for table, col, dtype in cur.fetchall():
            schema.setdefault(table, {})[col] = dtype

        # Function signatures for candidate generation
        cur.execute("""
            SELECT p.proname,
                   ARRAY_AGG(t.typname ORDER BY idx)
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
         LEFT JOIN LATERAL unnest(p.proargtypes) WITH ORDINALITY AS u(typoid, idx)
                   ON TRUE
         LEFT JOIN pg_type t ON t.oid = u.typoid
             WHERE n.nspname = 'public'
             GROUP BY p.proname;
        """)
        funcs: Dict[str, list] = {}
        for fname, argtypes in cur.fetchall():
            funcs[fname] = [t for t in (argtypes or []) if t is not None]
        schema["_functions"] = funcs  # type: ignore[assignment]

    return schema


# ---------------------------------------------------------------------------
# Log Reading & Parameter Parsing
# ---------------------------------------------------------------------------

def _extract_inner_query(sql: str) -> str:
    """Strip PREPARE wrapper if present, returning the inner query."""
    if not sql:
        return sql
    pattern = (
        r'^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*PREPARE\s+'
        r'(?:"[^"]+"|[a-zA-Z_][a-zA-Z0-9_]*)\s*'
        r'(?:\([^)]*\))?\s+AS\s+(.*)'
    )
    m = re.match(pattern, sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return sql
    inner = m.group(1).strip()
    multi = re.match(r'^(.*?;)\s*(?:EXECUTE|DEALLOCATE)\b.*$', inner,
                     re.IGNORECASE | re.DOTALL)
    if multi:
        inner = multi.group(1).strip()
    return inner


def _is_internal_query(body: str) -> bool:
    """Return True if body looks like an internal / catalog query."""
    upper = body.upper().lstrip()
    for prefix in _EXCLUDE_PREFIXES:
        if upper.startswith(prefix.upper()):
            return True
    lower = body.lower()
    for substr in _EXCLUDE_SUBSTRINGS:
        if substr in lower:
            return True
    return False


def _process_params(
    raw_params: list,
    db_name: Optional[str] = None,
) -> List[QueryParam]:
    """Resolve raw JSONL params list into typed QueryParam objects."""
    params: List[QueryParam] = []
    if not raw_params:
        return params

    for p in sorted(raw_params, key=lambda x: x.get("param", x.get("position", 0))):
        if not isinstance(p, dict):
            continue

        pos = p.get("param", p.get("position", len(params) + 1))
        type_oid = p.get("type")
        type_name = (
            resolve_type_oid(int(type_oid), dbname=db_name, allow_dynamic_lookup=True)
            if type_oid is not None
            else "text"
        )

        is_null = bool(p.get("null", False) or p.get("isnull", False))
        raw_val = p.get("val", p.get("value"))
        value = convert_param_value(type_name, raw_val, is_null=is_null)

        params.append(QueryParam(position=pos, pg_type=type_name, value=value))

    return params


def read_log_content(log_path: str, conn, start_offset: int = 0) -> str:
    """
    Read content from the query_logger log file starting from start_offset.
    Attempts direct file reading, falling back to PostgreSQL's pg_read_file().
    """
    # 1. Direct file reading
    try:
        p = Path(log_path)
        if p.is_file():
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                if start_offset > 0:
                    f.seek(start_offset)
                return f.read()
    except (PermissionError, OSError) as exc:
        logger.debug("Direct file read failed for %s: %s. Trying pg_read_file...", log_path, exc)

    if conn is None:
        return ""

    # 2. Database superuser fallback: pg_read_file
    filename = Path(log_path).name if log_path else "query_logger.log"
    try:
        with conn.cursor() as cur:
            if start_offset > 0:
                cur.execute(
                    "SELECT pg_read_file(%s, %s::bigint, 100000000::bigint);",
                    (filename, int(start_offset)),
                )
            else:
                cur.execute("SELECT pg_read_file(%s);", (filename,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("pg_read_file fallback failed for %s: %s", filename, exc)

    return ""


def save_workload_to_json(workload: ExtractedWorkload, output_path: str) -> None:
    """Serialize ExtractedWorkload queries, parameters, and weights to a JSON file."""
    data = []
    for q in workload.queries:
        query_text = str(q)
        weight = float(workload.query_weights.get(q, workload.query_weights.get(query_text, 1.0)))
        params_list = []
        for p in getattr(q, "params", []):
            params_list.append({
                "position": getattr(p, "position", 0),
                "type": getattr(p, "pg_type", "unknown"),
                "value": getattr(p, "value", None),
            })
        data.append({
            "query": query_text,
            "weight": weight,
            "with_params": len(params_list) > 0,
            "duration_ms": getattr(q, "duration_ms", 0.0),
            "rows_processed": getattr(q, "rows", 0),
            "params": params_list,
        })

    out_file = Path(output_path).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("Saved %d workload queries to %s", len(data), out_file)


def load_workload_from_json(input_path: str, conn=None) -> ExtractedWorkload:
    """Load ExtractedWorkload from a previously saved JSON file."""
    p = Path(input_path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Workload JSON file not found at: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Invalid workload JSON format at {p}: expected a list of query objects.")

    queries: List[CapturedQuery] = []
    query_weights: Dict[CapturedQuery, float] = {}

    for item in data:
        if not isinstance(item, dict):
            continue
        query_text = item.get("query", "")
        if not query_text:
            continue
        weight = float(item.get("weight", 1.0))
        duration_ms = float(item.get("duration_ms", 0.0))
        rows = int(item.get("rows_processed", item.get("rows", 0)))
        raw_params = item.get("params", [])
        params = [
            QueryParam(
                position=param.get("position", idx),
                pg_type=param.get("type", "text"),
                value=param.get("value"),
            )
            for idx, param in enumerate(raw_params, start=1)
            if isinstance(param, dict)
        ]
        q = CapturedQuery(
            query=query_text,
            params=params,
            duration_ms=duration_ms,
            rows=rows,
        )
        queries.append(q)
        query_weights[q] = weight

    schema = fetch_live_schema(conn) if conn is not None else {}
    return ExtractedWorkload(queries=queries, query_weights=query_weights, schema=schema)


def extract_workload(
    log_path: str,
    db_name: str,
    conn,
    start_offset: int = 0,
    output_path: Optional[str] = None,
) -> ExtractedWorkload:
    """
    Parse query_logger.log and extract the workload (SELECT, UPDATE, DELETE) for db_name.

    Optionally serializes the extracted workload to output_path if provided.

    Returns
    -------
    ExtractedWorkload
        Contains deduplicated queries with parameters, call weights, and live schema.
    """
    content = read_log_content(log_path, conn, start_offset=start_offset)
    if not content.strip():
        logger.warning("query_logger log content is empty (path: %s, offset: %d)", log_path, start_offset)
        empty_workload = ExtractedWorkload(schema=fetch_live_schema(conn))
        if output_path:
            save_workload_to_json(empty_workload, output_path)
        return empty_workload

    raw_entries: List[dict] = []
    for line_idx, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                raw_entries.append(obj)
        except json.JSONDecodeError as e:
            logger.debug("Line %d: invalid JSON: %s", line_idx, e)

    valid_entries: List[dict] = []
    for entry in raw_entries:
        if entry.get("db") != db_name:
            continue
        tag = entry.get("tag", "").upper()
        if tag not in ("SELECT", "UPDATE", "DELETE", "INSERT"):
            continue

        query_text = entry.get("query", "")
        query_text = _extract_inner_query(query_text)
        if not query_text:
            continue

        body = re.sub(r'^\s*((/\*.*?\*/)|(--.*)|\s)*', '', query_text, flags=re.DOTALL).strip()
        body_upper = body.upper()

        if tag == "SELECT":
            if not body_upper.startswith("SELECT"):
                continue
            if body_upper.startswith("SELECT 1"):
                continue
        elif tag == "UPDATE":
            if not body_upper.startswith("UPDATE"):
                continue
        elif tag == "DELETE":
            if not body_upper.startswith("DELETE"):
                continue
        elif tag == "INSERT":
            # Only include INSERT statements that have an inner SELECT (e.g. INSERT INTO ... SELECT)
            if "SELECT" not in body_upper:
                continue

        if _is_internal_query(body):
            continue

        entry["_clean_query"] = query_text
        valid_entries.append(entry)

    # Deduplicate by normalized query text AND parameter values, aggregating execution weights
    seen: Dict[CapturedQuery, CapturedQuery] = {}
    query_weights: Dict[CapturedQuery, float] = {}

    for entry in valid_entries:
        query_text = entry["_clean_query"]
        params = _process_params(entry.get("params", []), db_name=db_name)
        q = CapturedQuery(
            query=query_text,
            params=params,
            duration_ms=float(entry.get("duration_ms", 0.0)),
            rows=int(entry.get("rows", 0)),
        )
        if q in seen:
            query_weights[q] += 1.0
        else:
            seen[q] = q
            query_weights[q] = 1.0

    queries = list(seen.values())
    schema = fetch_live_schema(conn)

    logger.info(
        "Extracted %d unique queries from %d log entries (%d total read)",
        len(queries), len(valid_entries), len(raw_entries),
    )

    workload = ExtractedWorkload(
        queries=queries,
        query_weights=query_weights,
        schema=schema,
    )

    if output_path:
        save_workload_to_json(workload, output_path)

    return workload
