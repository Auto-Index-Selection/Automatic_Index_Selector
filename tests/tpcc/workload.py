"""
tests/tpcc/workload.py
----------------------
TPC-C workload loader and the observation-window driver.

`run_tpcc_workload` is passed to `observe_workload` as its `observation_hook`,
replacing the default `time.sleep(window_duration_seconds)`. The AIS pipeline
then observes a workload that was *actually executed* during the window rather
than whatever background traffic happened to arrive — so W, the query weights
and the write penalties are deterministic and reproducible.

It opens its own connection: running workload SQL on the snapshot connection
would let one failed statement abort the transaction the pipeline needs.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

from .transactions import run_transaction_round

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).resolve().parent / "queries"
READS_DIR = QUERIES_DIR / "reads"


def load_tpcc_queries() -> List[Tuple[str, str]]:
    """Load the TPC-C read queries as sorted (label, sql) pairs."""
    if not READS_DIR.exists():
        raise FileNotFoundError(f"TPC-C reads directory not found at: {READS_DIR}")

    def _num(p: Path) -> int:
        m = re.search(r"t(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    queries = [
        (f"T{_num(f)}", f.read_text().strip().rstrip(";").rstrip() + ";")
        for f in sorted(READS_DIR.glob("t*.sql"), key=_num)
    ]
    if not queries:
        raise ValueError(f"No t*.sql files found in {READS_DIR}")
    return queries


def connect(dbname: str):
    """Open a fresh connection to the benchmark database."""
    load_dotenv()
    return psycopg2.connect(
        dbname=dbname,
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
    )


def run_tpcc_workload(
    dbname: str,
    read_rounds: int = 3,
    txn_rounds: int = 2,
    seed: int = 42,
    verbose: bool = True,
) -> Dict:
    """
    Execute the TPC-C workload once, for one observation window.

    Reads populate pg_stat_statements (giving W and the call-count weights);
    the write transactions populate advisor_write_stats and pg_stat_user_tables
    (giving the write penalties). Every round commits separately so one failure
    cannot discard the window's accumulated writes.
    """
    queries = load_tpcc_queries()
    rng = random.Random(seed)
    stats = {"reads": 0, "read_failures": 0, "transactions": {}}
    t0 = time.perf_counter()

    conn = connect(dbname)
    try:
        # --- Reads: one commit per round ---
        for _ in range(read_rounds):
            try:
                with conn.cursor() as cur:
                    for label, sql in queries:
                        cur.execute(sql)
                        if cur.description:
                            cur.fetchall()
                        stats["reads"] += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                stats["read_failures"] += 1
                logger.debug("Read round aborted: %s", e)

        # --- Writes: the canonical transaction mix ---
        for _ in range(txn_rounds):
            counts = run_transaction_round(conn, rng)
            for name, n in counts.items():
                stats["transactions"][name] = stats["transactions"].get(name, 0) + n
    finally:
        conn.close()

    stats["seconds"] = time.perf_counter() - t0
    if verbose:
        txns = ", ".join(f"{k}={v}" for k, v in sorted(stats["transactions"].items())) or "none"
        print(f"  [Workload] Executed {stats['reads']} reads and transactions ({txns}) "
              f"in {stats['seconds']:.1f}s")
    return stats


def make_observation_hook(
    dbname: str,
    read_rounds: int = 3,
    txn_rounds: int = 2,
    seed: int = 42,
) -> Tuple[Callable[[], None], Dict]:
    """
    Build the zero-argument callable `observe_workload` expects, plus a dict
    that receives the execution stats once the hook has run.
    """
    captured: Dict = {}

    def _hook() -> None:
        captured.update(run_tpcc_workload(dbname, read_rounds, txn_rounds, seed))

    return _hook, captured
