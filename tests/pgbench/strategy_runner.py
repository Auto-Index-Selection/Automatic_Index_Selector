"""
tests/pgbench/strategy_runner.py
--------------------------------
Strategy runner for the pgbench (TPC-B) benchmark suite.

Per strategy it:
1. Spawns a concurrent background traffic thread (waits 2s, runs pgbench reads/DML).
2. Calls run_auto_index_selector, which observes that traffic and returns C*.
3. Hands C* to the shared harness to build, measure and clean up.

Note this suite observes a *separate* window per strategy, so strategies are
compared on slightly different workloads. tests/tpcc instead observes once and
shares one observation across every combination -- the controlled design.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2

from auto_index_selector.__main__ import run_auto_index_selector
from tests.bench_common.harness import build_measure_drop, capture_existing_indexes
from .workload import run_pgbench_traffic

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CSV_DIR = RESULTS_DIR / "csv"


def _traffic_worker(dbname: str, delay_seconds: float, duration_seconds: float, dml_rounds: int):
    """Wait delay_seconds for the AIS before-snapshot, then run concurrent traffic."""
    time.sleep(delay_seconds)
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        conn = psycopg2.connect(
            dbname=dbname,
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "postgres"),
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
        )
        try:
            print(f"  [Traffic] Executing concurrent background traffic ({dml_rounds} DML rounds)...")
            run_pgbench_traffic(conn, duration_seconds=duration_seconds, dml_rounds=dml_rounds)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Background traffic worker encountered error: %s", e)


def run_strategy(
    conn,
    queries: List[Tuple[str, str]],
    cg_name: str,
    cs_name: str,
    label: str,
    iterations: int = 3,
    window_seconds: int = 0,
    dml_rounds: int = 10,
    write_scale: float = 1.0,
    csv_dir: Optional[Path] = None,
    **kwargs,
) -> Dict:
    """Execute one (CG, CS, params) experiment end to end."""
    out_dir = csv_dir or CSV_DIR

    print("\n" + "-" * 65)
    print(f" [Strategy] {label}")
    print(f"   CG: {cg_name} | CS: {cs_name} | Params: {kwargs} | Window: {window_seconds}s")
    print("-" * 65)

    traffic_thread = None
    if window_seconds > 0:
        traffic_thread = threading.Thread(
            target=_traffic_worker,
            args=(conn.info.dbname, 2.0, max(1.0, float(window_seconds - 3)), dml_rounds),
            daemon=True,
        )
        traffic_thread.start()
        print(f"  [Traffic] Started background traffic thread "
              f"(starts in 2s, runs for {max(1.0, float(window_seconds - 3)):.0f}s)...")

    existing = capture_existing_indexes(conn)

    # 1. Ask AIS for the recommended index configuration
    t_sel0 = time.perf_counter()
    selected, W, query_weights, write_pen = run_auto_index_selector(
        conn=conn,
        config_override={
            "candidate_generation": {"module": cg_name},
            "config_selection": {"module": cs_name, **kwargs},
            "write_penalty": {
                "enabled": True if window_seconds > 0 else False,
                "window_duration_seconds": window_seconds,
                "write_scale": write_scale,
            },
        },
        verbose=True if window_seconds > 0 else False,
    )
    if traffic_thread and traffic_thread.is_alive():
        traffic_thread.join()

    sel_time = time.perf_counter() - t_sel0
    print(f"  1. Selection (AIS): {len(selected)} index(es) in {sel_time:.3f}s")
    for t, cols in sorted(selected):
        pen = write_pen(t, tuple(cols)) if write_pen and callable(write_pen) else 0.0
        print(f"     -> CREATE INDEX ON {t}({', '.join(cols)});  [write_penalty = {pen:.4f}]")

    # 2-5. Build C*, measure, drop, persist -- shared with every other suite.
    result = build_measure_drop(
        conn, queries, label, selected, write_pen, iterations, out_dir, existing
    )
    result.update({"cg": cg_name, "cs": cs_name, "selection_seconds": sel_time})
    return result
