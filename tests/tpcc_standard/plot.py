"""
tests/tpcc_standard/plot.py
Visualisation module for official TPC-C experiment results.
Generates 4 comparison charts saved as PNG files into plots/.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = Path(__file__).parent / "results"
PLOTS_DIR   = RESULTS_DIR / "plots"


def find_csv_dir(results_dir: Path) -> Path:
    if (results_dir / "csv").is_dir():
        return results_dir / "csv"
    if list(results_dir.glob("*_avg.csv")):
        return results_dir
    run_dirs = sorted(results_dir.glob("run_*"))
    if run_dirs:
        latest_run = run_dirs[-1]
        if (latest_run / "csv").is_dir():
            return latest_run / "csv"
        return latest_run
    return results_dir


def _load_total(results_dir: Path) -> Dict[str, float]:
    csv_dir = find_csv_dir(results_dir)
    totals: Dict[str, float] = {}
    for avg_file in csv_dir.glob("*_avg.csv"):
        label = avg_file.name[:-len("_avg.csv")]
        total = 0.0
        with open(avg_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    total += float(row["avg_time_seconds"])
                except ValueError:
                    pass
        totals[label] = total
    return totals


def _load_per_query(results_dir: Path) -> Dict[str, Dict[str, float]]:
    csv_dir = find_csv_dir(results_dir)
    per_query: Dict[str, Dict[str, float]] = {}
    for avg_file in csv_dir.glob("*_avg.csv"):
        label = avg_file.name[:-len("_avg.csv")]
        per_query[label] = {}
        with open(avg_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    per_query[label][row["query"]] = float(row["avg_time_seconds"])
                except ValueError:
                    per_query[label][row["query"]] = 0.0
    return per_query


def plot_total_comparison(totals: Dict[str, float], plots_dir: Optional[Path] = None) -> None:
    if not totals:
        return
    out_dir = plots_dir or PLOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    sorted_items = sorted(totals.items(), key=lambda x: x[1])
    labels = [k for k, _ in sorted_items]
    times  = [v for _, v in sorted_items]
    baseline_time = totals.get("baseline", None)

    colors = []
    for lbl in labels:
        if lbl == "baseline":
            colors.append("#e74c3c")
        elif "config_sel" in lbl:
            colors.append("#2ecc71")
        elif "cs_drop" in lbl:
            colors.append("#3498db")
        elif "cs_extend" in lbl:
            colors.append("#9b59b6")
        else:
            colors.append("#95a5a6")

    fig, ax = plt.subplots(figsize=(12, max(6, len(labels) * 0.45)))
    y_pos = np.arange(len(labels))
    bars = ax.barh(y_pos, times, color=colors, edgecolor="none", height=0.65)

    if baseline_time is not None:
        ax.axvline(baseline_time, color="#c0392b", linestyle="--", linewidth=1.5,
                   label=f"Baseline ({baseline_time:.3f}s)")
        ax.legend(loc="lower right", framealpha=0.8)

    for bar, t in zip(bars, times):
        ax.text(bar.get_width() + max(times)*0.01, bar.get_y() + bar.get_height()/2,
                f" {t:.3f}s", va="center", ha="left", fontsize=9)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Total Workload Execution Time (seconds)", fontsize=11)
    ax.set_title("TPC-C Standard (9 Tables): Strategy Comparison", fontsize=13, fontweight="bold", pad=12)
    ax.grid(axis="x", linestyle=":", alpha=0.6)
    plt.tight_layout()
    plt.savefig(out_dir / "total_comparison.png", dpi=150)
    plt.close()


def plot_per_query_heatmap(per_query: Dict[str, Dict[str, float]], plots_dir: Optional[Path] = None) -> None:
    if not per_query:
        return
    out_dir = plots_dir or PLOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    all_queries = sorted({q for qmap in per_query.values() for q in qmap},
                         key=lambda x: int(re.search(r"\d+", x).group()) if re.search(r"\d+", x) else 0)
    all_strategies = sorted(per_query.keys())
    if not all_queries or not all_strategies:
        return

    matrix = np.zeros((len(all_strategies), len(all_queries)))
    for r, strat in enumerate(all_strategies):
        for c, q in enumerate(all_queries):
            matrix[r, c] = per_query[strat].get(q, 0.0)

    fig, ax = plt.subplots(figsize=(max(10, len(all_queries) * 0.7), max(6, len(all_strategies) * 0.45)))
    cax = ax.matshow(matrix, cmap="YlOrRd", aspect="auto")
    fig.colorbar(cax, label="Execution Time (seconds)")

    ax.set_xticks(np.arange(len(all_queries)))
    ax.set_yticks(np.arange(len(all_strategies)))
    ax.set_xticklabels(all_queries, fontsize=9)
    ax.set_yticklabels(all_strategies, fontsize=9)
    ax.set_title("TPC-C Standard: Per-Query Execution Time Heatmap", fontsize=13, fontweight="bold", pad=20)
    plt.tight_layout()
    plt.savefig(out_dir / "per_query_heatmap.png", dpi=150)
    plt.close()


def plot_k_curve(totals: Dict[str, float], plots_dir: Optional[Path] = None) -> None:
    out_dir = plots_dir or PLOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    k_points = {}
    pattern = re.compile(r"^cg_rule_based_config_sel_k(\d+)$")
    for lbl, t in totals.items():
        m = pattern.match(lbl)
        if m:
            k_points[int(m.group(1))] = t

    if len(k_points) < 2:
        return

    ks = sorted(k_points.keys())
    times = [k_points[k] for k in ks]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(ks, times, marker="o", linewidth=2, color="#2ecc71", label="cg_rule_based + config_sel")

    if "baseline" in totals:
        ax.axhline(totals["baseline"], color="#e74c3c", linestyle="--", label=f"Baseline ({totals['baseline']:.3f}s)")

    ax.set_xlabel("Number of Indexes (k)", fontsize=11)
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11)
    ax.set_title("TPC-C Standard: Index Count vs Workload Time (config_sel)", fontsize=13, fontweight="bold")
    ax.legend()
    ax.grid(True, linestyle=":", alpha=0.6)
    plt.tight_layout()
    plt.savefig(out_dir / "config_sel_k_curve.png", dpi=150)
    plt.close()


def plot_budget_curves(totals: Dict[str, float], plots_dir: Optional[Path] = None) -> None:
    out_dir = plots_dir or PLOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    drop_pts, extend_pts = {}, {}
    pat_drop = re.compile(r"^cg_rule_based_cs_drop_mb(\d+|inf)$")
    pat_ext  = re.compile(r"^cg_rule_based_cs_extend_mb(\d+|inf)$")

    for lbl, t in totals.items():
        m = pat_drop.match(lbl)
        if m:
            val = float("inf") if m.group(1) == "inf" else float(m.group(1))
            drop_pts[val] = t
        m2 = pat_ext.match(lbl)
        if m2:
            val2 = float("inf") if m2.group(1) == "inf" else float(m2.group(1))
            extend_pts[val2] = t

    if not drop_pts and not extend_pts:
        return

    fig, ax = plt.subplots(figsize=(9, 5))
    if drop_pts:
        pts = sorted(drop_pts.items(), key=lambda x: (x[0] == float("inf"), x[0]))
        x_labels = ["inf" if x[0] == float("inf") else str(int(x[0])) for x in pts]
        y_vals   = [x[1] for x in pts]
        ax.plot(range(len(pts)), y_vals, marker="s", linewidth=2, color="#3498db", label="cs_drop")
        ax.set_xticks(range(len(pts)))
        ax.set_xticklabels(x_labels)

    if extend_pts:
        pts = sorted(extend_pts.items(), key=lambda x: (x[0] == float("inf"), x[0]))
        y_vals = [x[1] for x in pts]
        ax.plot(range(len(pts)), y_vals, marker="^", linewidth=2, color="#9b59b6", label="cs_extend")

    if "baseline" in totals:
        ax.axhline(totals["baseline"], color="#e74c3c", linestyle="--", label=f"Baseline ({totals['baseline']:.3f}s)")

    ax.set_xlabel("Storage Budget (MB)", fontsize=11)
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11)
    ax.set_title("TPC-C Standard: Storage Budget vs Workload Time", fontsize=13, fontweight="bold")
    ax.legend()
    ax.grid(True, linestyle=":", alpha=0.6)
    plt.tight_layout()
    plt.savefig(out_dir / "budget_curves.png", dpi=150)
    plt.close()


def generate_all_plots(results_dir: Optional[Path] = None, plots_dir: Optional[Path] = None) -> None:
    target_results_dir = results_dir or RESULTS_DIR
    target_plots_dir   = plots_dir or (target_results_dir / "plots" if target_results_dir.name.startswith("run_") else RESULTS_DIR / "plots")
    target_plots_dir.mkdir(parents=True, exist_ok=True)

    totals    = _load_total(target_results_dir)
    per_query = _load_per_query(target_results_dir)

    plot_total_comparison(totals, plots_dir=target_plots_dir)
    plot_per_query_heatmap(per_query, plots_dir=target_plots_dir)
    plot_k_curve(totals, plots_dir=target_plots_dir)
    plot_budget_curves(totals, plots_dir=target_plots_dir)
    print(f"\n[plot] All charts saved to: {target_plots_dir}")
