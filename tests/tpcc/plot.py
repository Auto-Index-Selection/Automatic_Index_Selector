"""
tests/tpcc/plot.py
------------------
Visualisation module for multi-combination TPC-C experiment results.
Generates publication-quality comparison charts saved into results/plots/.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # Headless backend
import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CSV_DIR = RESULTS_DIR / "csv"
PLOTS_DIR = RESULTS_DIR / "plots"


def load_all_strategy_results(csv_dir: Path) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]], Dict[str, float], Dict[str, float]]:
    """
    Loads all strategy CSVs from csv_dir.
    Returns:
        totals      : {label: total_time_seconds}
        per_query   : {label: {query: time_seconds}}
        dml_latencies: {label: dml_ms}
        storages    : {label: storage_mb}
    """
    totals: Dict[str, float] = {}
    per_query: Dict[str, Dict[str, float]] = {}
    dml_latencies: Dict[str, float] = {}
    storages: Dict[str, float] = {}

    for csv_file in csv_dir.glob("*.csv"):
        label = csv_file.stem
        per_query[label] = {}
        with open(csv_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                q = row["query"]
                sec = float(row["avg_time_seconds"])
                if q == "TOTAL":
                    totals[label] = sec
                elif q == "DML_LATENCY_MS":
                    dml_latencies[label] = float(row["avg_time_ms"])
                elif q == "STORAGE_MB":
                    storages[label] = float(row["avg_time_ms"])
                else:
                    per_query[label][q] = sec

    return totals, per_query, dml_latencies, storages


def plot_total_comparison(totals: Dict[str, float], plots_dir: Path) -> Optional[Path]:
    """Horizontal bar chart comparing all strategies against Baseline."""
    if not totals:
        return None

    baseline_time = totals.get("baseline")
    sorted_items = sorted(totals.items(), key=lambda x: x[1])
    labels = [k for k, _ in sorted_items]
    times = [v for _, v in sorted_items]

    colors = []
    for lbl in labels:
        if lbl == "baseline":
            colors.append("#e74c3c")
        elif "config_sel" in lbl or "greedy" in lbl:
            colors.append("#2ecc71")
        elif "cs_drop" in lbl:
            colors.append("#3498db")
        elif "cs_extend" in lbl:
            colors.append("#9b59b6")
        else:
            colors.append("#1abc9c")

    fig, ax = plt.subplots(figsize=(12, max(6, len(labels) * 0.45)))
    y_pos = np.arange(len(labels))
    bars = ax.barh(y_pos, times, color=colors, edgecolor="#2c3e50", height=0.65)

    if baseline_time is not None:
        ax.axvline(baseline_time, color="#c0392b", linestyle="--", linewidth=1.8, label=f"Baseline ({baseline_time:.3f}s)")
        ax.legend(loc="lower right", fontsize=10, framealpha=0.9)

    max_t = max(times) if times else 1.0
    for bar, t, lbl in zip(bars, times, labels):
        speedup_str = ""
        if baseline_time and lbl != "baseline":
            sp = ((baseline_time - t) / baseline_time) * 100.0
            speedup_str = f" ({sp:+.1f}%)"
        ax.text(bar.get_width() + max_t * 0.01, bar.get_y() + bar.get_height() / 2,
                f" {t:.3f}s{speedup_str}", va="center", ha="left", fontsize=9, fontweight="bold")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9, fontweight="bold")
    ax.set_xlabel("Total Workload Execution Time (seconds) — Lower is Better", fontsize=11, fontweight="bold")
    ax.set_title("TPC-C: Strategy Comparison — Total Workload Execution Time", fontsize=13, fontweight="bold", pad=15)
    ax.grid(axis="x", linestyle=":", alpha=0.6)

    out_path = plots_dir / "total_comparison.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_per_query_heatmap(per_query: Dict[str, Dict[str, float]], plots_dir: Path) -> Optional[Path]:
    """Heatmap matrix of per-query execution times across all strategies."""
    if not per_query:
        return None

    def _qnum(q: str) -> int:
        m = re.search(r"(\d+)", q)
        return int(m.group(1)) if m else 0

    all_queries = sorted({q for qmap in per_query.values() for q in qmap}, key=_qnum)
    all_strategies = sorted(per_query.keys())
    if not all_queries or not all_strategies:
        return None

    matrix = np.zeros((len(all_strategies), len(all_queries)))
    for r, strat in enumerate(all_strategies):
        for c, q in enumerate(all_queries):
            matrix[r, c] = per_query[strat].get(q, 0.0)

    fig, ax = plt.subplots(figsize=(max(10, len(all_queries) * 0.8), max(6, len(all_strategies) * 0.45)))
    cax = ax.matshow(matrix, cmap="YlGnBu_r", aspect="auto")
    cbar = fig.colorbar(cax, label="Execution Time (seconds)")
    cbar.ax.tick_params(labelsize=9)

    ax.set_xticks(np.arange(len(all_queries)))
    ax.set_yticks(np.arange(len(all_strategies)))
    ax.set_xticklabels(all_queries, fontsize=9, fontweight="bold")
    ax.set_yticklabels(all_strategies, fontsize=9, fontweight="bold")
    ax.set_title("TPC-C: Per-Query Execution Time Heatmap (seconds)", fontsize=12, fontweight="bold", pad=20)

    out_path = plots_dir / "heatmap_per_query.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_k_scaling(totals: Dict[str, float], plots_dir: Path) -> Optional[Path]:
    """Line plot showing speedup as index count k scales (config_sel)."""
    k_data: List[Tuple[int, float]] = []
    for lbl, t in totals.items():
        m = re.search(r"config_sel_k(\d+)", lbl)
        if m:
            k_data.append((int(m.group(1)), t))

    if not k_data:
        return None

    k_data.sort(key=lambda x: x[0])
    ks = [x[0] for x in k_data]
    times = [x[1] for x in k_data]
    baseline_time = totals.get("baseline")

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(ks, times, marker="o", linewidth=2.2, color="#2ecc71", label="cg_rule_based + config_sel")

    if baseline_time is not None:
        ax.axhline(baseline_time, color="#e74c3c", linestyle="--", linewidth=1.5, label=f"Baseline ({baseline_time:.3f}s)")

    for k, t in zip(ks, times):
        ax.annotate(f"{t:.3f}s", (k, t), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=9, fontweight="bold")

    ax.set_xlabel("Max Indexes Allowed (k)", fontsize=11, fontweight="bold")
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11, fontweight="bold")
    ax.set_title("TPC-C: K-Parameter Scaling (GreedyMK)", fontsize=12, fontweight="bold", pad=12)
    ax.set_xticks(ks)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(frameon=True)

    out_path = plots_dir / "k_scaling.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_budget_scaling(totals: Dict[str, float], plots_dir: Path) -> Optional[Path]:
    """Line plot showing speedup as storage budget scales for Drop vs Extend."""
    drop_data: List[Tuple[float, float]] = []
    extend_data: List[Tuple[float, float]] = []

    for lbl, t in totals.items():
        m_drop = re.search(r"cs_drop_mb(\d+|inf)", lbl)
        if m_drop:
            mb = float("inf") if m_drop.group(1) == "inf" else float(m_drop.group(1))
            drop_data.append((mb, t))
        m_ext = re.search(r"cs_extend_mb(\d+|inf)", lbl)
        if m_ext:
            mb = float("inf") if m_ext.group(1) == "inf" else float(m_ext.group(1))
            extend_data.append((mb, t))

    if not drop_data and not extend_data:
        return None

    fig, ax = plt.subplots(figsize=(9, 5))
    baseline_time = totals.get("baseline")

    if baseline_time is not None:
        ax.axhline(baseline_time, color="#e74c3c", linestyle="--", linewidth=1.5, label=f"Baseline ({baseline_time:.3f}s)")

    if drop_data:
        drop_data.sort(key=lambda x: x[0])
        # Replace inf with finite label for plotting
        labels = [str(int(x[0])) if x[0] != float("inf") else "∞" for x in drop_data]
        x_idx = np.arange(len(drop_data))
        times = [x[1] for x in drop_data]
        ax.plot(x_idx, times, marker="s", linewidth=2.0, color="#3498db", label="cs_drop (Drop Heuristic)")
        ax.set_xticks(x_idx)
        ax.set_xticklabels(labels)

    if extend_data:
        extend_data.sort(key=lambda x: x[0])
        x_idx = np.arange(len(extend_data))
        times = [x[1] for x in extend_data]
        ax.plot(x_idx, times, marker="^", linewidth=2.0, color="#9b59b6", label="cs_extend (Extend Algorithm)")

    ax.set_xlabel("Storage Budget (MB)", fontsize=11, fontweight="bold")
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11, fontweight="bold")
    ax.set_title("TPC-C: Storage Budget Scaling (Drop vs Extend)", fontsize=12, fontweight="bold", pad=12)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(frameon=True)

    out_path = plots_dir / "budget_scaling.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def find_target_run_dir(target: Optional[Path] = None) -> Path:
    """Finds the appropriate run directory (latest run_* or specific run folder)."""
    base = target or RESULTS_DIR
    if (base / "csv").is_dir() and any((base / "csv").glob("*.csv")):
        return base
    if (base / "latest").is_dir() and (base / "latest" / "csv").is_dir():
        return (base / "latest").resolve()
    run_dirs = sorted([d for d in base.glob("run_*") if d.is_dir()])
    if run_dirs:
        return run_dirs[-1]
    return base


def generate_all_plots(results_dir: Optional[Path] = None) -> List[Path]:
    """One-stop function to load all CSVs and generate all comparison plots."""
    run_dir = find_target_run_dir(results_dir)
    csv_dir = run_dir / "csv" if (run_dir / "csv").is_dir() else run_dir
    plots_dir = run_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Plotting] Loading CSV results from: {csv_dir}")
    print(f"[Plotting] Saving comparison charts to: {plots_dir}")

    totals, per_query, dml, storage = load_all_strategy_results(csv_dir)
    generated = []

    p1 = plot_total_comparison(totals, plots_dir)
    if p1: generated.append(p1)

    p2 = plot_per_query_heatmap(per_query, plots_dir)
    if p2: generated.append(p2)

    p3 = plot_k_scaling(totals, plots_dir)
    if p3: generated.append(p3)

    p4 = plot_budget_scaling(totals, plots_dir)
    if p4: generated.append(p4)

    print(f"[Plotting] Generated {len(generated)} comparison plots in {plots_dir}")
    return generated
