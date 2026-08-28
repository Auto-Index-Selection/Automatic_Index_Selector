"""
tests/tpcc_standard/tpcc_workload.py
Loads official TPC-C queries (TC1-TC10) and target database schema.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

QUERIES_DIR = Path(__file__).parent / "queries"


def load_tpcc_queries() -> List[Tuple[str, str]]:
    """
    Load all queries from queries/ sorted naturally (TC1..TC10).
    Returns list of (label, sql_string).
    """
    sql_files = list(QUERIES_DIR.glob("*.sql"))

    def _sort_key(p: Path) -> int:
        m = re.search(r"\d+", p.stem)
        return int(m.group()) if m else 999

    sql_files.sort(key=_sort_key)

    queries: List[Tuple[str, str]] = []
    for f in sql_files:
        label = f.stem.upper()
        content = f.read_text(encoding="utf-8").strip()
        lines = [line for line in content.splitlines() if not line.strip().startswith("--")]
        clean_sql = " ".join(lines).strip()
        if clean_sql:
            queries.append((label, clean_sql))

    return queries


def fetch_schema(conn) -> Dict[str, Dict[str, str]]:
    """
    Fetch the public schema for the connected PostgreSQL database.
    Returns: {table_name: {column_name: data_type}}
    """
    schema: Dict[str, Dict[str, str]] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position;
        """)
        for table, col, dtype in cur.fetchall():
            if table not in schema:
                schema[table] = {}
            schema[table][col] = dtype
    return schema
