"""
tests/bench_common/harness.py
-----------------------------
Schema-agnostic experiment harness shared by every benchmark suite.

Holds the parts that are identical whichever schema is under test:

  - capture_existing_indexes : record C0, flagging benchmark leftovers
  - run_baseline             : measure against the pre-existing configuration
  - build_measure_drop       : build C*, measure, always clean up
  - write_strategy_csv / write_selected_config : result persistence

What differs between suites is only *how C\\* is chosen* -- tests/pgbench runs
an observation window per strategy, tests/tpcc observes once and reuses it --
so index selection stays in each suite's own driver.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .runner import (
    create_physical_indexes,
    drop_physical_indexes,
    measure_read_queries,
    prewarm_tables,
)

logger = logging.getLogger(__name__)


def write_strategy_csv(label: str, results: Dict, storage_mb: float, csv_dir: Path) -> Path:
    """Save execution metrics for a strategy to CSV."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{label}.csv"
    failures = results.get("per_query_failures", {})
    passes = results.get("per_query_passes", {})

    def _row(name: str, ms: float, samples="", failed=""):
        return [name, f"{ms / 1000.0:.6f}", f"{ms:.3f}", samples, failed]

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "avg_time_seconds", "avg_time_ms", "samples", "failures"])
        for q, t_ms in sorted(results["per_query_avg_ms"].items()):
            writer.writerow(_row(q, t_ms, len(passes.get(q, [])), failures.get(q, 0)))
        writer.writerow(_row("TOTAL", results["total_avg_ms"]))
        writer.writerow(_row("TOTAL_MEDIAN", results.get("total_median_ms", float("nan"))))
        writer.writerow(_row("TOTAL_MIN", results.get("total_min_ms", float("nan"))))
        writer.writerow(_row("TOTAL_P95", results.get("total_p95_ms", float("nan"))))
        writer.writerow(["STORAGE_MB", f"{storage_mb:.2f}", f"{storage_mb:.2f}", "", ""])
        writer.writerow(["FAILED_QUERIES", "0", "0", "", results.get("total_failures", 0)])
    return csv_path


def write_selected_config(label: str, selected, write_pen, csv_dir: Path,
                          existing: Optional[List[Dict]] = None) -> Path:
    """
    Persist the recommended configuration C* alongside the timings.

    Flags any recommendation redundant with an index that already exists, which
    is otherwise invisible in the results and silently wastes storage.
    """
    configs_dir = csv_dir.parent / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    path = configs_dir / f"{label}.json"

    existing_cols = set()
    for idx in existing or []:
        definition = idx.get("definition") or ""
        if "(" in definition:
            cols = tuple(c.strip().split()[0] for c in definition.split("(", 1)[1].rsplit(")", 1)[0].split(","))
            existing_cols.add((idx["table"], cols))

    entries = []
    for table, cols in sorted(selected):
        cols = tuple(cols)
        redundant = any(
            t == table and cols == e[: len(cols)]
            for t, e in existing_cols
        )
        entries.append({
            "table": table,
            "columns": list(cols),
            "write_penalty": (write_pen(table, cols) if write_pen else 0.0),
            "redundant_with_existing": redundant,
        })
    path.write_text(json.dumps({"label": label, "indexes": entries}, indent=2))
    return path


def warn_if_invalid(label: str, results: Dict) -> None:
    """Make a run contaminated by query failures impossible to miss."""
    failed = results.get("total_failures", 0)
    if not failed:
        return
    per_query = results.get("per_query_failures", {})
    broken = ", ".join(f"{q}x{n}" for q, n in sorted(per_query.items()) if n)
    print(f"  ⚠ INVALID [{label}]: {failed} query execution(s) failed ({broken}).")
    print(f"    Totals for this strategy are NOT comparable to the baseline.")


def capture_existing_indexes(conn) -> List[Dict]:
    """
    Read every index present before measurement, flagging benchmark leftovers.

    This deliberately does NOT filter out the benchmark prefix -- a stale index
    surviving from an interrupted earlier run is exactly the contamination this
    snapshot exists to expose. Filtering it out would hide the evidence.
    """
    from .runner import BENCH_INDEX_PREFIX

    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' ORDER BY tablename, indexname;"
        )
        return [
            {
                "table": t,
                "index": n,
                "definition": d,
                "benchmark_leftover": n.startswith(BENCH_INDEX_PREFIX),
            }
            for t, n, d in cur.fetchall()
        ]


