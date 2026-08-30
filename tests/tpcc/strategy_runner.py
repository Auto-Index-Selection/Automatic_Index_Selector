"""
tests/tpcc/strategy_runner.py
-----------------------------
Pure AIS Strategy Runner for TPC-C Benchmarks:
1. Optionally spawns a concurrent background traffic thread (waits 2s, runs TPC-C DML/reads).
2. Invokes run_auto_index_selector from src/auto_index_selector to get C* and calculate write penalties.
3. Physically creates selected index set C* in PostgreSQL.
4. Measures actual wall-clock execution latency across N passes.
5. Saves CSV results.
6. Physically drops indexes and resets session state.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Any

import psycopg2

from auto_index_selector.__main__ import run_auto_index_selector
from .runner import discard_session, prewarm_tables, measure_read_queries, create_physical_indexes, drop_physical_indexes
from .workload import load_tpcc_queries, run_tpcc_traffic

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CSV_DIR = RESULTS_DIR / "csv"


def _traffic_worker(dbname: str, delay_seconds: float, duration_seconds: float, dml_rounds: int):
    """Wait delay_seconds for AIS before-snapshot, then execute concurrent TPC-C traffic."""
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
            run_tpcc_traffic(conn, duration_seconds=duration_seconds, dml_rounds=dml_rounds)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Background traffic worker encountered error: %s", e)


def write_strategy_csv(label: str, results: Dict, storage_mb: float, csv_dir: Path) -> Path:
    """Save execution metrics for a strategy to CSV."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{label}.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "avg_time_seconds", "avg_time_ms"])
        for q, t_ms in sorted(results["per_query_avg_ms"].items()):
            writer.writerow([q, f"{t_ms / 1000.0:.6f}", f"{t_ms:.3f}"])
        writer.writerow(["TOTAL", f"{results['total_avg_ms'] / 1000.0:.6f}", f"{results['total_avg_ms']:.3f}"])
        writer.writerow(["STORAGE_MB", f"{storage_mb:.2f}", f"{storage_mb:.2f}"])
    return csv_path


def run_baseline(conn, queries: List[Tuple[str, str]], iterations: int = 3, csv_dir: Optional[Path] = None) -> Dict:
    """Run baseline measurement with no indexes."""
    out_dir = csv_dir or CSV_DIR
    print("\n" + "=" * 65)
    print(" [Baseline] Measuring Physical Performance (No Indexes)")
    print("=" * 65)
    prewarm_tables(conn, verbose=False)
    read_results = measure_read_queries(conn, queries, iterations=iterations, warmup=True)
    write_strategy_csv("baseline", read_results, 0.0, out_dir)
    print(f"  ✓ Baseline Total Execution Time: {read_results['total_avg_ms']:.2f} ms ({read_results['total_avg_ms']/1000.0:.3f} s)")
    return {
        "label": "baseline",
        "read_results": read_results,
        "storage_mb": 0.0,
        "total_seconds": read_results["total_avg_ms"] / 1000.0,
        "total_ms": read_results["total_avg_ms"],
    }


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
    """
    Executes a single (CG, CS, params) experiment:
    1. Optionally spawns a concurrent background traffic thread (waits 2s, then generates DMLs/reads).
    2. Invokes run_auto_index_selector from src/auto_index_selector to get C* and calculate write penalties.
    3. Physically creates indexes in PostgreSQL.
    4. Measures execution times across iterations.
    5. Drops indexes and cleans up.
    """
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
        print(f"  [Traffic] Started background traffic thread (starts in 2s, runs for {max(1.0, float(window_seconds - 3)):.0f}s)...")

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
    print(f"  1. Selection (AIS): Selected {len(selected)} indexes in {sel_time:.3f}s")
    for t, cols in sorted(selected):
        pen = write_pen(t, tuple(cols)) if write_pen and callable(write_pen) else 0.0
        print(f"     -> CREATE INDEX ON {t}({', '.join(cols)});  [write_penalty = {pen:.4f}]")

    # 2. Create Physical Indexes
    created_indexes, build_time_s, storage_mb = create_physical_indexes(conn, selected)
    print(f"  2. Physical Build: {len(created_indexes)} indexes created in {build_time_s:.3f}s ({storage_mb:.2f} MB)")

    # 3. Measure Physical Performance
    try:
        read_results = measure_read_queries(conn, queries, iterations=iterations, warmup=True)
    finally:
        # 4. Clean up Indexes
        drop_physical_indexes(conn, created_indexes)
        print(f"  3. Cleanup: Dropped test indexes, session reset.")

    # 5. Save CSV
    write_strategy_csv(label, read_results, storage_mb, out_dir)
    print(f"  ✓ Total Workload Latency: {read_results['total_avg_ms']:.2f} ms ({read_results['total_avg_ms']/1000.0:.3f} s)")

    return {
        "label": label,
        "cg": cg_name,
        "cs": cs_name,
        "selected_config": selected,
        "read_results": read_results,
        "storage_mb": storage_mb,
        "build_time_s": build_time_s,
        "total_seconds": read_results["total_avg_ms"] / 1000.0,
        "total_ms": read_results["total_avg_ms"],
    }
