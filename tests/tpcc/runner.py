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

