"""
tests/tpcc/workload.py
----------------------
TPC-C workload loader and simulation traffic generator for read queries
and DML write transactions.
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

READS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "queries" / "tpcc"


def load_tpcc_queries() -> List[Tuple[str, str]]:
    """
    Load all TPC-C analytical read queries from scripts/queries/tpcc/.
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


class TPCCDMLGenerator:
    """Generates standard TPC-C (pgbench) transactions for write workload simulation."""

    def __init__(self, seed: int = 42, max_accounts: int = 100_000, max_branches: int = 10, max_tellers: int = 100):
        self._rng = random.Random(seed)
        self._max_accounts = max_accounts
        self._max_branches = max_branches
        self._max_tellers = max_tellers

    def generate_random_dml(self) -> Tuple[str, Sequence]:
        """Returns a random DML SQL template and parameter tuple."""
        choice = self._rng.choice(["account_update", "teller_update", "branch_update", "history_insert"])
        if choice == "account_update":
            delta = self._rng.randint(-500, 500)
            aid = self._rng.randint(1, self._max_accounts)
            return "UPDATE pgbench_accounts SET abalance = abalance + %s WHERE aid = %s;", (delta, aid)
        elif choice == "teller_update":
            delta = self._rng.randint(-500, 500)
            tid = self._rng.randint(1, self._max_tellers)
            return "UPDATE pgbench_tellers SET tbalance = tbalance + %s WHERE tid = %s;", (delta, tid)
        elif choice == "branch_update":
            delta = self._rng.randint(-500, 500)
            bid = self._rng.randint(1, self._max_branches)
            return "UPDATE pgbench_branches SET bbalance = bbalance + %s WHERE bid = %s;", (delta, bid)
        else:
            tid = self._rng.randint(1, self._max_tellers)
            bid = self._rng.randint(1, self._max_branches)
            aid = self._rng.randint(1, self._max_accounts)
            delta = self._rng.randint(-500, 500)
            return "INSERT INTO pgbench_history (tid, bid, aid, delta, mtime) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP);", (tid, bid, aid, delta)


def run_tpcc_traffic(conn, duration_seconds: int = 10, rounds_per_query: int = 5, dml_rounds: int = 20, seed: int = 42) -> int:
    """
    Executes TPC-C analytical read queries and DML write transactions against PostgreSQL.
    Used to populate pg_stat_statements and advisor_write_stats during observation windows.
    """
    queries = load_tpcc_queries()
    dml_gen = TPCCDMLGenerator(seed=seed)
    total_executed = 0

    t_end = time.time() + duration_seconds
    with conn.cursor() as cur:
        # Run read queries
        for _ in range(rounds_per_query):
            for label, sql in queries:
                try:
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                    total_executed += 1
                except Exception as e:
                    conn.rollback()
                    logger.debug("Query %s execution failed: %s", label, e)

        # Run DML writes
        for _ in range(dml_rounds):
            sql, params = dml_gen.generate_random_dml()
            try:
                cur.execute(sql, params)
                total_executed += 1
            except Exception as e:
                conn.rollback()
                logger.debug("DML execution failed: %s", e)

        conn.commit()

    # If duration remaining, sleep
    remaining = t_end - time.time()
    if remaining > 0:
        time.sleep(remaining)

    return total_executed
