"""
tests/tpcc/runner.py
--------------------
Orchestrates the full physical benchmarking experiment:
1. Executes TPC-C traffic & calls auto_index_selector for recommendations (C*).
2. Measures baseline physical query execution times (no indexes).
3. Physically creates C* in PostgreSQL & measures disk storage and build time.
4. Measures post-index physical query execution times and DML maintenance latency.
5. Cleans up created indexes and exports results to CSV.
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import psycopg2

from auto_index_selector.__main__ import run_auto_index_selector
from .workload import load_tpcc_queries, run_tpcc_traffic, TPCCDMLGenerator

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def discard_session(conn) -> None:
    """Reset PostgreSQL plan caches, prepared statements, and session state."""
    try:
        conn.commit()
        old = conn.autocommit
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DISCARD ALL;")
        conn.autocommit = old
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def prewarm_tables(conn, verbose: bool = True) -> None:
    """Prewarm database tables into RAM buffer cache for consistent baseline comparisons."""
    if verbose:
        print("[Prewarm] Warming all database tables into RAM buffer cache...")
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
        tables = [r[0] for r in cur.fetchall() if not r[0].startswith("hypopg")]
        for table in tables:
            try:
                cur.execute(f"SELECT count(*) FROM {table};")
                cur.fetchone()
            except Exception as e:
                conn.rollback()
    conn.commit()
    discard_session(conn)
    if verbose:
        print(f"[Prewarm] Loaded {len(tables)} tables into buffer cache in {time.perf_counter() - t0:.3f}s.\n")


def measure_read_queries(conn, queries: List[Tuple[str, str]], iterations: int = 3, warmup: bool = True) -> Dict:
    """
    Measures wall-clock execution latency for each query across N iterations.
    """
    if warmup:
        for label, sql in queries:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                conn.commit()
            except Exception:
                conn.rollback()

    discard_session(conn)
    per_query_passes: Dict[str, List[float]] = {label: [] for label, _ in queries}

    for it in range(iterations):
        for label, sql in queries:
            try:
                with conn.cursor() as cur:
                    t0 = time.perf_counter()
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    per_query_passes[label].append(elapsed_ms)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("Query %s failed during measurement: %s", label, e)
                per_query_passes[label].append(0.0)

    per_query_avg = {label: (sum(times) / len(times) if times else 0.0) for label, times in per_query_passes.items()}
    total_avg_ms = sum(per_query_avg.values())

    return {
        "per_query_avg_ms": per_query_avg,
        "per_query_passes": per_query_passes,
        "total_avg_ms": total_avg_ms,
    }


def measure_dml_latency(conn, sample_count: int = 50, seed: int = 42) -> float:
    """
    Measures the average wall-clock latency (ms) per DML statement.
    """
    gen = TPCCDMLGenerator(seed=seed)
    timings = []
    with conn.cursor() as cur:
        for _ in range(sample_count):
            sql, params = gen.generate_random_dml()
            try:
                t0 = time.perf_counter()
                cur.execute(sql, params)
                timings.append((time.perf_counter() - t0) * 1000.0)
            except Exception:
                conn.rollback()
        conn.commit()
    return sum(timings) / len(timings) if timings else 0.0


def create_physical_indexes(conn, configuration) -> Tuple[List[str], float, float]:
    """
    Executes CREATE INDEX for the recommended configuration in PostgreSQL.
    Returns (created_index_names, total_creation_time_s, total_size_mb).
    """
    created_names = []
    t0 = time.perf_counter()
    total_size_bytes = 0

    with conn.cursor() as cur:
        for table, cols in configuration:
            idx_name = f"idx_rec_{table}_{'_'.join(cols)}"[:63]
            cols_str = ", ".join(cols)
            sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({cols_str});"
            cur.execute(sql)
            created_names.append((table, idx_name))

            # Query physical index size
            cur.execute("SELECT pg_relation_size(%s);", (idx_name,))
            row = cur.fetchone()
            if row and row[0]:
                total_size_bytes += int(row[0])

        conn.commit()

    total_creation_time = time.perf_counter() - t0
    total_size_mb = total_size_bytes / (1024.0 * 1024.0)
    return created_names, total_creation_time, total_size_mb


def drop_physical_indexes(conn, created_names) -> None:
    """Drops all created test indexes."""
    with conn.cursor() as cur:
        for _, idx_name in created_names:
            try:
                cur.execute(f"DROP INDEX IF EXISTS {idx_name};")
            except Exception:
                pass
        conn.commit()


def run_tpcc_experiment(
    conn,
    window_seconds: int = 10,
    rounds: int = 20,
    iterations: int = 3,
    output_dir: Optional[Path] = None,
) -> Dict:
    """
    Executes full physical benchmarking lifecycle:
    Baseline Measurement -> Advisor Invocation (C*) -> Physical Index Build -> Post-Index Measurement -> Export.
    """
    out_dir = output_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    queries = load_tpcc_queries()

    print("\n" + "=" * 70)
    print("           TPC-C BENCHMARK & INDEX EVALUATION EXPERIMENT           ")
    print("=" * 70)

    # 1. Prewarm
    prewarm_tables(conn, verbose=True)

    # 2. Measure Baseline (No Indexes)
    print("--- [Phase 1/4] Measuring Baseline Query & DML Performance ---")
    baseline_read = measure_read_queries(conn, queries, iterations=iterations)
    baseline_dml_ms = measure_dml_latency(conn, sample_count=rounds)
    print(f"  ✓ Baseline Total Query Execution Time: {baseline_read['total_avg_ms']:.2f} ms")
    print(f"  ✓ Baseline Avg DML Latency:           {baseline_dml_ms:.3f} ms / write\n")

    # 3. Workload Traffic Simulation & Advisor Invocation
    print("--- [Phase 2/4] Running Observation Window & Invoking Index Advisor ---")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_statements_reset();")
    conn.commit()

    import threading

    def _traffic_worker():
        w_conn = psycopg2.connect(
            dbname=conn.info.dbname,
            user=conn.info.user,
            password=conn.info.password,
            host=conn.info.host,
            port=conn.info.port,
        )
        try:
            run_tpcc_traffic(w_conn, duration_seconds=window_seconds, rounds_per_query=5, dml_rounds=rounds)
        finally:
            w_conn.close()

    print(f"  [Simulator] Generating concurrent TPC-C traffic ({rounds} DMLs, {len(queries)} queries) during {window_seconds}s window...")
    sim_thread = threading.Thread(target=_traffic_worker)
    sim_thread.start()

    print("  [Advisor] Invoking Automatic Index Selector...")
    selected_config, W_delta, weights, write_pen = run_auto_index_selector(
        conn=conn,
        config_override={"write_penalty": {"window_duration_seconds": window_seconds}},
        verbose=False
    )
    sim_thread.join()

    print(f"  ✓ Advisor Recommended {len(selected_config)} Indexes:")
    for t, c in selected_config:
        pen = write_pen(t, tuple(c)) if write_pen and callable(write_pen) else 0.0
        print(f"      CREATE INDEX ON {t}({', '.join(c)}); [Est. Write Penalty = {pen:.4f}]")
    print()

    # 4. Physical Index Creation
    created_indexes, build_time_s, storage_mb = create_physical_indexes(conn, selected_config)
    print("--- [Phase 3/4] Physically Created Recommended Indexes ---")
    print(f"  ✓ Indexes Built in:   {build_time_s:.3f} s")
    print(f"  ✓ Total Storage Used: {storage_mb:.2f} MB\n")

    # 5. Measure Post-Index Performance
    print("--- [Phase 4/4] Measuring Post-Index Performance & Write Overhead ---")
    try:
        indexed_read = measure_read_queries(conn, queries, iterations=iterations)
        indexed_dml_ms = measure_dml_latency(conn, sample_count=rounds)
    finally:
        # 6. Cleanup Indexes
        drop_physical_indexes(conn, created_indexes)
        print("  ✓ Dropped physical test indexes. Database state restored.\n")

    speedup_pct = ((baseline_read['total_avg_ms'] - indexed_read['total_avg_ms']) / baseline_read['total_avg_ms']) * 100.0 if baseline_read['total_avg_ms'] > 0 else 0.0
    dml_overhead_pct = ((indexed_dml_ms - baseline_dml_ms) / baseline_dml_ms) * 100.0 if baseline_dml_ms > 0 else 0.0

    print("=" * 70)
    print("                       BENCHMARK RESULTS SUMMARY                       ")
    print("=" * 70)
    print(f"  Baseline Total Query Latency:  {baseline_read['total_avg_ms']:10.2f} ms")
    print(f"  Indexed Total Query Latency:   {indexed_read['total_avg_ms']:10.2f} ms")
    print(f"  Net Query Speedup:             {speedup_pct:10.2f} %")
    print(f"  Baseline DML Write Latency:    {baseline_dml_ms:10.3f} ms")
    print(f"  Indexed DML Write Latency:     {indexed_dml_ms:10.3f} ms  (Overhead: {dml_overhead_pct:+.2f}%)")
    print(f"  Index Storage Overhead:        {storage_mb:10.2f} MB")
    print("=" * 70 + "\n")

    # Save to CSV
    csv_path = out_dir / "tpcc_benchmark_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query_label", "baseline_ms", "indexed_ms", "speedup_pct"])
        for label, _ in queries:
            b_ms = baseline_read["per_query_avg_ms"].get(label, 0.0)
            i_ms = indexed_read["per_query_avg_ms"].get(label, 0.0)
            sp = ((b_ms - i_ms) / b_ms * 100.0) if b_ms > 0 else 0.0
            writer.writerow([label, f"{b_ms:.3f}", f"{i_ms:.3f}", f"{sp:.2f}"])
        writer.writerow(["TOTAL_WORKLOAD", f"{baseline_read['total_avg_ms']:.3f}", f"{indexed_read['total_avg_ms']:.3f}", f"{speedup_pct:.2f}"])
        writer.writerow(["DML_WRITE_LATENCY", f"{baseline_dml_ms:.3f}", f"{indexed_dml_ms:.3f}", f"{dml_overhead_pct:.2f}"])

    print(f"[Results] Benchmark metrics saved to {csv_path}")

    return {
        "baseline_read": baseline_read,
        "indexed_read": indexed_read,
        "baseline_dml_ms": baseline_dml_ms,
        "indexed_dml_ms": indexed_dml_ms,
        "speedup_pct": speedup_pct,
        "storage_mb": storage_mb,
        "selected_config": selected_config,
        "csv_path": csv_path,
    }
