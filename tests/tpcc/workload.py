"""
tests/tpcc/workload.py
----------------------
TPC-C workload loader and simulation traffic generator for read queries
and DML write transactions. All queries are stored as fixed, auditable
SQL files under tests/tpcc/queries/reads/ and tests/tpcc/queries/dml/.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).resolve().parent / "queries"
READS_DIR = QUERIES_DIR / "reads"
DMLS_DIR = QUERIES_DIR / "dml"


def load_tpcc_queries() -> List[Tuple[str, str]]:
    """
    Load all TPC-C analytical read queries from tests/tpcc/queries/reads/.
    Returns a sorted list of (label, sql) tuples (e.g. ('T1', 'SELECT...')).
    """
    if not READS_DIR.exists():
        raise FileNotFoundError(f"TPC-C reads directory not found at: {READS_DIR}")

    def _num(p: Path) -> int:
        m = re.search(r"t(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    queries: List[Tuple[str, str]] = []
    for sql_file in sorted(READS_DIR.glob("t*.sql"), key=_num):
        sql = sql_file.read_text().strip().rstrip(";").rstrip() + ";"
        label = f"T{_num(sql_file)}"
        queries.append((label, sql))

    if not queries:
        raise ValueError(f"No t*.sql files found in {READS_DIR}")

    return queries


def load_tpcc_dml_queries() -> List[Tuple[str, str]]:
    """
    Load all fixed TPC-C DML write transactions from tests/tpcc/queries/dml/.
    Returns a sorted list of (label, sql) tuples (e.g. ('D1', 'UPDATE...')).
    """
    if not DMLS_DIR.exists():
        raise FileNotFoundError(f"TPC-C DML directory not found at: {DMLS_DIR}")

    def _num(p: Path) -> int:
        m = re.search(r"d(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    dml_queries: List[Tuple[str, str]] = []
    for sql_file in sorted(DMLS_DIR.glob("d*.sql"), key=_num):
        sql = sql_file.read_text().strip().rstrip(";").rstrip() + ";"
        label = f"D{_num(sql_file)}"
        dml_queries.append((label, sql))

    if not dml_queries:
        raise ValueError(f"No d*.sql files found in {DMLS_DIR}")

    return dml_queries


def run_tpcc_traffic(conn, duration_seconds: int = 10, rounds_per_query: int = 5, dml_rounds: int = 20) -> int:
    """
    Executes fixed TPC-C analytical read queries and DML write transactions against PostgreSQL.
    Used to populate pg_stat_statements and advisor_write_stats during observation windows.
    """
    read_queries = load_tpcc_queries()
    dml_queries = load_tpcc_dml_queries()
    total_executed = 0

    t_end = time.time() + duration_seconds
    with conn.cursor() as cur:
        # 1. Run read queries
        for _ in range(rounds_per_query):
            for label, sql in read_queries:
                try:
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                    total_executed += 1
                except Exception as e:
                    conn.rollback()
                    logger.debug("Query %s execution failed: %s", label, e)

        # 2. Run DML write transactions
        for _ in range(dml_rounds):
            for label, sql in dml_queries:
                try:
                    cur.execute(sql)
                    total_executed += 1
                except Exception as e:
                    conn.rollback()
                    logger.debug("DML %s execution failed: %s", label, e)

        conn.commit()

    # If duration remaining, sleep to complete observation window
    remaining = t_end - time.time()
    if remaining > 0:
        time.sleep(remaining)

    return total_executed
