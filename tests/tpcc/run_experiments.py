"""
tests/tpcc/run_experiments.py
-----------------------------
TPC-C multi-combination benchmark against tpcc_standard_db.

Per C0 arm (`bare`, then `pkeys`):

    setup C0            drop or create the standard TPC-C primary keys
    opening baseline    measure against C0 alone
    observe ONCE        snapshot -> run the TPC-C workload inline -> snapshot
                        -> W, query weights, write penalties
    for each (CG, CS):  select C* from that SHARED observation,
                        build it, measure, drop it
    closing baseline    detect drift over the arm

Observing once is what makes this a controlled comparison: every algorithm
combination sees the *identical* workload, so differences between them are
attributable to the algorithms rather than to workload sampling noise. It also
means the write transactions mutate the database once per arm instead of once
per combination.
"""
from __future__ import annotations

import argparse
import json
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

from auto_index_selector.__main__ import _merge_config, observe_workload, select_indexes
from tests.bench_common.harness import (
    build_measure_drop,
    capture_existing_indexes,
    run_baseline,
)
from tests.bench_common.plot import generate_all_plots
from tests.bench_common.report import write_summary
from .schema import setup_arm, table_row_counts
from .workload import connect, load_tpcc_queries, make_observation_hook

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SUITE = "TPC-C"

# The database this suite's queries are written against. Override with --db.
# Deliberately NOT read from .env's DB_NAME, which belongs to the TPC-H scripts.
DEFAULT_DB = "tpcc_standard_db"

ARMS = ["bare", "pkeys"]


def verify_schema(conn, queries: List, db_name: str) -> None:
    """Fail fast if the target database lacks the tables the queries reference."""
    referenced = set()
    for _, sql in queries:
        referenced.update(
            re.findall(r"(?:FROM|JOIN|UPDATE|INTO)\s+([a-zA-Z_][\w]*)", sql, re.IGNORECASE)
        )
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
        present = {r[0].lower() for r in cur.fetchall()}

    missing = sorted(t for t in referenced if t.lower() not in present)
    if missing:
        raise SystemExit(
            f"\n[Setup] ABORT: database '{db_name}' is missing {len(missing)} table(s) "
            f"the TPC-C workload references: {missing}\n"
            f"        Pass the right database with --db (this suite targets '{DEFAULT_DB}')."
        )
    print(f"[Setup] Schema check passed: all {len(referenced)} referenced table(s) present.")


def build_experiment_list(k_values: List[int], budget_values: List[int],
                          only_filter: Optional[List[str]] = None) -> List[dict]:
    """Construct the full list of algorithmic combinations to evaluate."""
    experiments = []

    def include(name: str) -> bool:
        return not only_filter or any(f.lower() in name.lower() for f in only_filter)

    if include("config_sel") or include("greedy"):
        for k in k_values:
            experiments.append({"cg": "cg_rule_based", "cs": "config_sel",
                                "label": f"cg_rule_based_config_sel_k{k}",
                                "kwargs": {"m": 2, "k": k}})

    if include("cs_drop") or include("drop"):
        for mb in budget_values:
            experiments.append({"cg": "cg_rule_based", "cs": "cs_drop",
                                "label": f"cg_rule_based_cs_drop_mb{mb}",
                                "kwargs": {"storage_budget": mb * 1024 * 1024, "budget_mb": mb}})
        experiments.append({"cg": "cg_rule_based", "cs": "cs_drop",
                            "label": "cg_rule_based_cs_drop_mbinf",
                            "kwargs": {"storage_budget": float("inf"), "budget_mb": float("inf")}})

    if include("cs_extend") or include("extend"):
        for mb in budget_values:
            experiments.append({"cg": "cg_rule_based", "cs": "cs_extend",
                                "label": f"cg_rule_based_cs_extend_mb{mb}",
                                "kwargs": {"budget_mb": float(mb)}})
        experiments.append({"cg": "cg_rule_based", "cs": "cs_extend",
                            "label": "cg_rule_based_cs_extend_mbinf",
                            "kwargs": {"budget_mb": float("inf")}})

    for cg in ["cg_auto_admin", "cg_dta", "cg_naive"]:
        if include(cg):
            experiments.append({"cg": cg, "cs": "config_sel",
                                "label": f"{cg}_config_sel_k10",
                                "kwargs": {"m": 2, "k": 10}})
    return experiments


