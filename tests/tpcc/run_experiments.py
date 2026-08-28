"""
tests/tpcc/run_experiments.py
TPC-C (pgbench) strategy comparison framework.
Fully self-contained within tests/tpcc/.

Usage
-----
# Full run (all CG × CS combinations, 3 iterations)
PYTHONPATH=src python tests/tpcc/run_experiments.py

# Quick smoke test: baseline + config_sel k=5, 1 iteration
PYTHONPATH=src python tests/tpcc/run_experiments.py \
  --only baseline config_sel --k 5 --iterations 1

# Re-generate plots from existing CSVs
PYTHONPATH=src python tests/tpcc/run_experiments.py --plot-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import psycopg2
from dotenv import load_dotenv

# Repo root on sys.path so both src/ and tests/ are importable
_REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

# Self-contained TPC-C modules
from tests.tpcc.strategy_runner import run_baseline, run_strategy
from tests.tpcc.measure import prewarm_database_tables
from tests.tpcc.plot import generate_all_plots
from tests.tpcc.tpcc_workload import load_tpcc_queries, fetch_schema

RESULTS_DIR = Path(__file__).parent / "results"

# ---------------------------------------------------------------------------
# Experiment definitions (same matrix as tpch)
# ---------------------------------------------------------------------------

K_VALUES       = [2, 3, 5, 7, 10]
BUDGET_MB_VALUES = [100, 250, 500, 1000]


def build_experiment_list() -> List[dict]:
    experiments = []

    # config_sel k-sweep with cg_rule_based
    for k in K_VALUES:
        experiments.append({
            "cg": "cg_rule_based",
            "cs": "config_sel",
            "label": f"cg_rule_based_config_sel_k{k}",
            "kwargs": {"m": 2, "k": k},
        })

    # cs_drop budget-sweep
    for mb in BUDGET_MB_VALUES:
        experiments.append({
            "cg": "cg_rule_based",
            "cs": "cs_drop",
            "label": f"cg_rule_based_cs_drop_mb{mb}",
            "kwargs": {"storage_budget": mb * 1024 * 1024},
        })
    experiments.append({
        "cg": "cg_rule_based",
        "cs": "cs_drop",
        "label": "cg_rule_based_cs_drop_mbinf",
        "kwargs": {"storage_budget": float("inf")},
    })

    # cs_extend budget-sweep
    for mb in BUDGET_MB_VALUES:
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

    # Other CG modules with config_sel k=10
    for cg in ["cg_auto_admin", "cg_dta", "cg_naive"]:
        experiments.append({
            "cg": cg,
            "cs": "config_sel",
            "label": f"{cg}_config_sel_k10",
            "kwargs": {"m": 2, "k": 10},
        })

    return experiments


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TPC-C (pgbench) strategy comparison")
    p.add_argument("--only", nargs="+", metavar="KEY")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--k", type=int, default=None)
    p.add_argument("--plot-only", action="store_true")
    p.add_argument("--no-plot", action="store_true")
    p.add_argument(
        "--no-warmup", action="store_true",
        help="Skip the per-strategy pre-measurement warm-up pass.",
    )
    p.add_argument(
        "--no-prewarm", action="store_true",
        help="Skip the global database table pre-warming pass.",
    )
    p.add_argument("--run-dir", type=str, default=None, help="Specific run directory under results/ to plot")
    return p.parse_args()


def get_connection():
    load_dotenv()
    return psycopg2.connect(
        dbname=os.getenv("TPCC_DB_NAME", "tpcc_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "123"),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    if args.plot_only:
        target_dir = Path(args.run_dir) if (args.run_dir and Path(args.run_dir).is_dir()) else (RESULTS_DIR / args.run_dir if args.run_dir else RESULTS_DIR)
        print(f"[run_experiments/tpcc] Regenerating plots from CSVs in: {target_dir}...")
        generate_all_plots(results_dir=target_dir)
        return 0

    conn = get_connection()
    print(f"Connected to: {conn.get_dsn_parameters()['dbname']}")

    queries = load_tpcc_queries()
    print(f"Loaded {len(queries)} TPC-C queries from scripts/queries/tpcc/")

    schema = fetch_schema(conn)
    print(f"Schema loaded: {len(schema)} tables\n")

    # ---- Global prewarm: load all table data pages into memory upfront ----
    if not args.no_prewarm:
        prewarm_database_tables(conn, schema)

    experiments = build_experiment_list()

    # Deduplicate if --k overrides all k values to the same number
    if args.k is not None:
        seen_labels = set()
        new_experiments = []
        for exp in experiments:
            if "k" in exp["kwargs"]:
                exp["kwargs"]["k"] = args.k
                exp["label"] = re.sub(r"_k\d+$", f"_k{args.k}", exp["label"])
            if exp["label"] not in seen_labels:
                seen_labels.add(exp["label"])
                new_experiments.append(exp)
        experiments = new_experiments

    if args.only:
        keys_lower = [k.lower() for k in args.only]
        run_baseline_flag = any("baseline" in k for k in keys_lower)
        experiments = [
            e for e in experiments
            if any(k in e["label"].lower() for k in keys_lower if k != "baseline")
        ]
    else:
        run_baseline_flag = True

    total_experiments = len(experiments) + (1 if run_baseline_flag else 0)
    print(f"Total experiments to run: {total_experiments} × {args.iterations} iterations\n")

    # ---- Setup timestamped run directory structure ----
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / f"run_{timestamp}"
    csv_dir = run_dir / "csv"
    plots_dir = run_dir / "plots"
    indexes_dir = run_dir / "indexes"

    csv_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output Directory: {run_dir}")
    print(f"  CSVs:    {csv_dir}")
    print(f"  Plots:   {plots_dir}")
    print(f"  Indexes: {indexes_dir}")

    warmup = not args.no_warmup
    print(f"Pre-warmup cache: {'ENABLED (fair buffer cache)' if warmup else 'DISABLED (cold start)'}")
    print()

    results_summary = {}
    t_start = time.time()

    if run_baseline_flag:
        r = run_baseline(
            conn, queries, iterations=args.iterations,
            results_dir=csv_dir,
            warmup=warmup,
        )
        results_summary["baseline"] = r["total"]

    for i, exp in enumerate(experiments, 1):
        print(f"\n[{i}/{len(experiments)}] {exp['label']}")
        try:
            r = run_strategy(
                conn=conn,
                cg_name=exp["cg"],
                cs_name=exp["cs"],
                queries=queries,
                schema=schema,
                label=exp["label"],
                cs_kwargs=exp["kwargs"],
                iterations=args.iterations,
                results_dir=csv_dir,
                indexes_dir=indexes_dir,
                warmup=warmup,
            )
            results_summary[exp["label"]] = r["total"]
        except Exception as exc:
            print(f"  [ERROR] {exp['label']} failed: {exc}")
            try:
                conn.rollback()
            except Exception:
                pass

    wall = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"All experiments done in {wall/60:.1f} minutes.")
    print(f"\nTotal time summary (avg seconds):")
    summary_lines = []
    for label, total in sorted(results_summary.items(), key=lambda x: x[1]):
        line = f"  {label:45s}  {total:.3f}s"
        print(line)
        summary_lines.append(f"{label}: {total:.6f}")

    # Save summary files
    (run_dir / "summary.txt").write_text(
        f"Workload: TPC-C\n"
        f"Timestamp: {timestamp}\n"
        f"Duration: {wall/60:.2f} minutes\n"
        f"Iterations: {args.iterations}\n\n"
        f"Rankings (avg seconds):\n" + "\n".join(summary_lines) + "\n"
    )

    with open(run_dir / "summary.json", "w") as f:
        json.dump({
            "workload": "TPC-C",
            "timestamp": timestamp,
            "duration_seconds": wall,
            "iterations": args.iterations,
            "results": results_summary,
        }, f, indent=2)

    conn.close()

    if not args.no_plot:
        print("\nGenerating comparison charts...")
        generate_all_plots(results_dir=run_dir, plots_dir=plots_dir)

    print(f"\n[DONE] All results and plots saved inside: {run_dir}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
