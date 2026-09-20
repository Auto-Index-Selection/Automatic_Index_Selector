"""
tests/pgbench/run_experiments.py
-----------------------------
Comprehensive pgbench (TPC-B) Multi-Combination Benchmark Experiment Suite:
1. Measures Baseline (0 indexes) once.
2. For each combination:
   - Starts concurrent background traffic thread (after 2s delay)
   - Invokes AIS (src/auto_index_selector) with observation window & write penalty calculation
   - AIS observes DML writes, computes dynamic write penalties, and selects C*
   - Physically builds C* in PostgreSQL (CREATE INDEX)
   - Measures actual wall-clock execution latency across iterations
   - Physically cleans up C* (DROP INDEX)
3. Generates 4 publication comparison charts saved in timestamped run folders:
   - total_comparison.png (All strategies vs Baseline)
   - heatmap_per_query.png (Per-query breakdown)
   - k_scaling.png (K-parameter scaling)
   - budget_scaling.png (Storage budget scaling)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
from dotenv import load_dotenv

from tests.bench_common.harness import run_baseline
from tests.bench_common.plot import generate_all_plots
from tests.bench_common.report import write_summary
from .workload import load_pgbench_queries
from .strategy_runner import run_strategy

SUITE = "pgbench (TPC-B)"

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# The database this suite's queries are written against. Override with --db.
DEFAULT_DB = "tpcc_db"


def verify_schema(conn, queries: List, db_name: str) -> None:
    """
    Fail fast if the target database lacks the tables the queries reference.

    Running the full matrix against the wrong database wastes a long run and,
    before failed queries were counted, produced a fictitious 0.00 ms 'result'.
    """
    referenced = set()
    for _, sql in queries:
        referenced.update(re.findall(r"(?:FROM|JOIN|UPDATE|INTO)\s+([a-zA-Z_][\w]*)", sql, re.IGNORECASE))

    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
        present = {r[0].lower() for r in cur.fetchall()}

    missing = sorted(t for t in referenced if t.lower() not in present)
    if missing:
        raise SystemExit(
            f"\n[Setup] ABORT: database '{db_name}' is missing {len(missing)} table(s) "
            f"the workload queries reference: {missing}\n"
            f"        Tables present: {sorted(present) or '<none>'}\n"
            f"        Pass the right database with --db (this suite targets '{DEFAULT_DB}')."
        )
    print(f"[Setup] Schema check passed: all {len(referenced)} referenced table(s) present in '{db_name}'.")


def build_experiment_list(k_values: List[int], budget_values: List[int], only_filter: Optional[List[str]] = None) -> List[dict]:
    """Constructs the full list of algorithmic combinations to evaluate."""
    experiments = []

    def should_include(name: str) -> bool:
        if not only_filter:
            return True
        return any(f.lower() in name.lower() for f in only_filter)

    # 1. GreedyMK (config_sel) k-sweep
    if should_include("config_sel") or should_include("greedy"):
        for k in k_values:
            experiments.append({
                "cg": "cg_rule_based",
                "cs": "config_sel",
                "label": f"cg_rule_based_config_sel_k{k}",
                "kwargs": {"m": 2, "k": k},
            })

    # 2. Drop Heuristic (cs_drop) budget-sweep
    if should_include("cs_drop") or should_include("drop"):
        for mb in budget_values:
            experiments.append({
                "cg": "cg_rule_based",
                "cs": "cs_drop",
                "label": f"cg_rule_based_cs_drop_mb{mb}",
                "kwargs": {"storage_budget": mb * 1024 * 1024, "budget_mb": mb},
            })
        experiments.append({
            "cg": "cg_rule_based",
            "cs": "cs_drop",
            "label": "cg_rule_based_cs_drop_mbinf",
            "kwargs": {"storage_budget": float("inf"), "budget_mb": float("inf")},
        })

    # 3. Extend Algorithm (cs_extend) budget-sweep
    if should_include("cs_extend") or should_include("extend"):
        for mb in budget_values:
            experiments.append({
                "cg": "cg_rule_based",
                "cs": "cs_extend",
                "label": f"cg_rule_based_cs_extend_mb{mb}",
                "kwargs": {"budget_mb": float(mb)},
            })
        experiments.append({
            "cg": "cg_rule_based",
            "cs": "cs_extend",
            "label": "cg_rule_based_cs_extend_mbinf",
            "kwargs": {"budget_mb": float("inf")},
        })

    # 4. Other Candidate Generators with config_sel k=10
    for cg in ["cg_auto_admin", "cg_dta", "cg_naive"]:
        if should_include(cg):
            experiments.append({
                "cg": cg,
                "cs": "config_sel",
                "label": f"{cg}_config_sel_k10",
                "kwargs": {"m": 2, "k": 10},
            })

    return experiments


def _report_baseline_drift(opening: Optional[Dict], closing: Dict, tolerance_pct: float = 5.0) -> None:
    """
    Compare the opening and closing baselines. A large gap means the database or
    machine drifted during the run, so per-strategy differences of that size are
    not attributable to the strategies.
    """
    if not opening:
        return
    t0, t1 = opening["total_ms"], closing["total_ms"]
    if not t0 or t0 != t0:  # zero or NaN
        return
    drift_pct = ((t1 - t0) / t0) * 100.0
    print("\n" + "=" * 65)
    print(f" [Drift] Opening baseline: {t0:.2f} ms | Closing baseline: {t1:.2f} ms "
          f"({drift_pct:+.1f}%)")
    if abs(drift_pct) > tolerance_pct:
        print(f" ⚠ Baseline drifted more than {tolerance_pct:.0f}% during the run.")
        print("   Strategy differences smaller than this are not attributable to the strategy.")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Run Multi-Combination pgbench (TPC-B) Benchmark Experiments")
    parser.add_argument("--db", type=str, default=None, help="Target database name (default: from .env)")
    parser.add_argument("--window", type=int, default=10, help="AIS observation window duration in seconds (default: 10)")
    parser.add_argument("--rounds", type=int, default=15, help="Background DML traffic rounds (default: 15)")
    parser.add_argument("--scale", type=float, default=1.0, help="Write penalty scale factor (default: 1.0)")
    parser.add_argument("--iterations", type=int, default=5,
                        help="Measurement passes per query (default: 5). Below ~5 the "
                             "run-to-run spread exceeds the differences between strategies.")
    parser.add_argument("--k", nargs="+", type=int, default=[2, 3, 5, 7, 10], help="K values for config_sel (default: 2 3 5 7 10)")
    parser.add_argument("--budget", nargs="+", type=int, default=[100, 250, 500, 1000], help="Budget MB values for drop/extend (default: 100 250 500 1000)")
    parser.add_argument("--only", nargs="+", type=str, default=None, help="Filter strategies to run (e.g. --only baseline config_sel cs_extend)")
    parser.add_argument("--output-dir", type=str, default=None, help="Directory to save run results (defaults to tests/pgbench/results/run_TIMESTAMP)")
    parser.add_argument("--plot-only", action="store_true", help="Skip benchmarks and re-generate plots from existing CSVs")

    args = parser.parse_args()

    if args.plot_only:
        print("[Plotting] Generating comparison plots from existing CSV results...")
        target_dir = Path(args.output_dir) if args.output_dir else RESULTS_DIR
        generate_all_plots(target_dir, SUITE)
        write_summary(target_dir)
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_dir) if args.output_dir else (RESULTS_DIR / f"run_{timestamp}")
    csv_dir = run_dir / "csv"
    plots_dir = run_dir / "plots"

    csv_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Maintain 'latest' symlink
    latest_symlink = RESULTS_DIR / "latest"
    try:
        if latest_symlink.is_symlink() or latest_symlink.exists():
            latest_symlink.unlink()
        latest_symlink.symlink_to(run_dir.name, target_is_directory=True)
    except Exception:
        pass

    print(f"[Results] Output Run Directory: {run_dir}")
    print(f"  CSVs:  {csv_dir}")
    print(f"  Plots: {plots_dir}")

    load_dotenv()
    # Deliberately NOT falling back to DB_NAME: that variable belongs to the
    # TPC-H scripts (scripts/simulate_workload.py) and points at tpch_db, which
    # has none of this suite's tables. Inheriting it would run the whole matrix
    # against a database where every query fails.
    db_name = args.db or DEFAULT_DB

    print(f"[Setup] Connecting to PostgreSQL [{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/{db_name}]...")
    conn = psycopg2.connect(
        dbname=db_name,
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
    )

    try:
        # Load queries
        queries = load_pgbench_queries()
        verify_schema(conn, queries, db_name)
        print(f"[Workload] Loaded {len(queries)} pgbench queries.")

        # 1. Run Baseline Measurement (No Indexes)
        run_base = args.only is None or "baseline" in [x.lower() for x in args.only]
        baseline_result = None
        if run_base:
            baseline_result = run_baseline(conn, queries, iterations=args.iterations, csv_dir=csv_dir)

        # 2. Build and Run Experiments Matrix via AIS with Write Penalty Calculation
        experiments = build_experiment_list(args.k, args.budget, only_filter=args.only)
        print(f"\n[Matrix] Scheduled {len(experiments)} algorithmic configurations to evaluate via AIS (Window: {args.window}s).")

        for i, exp in enumerate(experiments, 1):
            print(f"\n>>> Running Experiment [{i}/{len(experiments)}]: {exp['label']}")
            run_strategy(
                conn=conn,
                queries=queries,
                cg_name=exp["cg"],
                cs_name=exp["cs"],
                label=exp["label"],
                iterations=args.iterations,
                window_seconds=args.window,
                dml_rounds=args.rounds,
                write_scale=args.scale,
                csv_dir=csv_dir,
                **exp["kwargs"]
            )

        # 2b. Re-measure the baseline. The opening baseline is taken before any
        #     traffic; the strategies then run serially for many minutes, each
        #     applying DML and building/dropping hundreds of MB of indexes. Any
        #     drift over that period would otherwise be silently attributed to
        #     whichever strategy happened to run last.
        if run_base and experiments:
            closing = run_baseline(conn, queries, iterations=args.iterations,
                                   csv_dir=csv_dir, label="baseline_closing")
            _report_baseline_drift(baseline_result, closing)

        # 3. Generate Comparative Plots
        print("\n" + "=" * 65)
        print(" [Plotting] Generating Comparative Visualizations")
        print("=" * 65)
        generate_all_plots(run_dir, SUITE)
        summary = write_summary(run_dir)
        if summary:
            print(f"[Report] {summary}")

    finally:
        conn.close()
        print("[Setup] PostgreSQL connection closed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