def report_drift(opening: Optional[Dict], closing: Dict, tolerance_pct: float = 5.0) -> None:
    """Compare opening and closing baselines; a large gap invalidates the arm."""
    if not opening:
        return
    t0, t1 = opening["total_ms"], closing["total_ms"]
    if not t0 or t0 != t0:
        return
    drift = ((t1 - t0) / t0) * 100.0
    print("\n" + "=" * 65)
    print(f" [Drift] Opening {t0:.2f} ms | Closing {t1:.2f} ms ({drift:+.1f}%)")
    if abs(drift) > tolerance_pct:
        print(f" ⚠ Baseline drifted more than {tolerance_pct:.0f}% during this arm.")
        print("   Strategy differences smaller than this are not attributable to the strategy.")
    print("=" * 65)


def run_arm(conn, arm: str, queries, args, arm_dir: Path) -> None:
    """Run one complete C0 arm: setup, baseline, one observation, all combinations."""
    csv_dir, configs_dir = arm_dir / "csv", arm_dir / "configs"
    csv_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "#" * 65)
    print(f"#  ARM: {arm}   ({'no indexes at all' if arm == 'bare' else 'standard TPC-C primary keys'})")
    print("#" * 65)

    setup_arm(conn, arm)
    rows_before = table_row_counts(conn)

    baseline = run_baseline(conn, queries, iterations=args.iterations,
                            csv_dir=csv_dir, label="baseline")

    # --- Observe ONCE; every combination below reuses this exact observation ---
    print("\n" + "=" * 65)
    print(" [Observation] Running the TPC-C workload inline (single shared window)")
    print("=" * 65)
    hook, hook_stats = make_observation_hook(
        conn.info.dbname, args.read_rounds, args.txn_rounds, args.seed
    )
    cfg = _merge_config({
        "write_penalty": {"enabled": True, "window_duration_seconds": 0,
                          "write_scale": args.scale},
    })
    t0 = time.perf_counter()
    observation = observe_workload(conn, cfg, observation_hook=hook, verbose=True)
    observe_seconds = time.perf_counter() - t0

    if not observation:
        print(f"  ⚠ ABORT arm '{arm}': the observation window captured no queries.")
        return

    write_delta_summary = {
        t: {"inserts": d.delta_inserts, "updates": d.delta_updates,
            "deletes": d.delta_deletes,
            "column_sets": {"+".join(sorted(cs)): n
                            for cs, n in (d.column_set_update_rows or {}).items()}}
        for t, d in (observation.write_delta or {}).items()
    }
    (configs_dir / "observation.json").write_text(json.dumps({
        "arm": arm,
        "seconds": observe_seconds,
        "workload_execution": hook_stats,
        "queries_observed": len(observation.W),
        "query_weights": {q: observation.query_weights.get(q, 1.0) for q in observation.W},
        "write_delta": write_delta_summary,
        "row_counts_before": rows_before,
    }, indent=2, default=str))
    print(f"  ✓ Shared observation captured in {observe_seconds:.1f}s: "
          f"{len(observation.W)} queries, {len(write_delta_summary)} tables written.")

    experiments = build_experiment_list(args.k, args.budget, args.only)
    print(f"\n[Matrix] {len(experiments)} combination(s), all evaluated against the "
          f"SAME observation.")

    existing = capture_existing_indexes(conn)
    for i, exp in enumerate(experiments, 1):
        print("\n" + "-" * 65)
        print(f" [{i}/{len(experiments)}] {exp['label']}")
        print(f"   CG: {exp['cg']} | CS: {exp['cs']} | Params: {exp['kwargs']}")
        print("-" * 65)

        exp_cfg = _merge_config({
            "candidate_generation": {"module": exp["cg"]},
            "config_selection": {"module": exp["cs"], **exp["kwargs"]},
            "write_penalty": {"enabled": True, "window_duration_seconds": 0,
                              "write_scale": args.scale},
        })
        t_sel = time.perf_counter()
        selected = select_indexes(conn, exp_cfg, observation, verbose=False)
        print(f"  1. Selection: {len(selected)} index(es) in {time.perf_counter() - t_sel:.3f}s")
        for t, cols in sorted(selected):
            wp = observation.write_penalties
            pen = wp(t, tuple(cols)) if wp else 0.0
            print(f"     -> {t}({', '.join(cols)})  [write_penalty = {pen:.4f}]")

        build_measure_drop(conn, queries, exp["label"], selected,
                           observation.write_penalties, args.iterations, csv_dir, existing)

    closing = run_baseline(conn, queries, iterations=args.iterations,
                           csv_dir=csv_dir, label="baseline_closing")
    report_drift(baseline, closing)

    rows_after = table_row_counts(conn)
    (configs_dir / "row_counts.json").write_text(json.dumps(
        {"arm": arm, "before": rows_before, "after": rows_after,
         "growth": {t: rows_after.get(t, 0) - rows_before.get(t, 0) for t in rows_before}},
        indent=2))

    generate_all_plots(arm_dir, f"{SUITE} [{arm}]")
    summary = write_summary(arm_dir)
    if summary:
        print(f"  [Report] {summary}")


