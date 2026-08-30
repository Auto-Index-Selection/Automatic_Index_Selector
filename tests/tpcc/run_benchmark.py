"""
tests/tpcc/run_benchmark.py
---------------------------
CLI Entry point for running the dedicated TPC-C index evaluation experiment.

Usage::

    PYTHONPATH=src /home/pratik/Extend/venv/bin/python -m tests.tpcc.run_benchmark --window 15 --rounds 20 --iterations 3 --plot
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from .runner import run_tpcc_experiment
from .plot import plot_tpcc_results


def main():
    parser = argparse.ArgumentParser(description="Run TPC-C Automatic Index Selection Benchmark & Physical Evaluation")
    parser.add_argument("--db", type=str, default=None, help="Target database (defaults to DB_NAME from .env)")
    parser.add_argument("--window", type=int, default=10, help="Observation window in seconds (default: 10)")
    parser.add_argument("--rounds", type=int, default=20, help="DML write rounds during simulation (default: 20)")
    parser.add_argument("--iterations", type=int, default=3, help="Number of measurement passes per query (default: 3)")
    parser.add_argument("--no-plot", action="store_true", help="Skip generating comparison charts")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for results (default: tests/tpcc/results)")

    args = parser.parse_args()

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

    from datetime import datetime

    results_base = Path(__file__).resolve().parent / "results"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_dir) if args.output_dir else (results_base / f"run_{timestamp}")
    run_dir.mkdir(parents=True, exist_ok=True)

    # Maintain latest symlink
    latest_symlink = results_base / "latest"
    try:
        if latest_symlink.is_symlink() or latest_symlink.exists():
            latest_symlink.unlink()
        latest_symlink.symlink_to(run_dir.name, target_is_directory=True)
    except Exception:
        pass

    print(f"[Results] Output Run Directory: {run_dir}")

    try:
        results = run_tpcc_experiment(
            conn=conn,
            window_seconds=args.window,
            rounds=args.rounds,
            iterations=args.iterations,
            output_dir=run_dir,
        )

        if not args.no_plot and "csv_path" in results:
            print("\n--- [Plotting] Generating Comparison Charts ---")
            plot_tpcc_results(results["csv_path"], plots_dir=run_dir / "plots")

    finally:
        conn.close()
        print("[Setup] PostgreSQL connection closed.")


if __name__ == "__main__":
    sys.exit(main())
