"""
tests/tpcc/run_experiments.py
-----------------------------
Comprehensive TPC-C Multi-Combination Benchmark Experiment Suite:
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
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
from dotenv import load_dotenv

from .workload import load_tpcc_queries
from .strategy_runner import run_baseline, run_strategy
from .plot import generate_all_plots

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


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


def main():
    parser = argparse.ArgumentParser(description="Run Multi-Combination TPC-C Benchmark Experiments")
    parser.add_argument("--db", type=str, default=None, help="Target database name (default: from .env)")
    parser.add_argument("--window", type=int, default=10, help="AIS observation window duration in seconds (default: 10)")
    parser.add_argument("--rounds", type=int, default=15, help="Background DML traffic rounds (default: 15)")
    parser.add_argument("--scale", type=float, default=1.0, help="Write penalty scale factor (default: 1.0)")
    parser.add_argument("--iterations", type=int, default=2, help="Measurement passes per query (default: 2)")
    parser.add_argument("--k", nargs="+", type=int, default=[2, 3, 5, 7, 10], help="K values for config_sel (default: 2 3 5 7 10)")
    parser.add_argument("--budget", nargs="+", type=int, default=[100, 250, 500, 1000], help="Budget MB values for drop/extend (default: 100 250 500 1000)")
    parser.add_argument("--only", nargs="+", type=str, default=None, help="Filter strategies to run (e.g. --only baseline config_sel cs_extend)")
    parser.add_argument("--output-dir", type=str, default=None, help="Directory to save run results (defaults to tests/tpcc/results/run_TIMESTAMP)")
    parser.add_argument("--plot-only", action="store_true", help="Skip benchmarks and re-generate plots from existing CSVs")

    args = parser.parse_args()

    if args.plot_only:
        print("[Plotting] Generating comparison plots from existing CSV results...")
        target_dir = Path(args.output_dir) if args.output_dir else RESULTS_DIR
        generate_all_plots(target_dir)
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
    db_name = args.db or os.getenv("DB_NAME", "tpcc_db")

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
        queries = load_tpcc_queries()
        print(f"[Workload] Loaded {len(queries)} TPC-C queries.")

        # 1. Run Baseline Measurement (No Indexes)
        run_base = args.only is None or "baseline" in [x.lower() for x in args.only]
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

        # 3. Generate Comparative Plots
        print("\n" + "=" * 65)
        print(" [Plotting] Generating Comparative Visualizations")
        print("=" * 65)
        generate_all_plots(run_dir)

    finally:
        conn.close()
        print("[Setup] PostgreSQL connection closed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
