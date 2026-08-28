"""
tests/tpcc/tpcc_workload.py
Loads TPC-C (pgbench) SQL queries from scripts/queries/tpcc/ and fetches the
live schema from the tpcc_db database.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

# <repo_root>/scripts/queries/tpcc/
READS_DIR = Path(__file__).parent.parent.parent / "scripts" / "queries" / "tpcc"


def load_tpcc_queries() -> List[Tuple[str, str]]:
    """
    Return a sorted list of (label, sql) tuples for all TPC-C query files.
    Labels are like 'T1', 'T2', ..., 'T10'.
    """
    if not READS_DIR.exists():
        raise FileNotFoundError(
            f"TPC-C reads directory not found: {READS_DIR}\n"
            "Expected path: scripts/queries/tpcc/t1_*.sql ... t10_*.sql"
        )

    def _num(p: Path) -> int:
        m = re.search(r"(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    queries: List[Tuple[str, str]] = []
    for sql_file in sorted(READS_DIR.glob("t*.sql"), key=_num):
        sql = sql_file.read_text().strip().rstrip(";").rstrip() + ";"
        label = f"T{_num(sql_file)}"
        queries.append((label, sql))

    if not queries:
        raise ValueError(f"No t*.sql files found in {READS_DIR}")

    return queries


def fetch_schema(conn) -> Dict[str, Dict[str, str]]:
    """
    Fetch the live PostgreSQL schema {table: {column: data_type}} from
    information_schema.  Falls back to empty dict on failure.
    """
    schema: Dict[str, Dict[str, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position;
            """)
            for table, col, dtype in cur.fetchall():
                schema.setdefault(table, {})[col] = dtype.upper()
    except Exception as exc:
        print(f"[tpcc_workload] Warning: could not fetch schema: {exc}")
        conn.rollback()
    return schema
