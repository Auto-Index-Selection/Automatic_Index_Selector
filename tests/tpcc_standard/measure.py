"""
tests/tpcc_standard/measure.py
Measure query execution times for the official TPC-C workload across iterations.
Writes results to CSV files in the standard format.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RESULTS_DIR = Path(__file__).parent / "results"


def discard_session(conn) -> None:
    """Reset PostgreSQL plan caches, prepared statements, and session state."""
    try:
        conn.commit()
        old_autocommit = conn.autocommit
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DISCARD ALL;")
        conn.autocommit = old_autocommit
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def prewarm_database_tables(conn, schema: Dict, verbose: bool = True) -> None:
    """
    Read all tables into PostgreSQL buffer cache / OS page cache upfront.
    Ensures Baseline and all strategies start on an equal, warm buffer cache.
    """
    if verbose:
        print("[Prewarm] Warming all 9 TPC-C database tables into RAM buffer cache...")
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        for table in sorted(schema.keys()):
            if table.startswith("hypopg"):
                continue
            try:
                cur.execute(f"SELECT count(*) FROM {table};")
                cur.fetchone()
            except Exception as exc:
                print(f"  [WARN] Prewarm on {table} failed: {exc}")
                conn.rollback()
    conn.commit()
    discard_session(conn)
    elapsed = time.perf_counter() - t0
    if verbose:
        print(f"[Prewarm] Loaded {len(schema)} tables into cache in {elapsed:.3f}s.\n")


def measure_workload(
    conn,
    queries: List[Tuple[str, str]],
    iterations: int = 3,
    warmup: bool = True,
) -> Dict:
    per_query_sums: Dict[str, float] = {}
    per_query_counts: Dict[str, int] = {}
    per_query_passes: Dict[str, List[float]] = {label: [] for label, _ in queries}
    total_times: List[float] = []

    # 0. Pre-measurement warm-up
    if warmup:
        print("  [Warmup] Pre-warming buffer cache...", end=" ", flush=True)
        t_w0 = time.perf_counter()
        for label, sql in queries:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                conn.commit()
            except Exception:
                conn.rollback()
        w_elapsed = time.perf_counter() - t_w0
        print(f"done ({w_elapsed:.3f}s)")
        discard_session(conn)

    for it in range(1, iterations + 1):
        print(f"  Iteration {it}/{iterations}", end=" | ", flush=True)
        iter_total = 0.0

        for label, sql in queries:
            t0 = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                elapsed = time.perf_counter() - t0
                conn.commit()
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                conn.rollback()
                print(f"\n  [ERROR] {label}: {exc}", flush=True)

            per_query_sums[label] = per_query_sums.get(label, 0.0) + elapsed
            per_query_counts[label] = per_query_counts.get(label, 0) + 1
            per_query_passes[label].append(elapsed)
            iter_total += elapsed

            print(label, end=" ", flush=True)

        total_times.append(iter_total)
        print(f"| total={iter_total:.3f}s")

    per_query_avg: Dict[str, float] = {}
    for lbl in per_query_sums:
        count = per_query_counts.get(lbl, 1)
        per_query_avg[lbl] = per_query_sums[lbl] / count if count > 0 else 0.0

    avg_total = sum(total_times) / len(total_times) if total_times else 0.0

    return {
        "per_query": per_query_avg,
        "total": avg_total,
        "iterations": iterations,
        "all_passes": per_query_passes,
    }


def write_csv(
    results: Dict,
    label: str,
    results_dir: Path = RESULTS_DIR,
) -> Tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    qt_path = results_dir / f"{label}_qt.csv"
    avg_path = results_dir / f"{label}_avg.csv"

    per_query_avg = results["per_query"]
    all_passes = results.get("all_passes", {})
    iterations = results["iterations"]

    with open(qt_path, "w", newline="") as f:
        writer = csv.writer(f)
        header = ["query"] + [f"it_{i}" for i in range(1, iterations + 1)]
        writer.writerow(header)
        for q_label in per_query_avg.keys():
            passes = all_passes.get(q_label, [])
            row = [q_label] + [f"{t:.6f}" for t in passes]
            writer.writerow(row)

    with open(avg_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "avg_time_seconds"])
        for q_label, avg_t in per_query_avg.items():
            writer.writerow([q_label, f"{avg_t:.6f}"])

    return qt_path, avg_path
