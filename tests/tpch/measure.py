"""
tests/tpch/measure.py
Executes the TPC-H workload against the live database and records
wall-clock time for each query across multiple iterations.

Timeout behaviour
-----------------
- statement_timeout_ms  : PostgreSQL-level statement timeout. Queries that
  exceed this are cancelled by the server (QueryCanceled exception).
- skip_timed_out        : If True  — timed-out queries are excluded from the
                          per-query averages AND from the total workload time.
                          If False — timed-out queries contribute 0s to the
                          total (they ran but exceeded the limit).
  The default is True (recommended): only successfully completed queries are
  used for fair comparison between strategies.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

RESULTS_DIR = Path(__file__).parent / "results"

# Sentinel value stored when a query times out
_TIMED_OUT = "TIMED_OUT"


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
        print("[Prewarm] Warming all database tables into RAM buffer cache...")
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
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
    statement_timeout_ms: Optional[int] = 30_000,
    skip_timed_out: bool = True,
    warmup: bool = True,
) -> Dict:
    """
    Run every query in `queries` for `iterations` passes.

    Parameters
    ----------
    conn                 : psycopg2 connection (autocommit=False)
    queries              : list of (label, sql) tuples
    iterations           : number of measurement passes
    statement_timeout_ms : PostgreSQL statement timeout in milliseconds.
                           Pass None (or 0) to disable the timeout entirely.
    skip_timed_out       : Whether to exclude timed-out queries from totals.
                           If False: timeout duration is added as a penalty time.
    warmup               : Run an unrecorded initial pass to pre-warm the cache.

    Returns
    -------
    {
        "per_query"   : {label: avg_seconds},   # only completed queries
        "timed_out"   : {label},                 # set of query labels that timed out
        "total"       : avg_total_seconds,       # sum of completed queries only
        "iterations"  : iterations,
    }
    """
    per_query_sums: Dict[str, float] = {}
    per_query_counts: Dict[str, int] = {}
    timed_out_labels: Set[str] = set()
    total_times: List[float] = []

    # Build SET command once
    if statement_timeout_ms and statement_timeout_ms > 0:
        timeout_sql = f"SET statement_timeout = {statement_timeout_ms};"
        timeout_label_ms = statement_timeout_ms
    else:
        timeout_sql = "SET statement_timeout = 0;"   # 0 = disabled
        timeout_label_ms = None

    # ---- 0. Pre-measurement Warm-up pass ----
    if warmup:
        print("  [Warmup] Pre-warming buffer cache...", end=" ", flush=True)
        t_w0 = time.perf_counter()
        for label, sql in queries:
            with conn.cursor() as cur:
                cur.execute(timeout_sql)
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
            # Re-apply timeout before every query (reset after any rollback)
            with conn.cursor() as cur:
                cur.execute(timeout_sql)

            t0 = time.perf_counter()
            timed_out = False
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
                err = str(exc).lower()
                if "canceling statement" in err or "statement timeout" in err or "query_canceled" in err:
                    timed_out = True
                    timed_out_labels.add(label)
                    print(f"\n  [TIMEOUT] {label} exceeded {timeout_label_ms}ms — "
                          f"{'excluded' if skip_timed_out else 'counted penalty'}", flush=True)
                else:
                    print(f"\n  [ERROR] {label}: {exc}", flush=True)

            if timed_out:
                if not skip_timed_out:
                    penalty = (statement_timeout_ms / 1000.0) if statement_timeout_ms else elapsed
                    iter_total += penalty
                    per_query_sums[label] = per_query_sums.get(label, 0.0) + penalty
                    per_query_counts[label] = per_query_counts.get(label, 0) + 1
            else:
                per_query_sums[label] = per_query_sums.get(label, 0.0) + elapsed
                per_query_counts[label] = per_query_counts.get(label, 0) + 1
                iter_total += elapsed

            print(label, end=" ", flush=True)

        total_times.append(iter_total)
        print(f"| total={iter_total:.3f}s")

    # Compute averages — only over iterations where query actually ran
    per_query_avg: Dict[str, float] = {}
    for lbl in per_query_sums:
        count = per_query_counts.get(lbl, 1)
        per_query_avg[lbl] = per_query_sums[lbl] / count if count > 0 else 0.0

    avg_total = sum(total_times) / len(total_times) if total_times else 0.0

    # Print summary of timed-out queries
    if timed_out_labels:
        print(f"\n  Timed-out queries (excluded from results): {sorted(timed_out_labels)}")

    return {
        "per_query": per_query_avg,
        "timed_out": timed_out_labels,
        "total": avg_total,
        "iterations": iterations,
    }


def write_csv(
    results: Dict,
    label: str,
    results_dir: Path = RESULTS_DIR,
) -> Tuple[Path, Path]:
    """
    Write two CSV files:
      - <label>_qt.csv  : per-query avg time (timed-out queries marked "TIMED_OUT")
      - <label>_avg.csv : total avg time + number of timed-out queries
    Returns (query_csv_path, total_csv_path).
    """
    results_dir.mkdir(parents=True, exist_ok=True)

    qt_path = results_dir / f"{label}_qt.csv"
    avg_path = results_dir / f"{label}_avg.csv"

    per_query = results["per_query"]
    timed_out = results.get("timed_out", set())

    # All expected query labels (including timed-out ones)
    all_labels = _sort_labels(
        list(per_query.keys()) + [l for l in timed_out if l not in per_query]
    )

    with open(qt_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "avg_time_seconds", "status"])
        for lbl in all_labels:
            if lbl in timed_out:
                w.writerow([lbl, _TIMED_OUT, "TIMED_OUT"])
            else:
                w.writerow([lbl, f"{per_query[lbl]:.6f}", "OK"])

    with open(avg_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "avg_total_time_seconds", "timed_out_queries"])
        w.writerow([
            label,
            f"{results['total']:.6f}",
            ",".join(sorted(timed_out)) if timed_out else "",
        ])

    return qt_path, avg_path


def _sort_labels(labels) -> List[str]:
    def _key(lbl):
        digits = "".join(c for c in str(lbl) if c.isdigit())
        return (int(digits) if digits else 0, str(lbl))
    return sorted(set(labels), key=_key)