def main():
    p = argparse.ArgumentParser(description="Run TPC-C Multi-Combination Benchmark Experiments")
    p.add_argument("--db", default=None, help=f"Target database (default: {DEFAULT_DB})")
    p.add_argument("--arms", nargs="+", default=ARMS, choices=ARMS,
                   help="Which C0 arms to run (default: both)")
    p.add_argument("--read-rounds", type=int, default=3,
                   help="Read passes during the observation window (default: 3)")
    p.add_argument("--txn-rounds", type=int, default=2,
                   help="Transaction-mix rounds during the observation window (default: 2)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for transactions (default: 42)")
    p.add_argument("--scale", type=float, default=1.0, help="Write penalty scale (default: 1.0)")
    p.add_argument("--iterations", type=int, default=5,
                   help="Measurement passes per query (default: 5)")
    p.add_argument("--k", nargs="+", type=int, default=[2, 3, 5, 7, 10])
    p.add_argument("--budget", nargs="+", type=int, default=[100, 250, 500, 1000])
    p.add_argument("--only", nargs="+", default=None, help="Filter strategies to run")
    p.add_argument("--output-dir", default=None)
    p.add_argument("--plot-only", action="store_true")
    args = p.parse_args()

    if args.plot_only:
        target = Path(args.output_dir) if args.output_dir else RESULTS_DIR
        for arm in args.arms:
            if (target / arm).is_dir():
                generate_all_plots(target / arm, f"{SUITE} [{arm}]")
                write_summary(target / arm)
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_dir) if args.output_dir else (RESULTS_DIR / f"run_{ts}")
    run_dir.mkdir(parents=True, exist_ok=True)

    latest = RESULTS_DIR / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_dir.name, target_is_directory=True)
    except Exception:
        pass

    load_dotenv()
    db_name = args.db or DEFAULT_DB
    print(f"[Results] {run_dir}")
    print(f"[Setup] Connecting to "
          f"{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/{db_name}...")
    conn = connect(db_name)

    try:
        queries = load_tpcc_queries()
        verify_schema(conn, queries, db_name)
        print(f"[Workload] Loaded {len(queries)} TPC-C read queries.")

        for arm in args.arms:
            run_arm(conn, arm, queries, args, run_dir / arm)

        print("\n" + "=" * 65)
        print(" NOTE: the observation commits real TPC-C writes, so the arms see")
        print(" different data. Compare each arm against ITS OWN baseline, never")
        print(" across arms. See configs/row_counts.json for the growth per arm.")
        print("=" * 65)
    finally:
        conn.close()
        print("[Setup] Connection closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
