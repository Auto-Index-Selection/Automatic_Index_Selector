"""
tests/bench_common/runner.py
----------------------------
Schema-agnostic measurement primitives shared by every benchmark suite
(tests/pgbench, tests/tpcc):

  - discard_session      : reset plan caches and session state
  - prewarm_tables       : pull table heaps into the buffer cache
  - measure_read_queries : wall-clock latency per query, failure-aware
  - create_physical_indexes / drop_physical_indexes : build and sweep C*

Nothing here knows which schema is being benchmarked. Suite-specific query
loading and traffic generation live in each suite's own workload module.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Every index this harness builds carries this prefix. Cleanup sweeps by it,
# so nothing outside the prefix can ever be dropped.
BENCH_INDEX_PREFIX = "idx_rec_"


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

    A query that raises is NOT recorded as 0.0 ms -- that would make a broken
    query look like an infinitely fast one and pull the workload total *down*.
    Failures are counted instead, and a query with no successful sample is
    reported as NaN so it propagates into the total and marks the run invalid.
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
    per_query_failures: Dict[str, int] = {label: 0 for label, _ in queries}
    per_iteration_total_ms: List[float] = []

    for _ in range(iterations):
        iteration_ms = 0.0
        iteration_complete = True
        for label, sql in queries:
            try:
                with conn.cursor() as cur:
                    t0 = time.perf_counter()
                    cur.execute(sql)
                    if cur.description:
                        cur.fetchall()
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    per_query_passes[label].append(elapsed_ms)
                    iteration_ms += elapsed_ms
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("Query %s failed during measurement: %s", label, e)
                per_query_failures[label] += 1
                iteration_complete = False
        # An iteration missing a query is not comparable to a complete one.
        per_iteration_total_ms.append(iteration_ms if iteration_complete else float("nan"))

    per_query_avg = {
        label: (sum(times) / len(times) if times else float("nan"))
        for label, times in per_query_passes.items()
    }
    # NaN propagates: a total missing a query must not be compared to a full one.
    total_avg_ms = sum(per_query_avg.values())

    complete = [t for t in per_iteration_total_ms if not math.isnan(t)]
    complete.sort()

    def _pct(p: float) -> float:
        if not complete:
            return float("nan")
        return complete[min(len(complete) - 1, int(round(p * (len(complete) - 1))))]

    return {
        "per_query_avg_ms": per_query_avg,
        "per_query_passes": per_query_passes,
        "per_query_failures": per_query_failures,
        "per_iteration_total_ms": per_iteration_total_ms,
        "total_avg_ms": total_avg_ms,
        "total_median_ms": (statistics.median(complete) if complete else float("nan")),
        "total_min_ms": (complete[0] if complete else float("nan")),
        "total_p95_ms": _pct(0.95),
        "total_failures": sum(per_query_failures.values()),
    }


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
            idx_name = f"{BENCH_INDEX_PREFIX}{table}_{'_'.join(cols)}"[:63]
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
    """
    Drops all created test indexes.

    Runs in autocommit so one failing DROP cannot abort the transaction and
    silently turn every subsequent DROP into a no-op -- a leaked index would
    contaminate every later strategy and the next run's baseline.

    Sweeps by the BENCH_INDEX_PREFIX rather than trusting `created_names`: when
    CREATE INDEX fails partway, the caller never receives the names of the
    indexes that *were* built, so a name-only cleanup would leave them behind.
    """
    try:
        conn.commit()
    except Exception:
        conn.rollback()

    previous_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname LIKE %s;",
                (BENCH_INDEX_PREFIX + "%",),
            )
            targets = {r[0] for r in cur.fetchall()}
            targets.update(name for _, name in created_names)

            orphans = targets - {name for _, name in created_names}
            if orphans:
                logger.warning("Sweeping %d orphaned benchmark index(es): %s",
                               len(orphans), sorted(orphans))

            for idx_name in sorted(targets):
                try:
                    cur.execute(f"DROP INDEX IF EXISTS {idx_name};")
                except Exception as e:
                    logger.warning("Failed to drop index %s: %s", idx_name, e)

            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname LIKE %s;",
                (BENCH_INDEX_PREFIX + "%",),
            )
            leaked = [r[0] for r in cur.fetchall()]
    finally:
        conn.autocommit = previous_autocommit

    if leaked:
        raise RuntimeError(
            f"Benchmark indexes survived cleanup and would contaminate later "
            f"measurements: {leaked}. Drop them manually before re-running."
        )