def sweep_stale_indexes(conn, verbose: bool = True) -> List[str]:
    """
    Remove benchmark indexes left behind by an interrupted earlier run.

    Must happen BEFORE the baseline is measured: a leftover index would make the
    baseline a fully-indexed measurement, and every strategy would then appear
    slower than "baseline".
    """
    stale = [i["index"] for i in capture_existing_indexes(conn) if i["benchmark_leftover"]]
    if stale:
        if verbose:
            print(f"  ⚠ Found {len(stale)} stale benchmark index(es) from a previous run; removing:")
            for name in stale:
                print(f"      {name}")
        drop_physical_indexes(conn, [])
    return stale


def run_baseline(
    conn,
    queries: List[Tuple[str, str]],
    iterations: int = 3,
    csv_dir: Optional[Path] = None,
    label: str = "baseline",
) -> Dict:
    """
    Measure the workload against whatever indexes already exist (C0).

    This is NOT necessarily an index-free baseline -- if the schema has primary
    keys they remain in place. The captured C0 records exactly what was present.
    """
    out_dir = Path(csv_dir)
    print("\n" + "=" * 65)
    print(f" [Baseline:{label}] Measuring against the pre-existing configuration (C0)")
    print("=" * 65)

    stale = sweep_stale_indexes(conn)
    existing = capture_existing_indexes(conn)

    configs_dir = out_dir.parent / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / f"{label}_existing_indexes.json").write_text(
        json.dumps({"label": label, "existing_indexes": existing,
                    "swept_before_measuring": stale}, indent=2)
    )
    print(f"  [C0] {len(existing)} pre-existing index(es) retained (not dropped).")

    prewarm_tables(conn, verbose=False)
    read_results = measure_read_queries(conn, queries, iterations=iterations, warmup=True)
    write_strategy_csv(label, read_results, 0.0, out_dir)
    warn_if_invalid(label, read_results)
    print(f"  ✓ Baseline total: {read_results['total_avg_ms']:.2f} ms "
          f"({read_results['total_avg_ms'] / 1000.0:.3f} s)")
    return {
        "label": label,
        "read_results": read_results,
        "existing_indexes": existing,
        "storage_mb": 0.0,
        "total_seconds": read_results["total_avg_ms"] / 1000.0,
        "total_ms": read_results["total_avg_ms"],
    }


def build_measure_drop(
    conn,
    queries: List[Tuple[str, str]],
    label: str,
    selected,
    write_pen,
    iterations: int,
    csv_dir: Path,
    existing: Optional[List[Dict]] = None,
) -> Dict:
    """
    Build C*, measure the workload against it, then always drop it.

    Creation sits inside the try so a failure partway through still drops
    whatever was already built -- a leaked index contaminates every later
    strategy and the next run's baseline.
    """
    created_indexes: List[Tuple[str, str]] = []
    build_time_s = 0.0
    storage_mb = 0.0
    try:
        created_indexes, build_time_s, storage_mb = create_physical_indexes(conn, selected)
        print(f"  2. Physical build: {len(created_indexes)} index(es) in "
              f"{build_time_s:.3f}s ({storage_mb:.2f} MB)")
        read_results = measure_read_queries(conn, queries, iterations=iterations, warmup=True)
    finally:
        drop_physical_indexes(conn, created_indexes)
        print("  3. Cleanup: dropped benchmark indexes.")

    write_strategy_csv(label, read_results, storage_mb, csv_dir)
    write_selected_config(label, selected, write_pen, csv_dir, existing)
    warn_if_invalid(label, read_results)
    print(f"  ✓ Total workload latency: {read_results['total_avg_ms']:.2f} ms "
          f"({read_results['total_avg_ms'] / 1000.0:.3f} s)")

    return {
        "label": label,
        "selected_config": selected,
        "read_results": read_results,
        "storage_mb": storage_mb,
        "build_time_s": build_time_s,
        "total_seconds": read_results["total_avg_ms"] / 1000.0,
        "total_ms": read_results["total_avg_ms"],
    }
