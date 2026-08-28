"""
tests/tpch/tpch_workload.py
Loads TPC-H SQL queries from scripts/queries/reads/ and fetches the live
schema from the database so downstream modules can use both.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

# <repo_root>/scripts/queries/reads/
READS_DIR = Path(__file__).parent.parent.parent / "scripts" / "queries" / "reads"


def load_tpch_queries() -> List[Tuple[str, str]]:
    """
    Return a sorted list of (label, sql) tuples for all TPC-H query files.
    Labels are 'Q1', 'Q2', ..., 'Q22'.
    """
    if not READS_DIR.exists():
        raise FileNotFoundError(
            f"TPC-H reads directory not found: {READS_DIR}\n"
            "Expected path: scripts/queries/reads/q1.sql ... q22.sql"
        )

    def _num(p: Path) -> int:
        m = re.search(r"\d+", p.stem)
        return int(m.group()) if m else 0

    queries: List[Tuple[str, str]] = []
    for sql_file in sorted(READS_DIR.glob("q*.sql"), key=_num):
        sql = sql_file.read_text().strip().rstrip(";").rstrip() + ";"
        label = f"Q{_num(sql_file)}"
        queries.append((label, sql))

    if not queries:
        raise ValueError(f"No q*.sql files found in {READS_DIR}")

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
        print(f"[tpch_workload] Warning: could not fetch schema: {exc}")
        conn.rollback()
    return schema
