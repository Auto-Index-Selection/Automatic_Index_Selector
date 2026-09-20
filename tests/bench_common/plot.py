"""
tests/bench_common/plot.py
--------------------------
Schema-agnostic visualisation for multi-combination experiment results.

Shared by every benchmark suite, so it holds no suite-specific paths: the
caller supplies the run directory, and `suite` supplies the chart-title prefix.
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



SUMMARY_ROWS = {"TOTAL", "TOTAL_MEDIAN", "TOTAL_MIN", "TOTAL_P95",
                "STORAGE_MB", "DML_LATENCY_MS", "FAILED_QUERIES"}


def load_all_strategy_results(csv_dir: Path) -> Dict[str, Dict]:
    """
    Loads all strategy CSVs from csv_dir.

    Returns a dict with keys:
        totals      : {label: total_time_seconds}
        per_query   : {label: {query: time_seconds}}
        dml         : {label: dml_ms}
        storages    : {label: storage_mb}
        spread      : {label: {"min": s, "median": s, "p95": s}}
        failures    : {label: failed_query_count}

    Strategies with failed queries are dropped from `totals`: their total is a
    sum over an incomplete query set and is not comparable to the baseline.
    """
    totals: Dict[str, float] = {}
    per_query: Dict[str, Dict[str, float]] = {}
    dml_latencies: Dict[str, float] = {}
    storages: Dict[str, float] = {}
    spread: Dict[str, Dict[str, float]] = {}
    failures: Dict[str, int] = {}

    # Summary rows must be recognised explicitly, otherwise they are mistaken
    # for query names and pollute the per-query heatmap.
    for csv_file in sorted(csv_dir.glob("*.csv")):
        label = csv_file.stem
        per_query[label] = {}
        spread[label] = {}
        with open(csv_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                q = row["query"]
                try:
                    sec = float(row["avg_time_seconds"])
                except (TypeError, ValueError):
                    sec = float("nan")
                if q == "TOTAL":
                    totals[label] = sec
                elif q == "TOTAL_MEDIAN":
                    spread[label]["median"] = sec
                elif q == "TOTAL_MIN":
                    spread[label]["min"] = sec
                elif q == "TOTAL_P95":
                    spread[label]["p95"] = sec
                elif q == "FAILED_QUERIES":
                    failures[label] = int(float(row.get("failures") or 0))
                elif q == "DML_LATENCY_MS":
                    dml_latencies[label] = float(row["avg_time_ms"])
                elif q == "STORAGE_MB":
                    storages[label] = float(row["avg_time_ms"])
                elif q not in SUMMARY_ROWS:
                    per_query[label][q] = sec

    invalid = sorted(lbl for lbl, n in failures.items() if n)
    invalid += sorted(lbl for lbl, t in totals.items() if np.isnan(t) and lbl not in invalid)
    if invalid:
        print(f"[Plotting] ⚠ Excluding {len(invalid)} strategy/strategies with failed queries: {invalid}")
    for lbl in invalid:
        totals.pop(lbl, None)

    return {
        "totals": totals,
        "per_query": per_query,
        "dml": dml_latencies,
        "storages": storages,
        "spread": spread,
        "failures": failures,
    }


def plot_total_comparison(totals: Dict[str, float], plots_dir: Path, suite: str = "Benchmark") -> Optional[Path]:
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
        ax.axvline(baseline_time, color="#c0392b", linestyle="--", linewidth=1.8, label=f"Baseline — existing PK indexes only ({baseline_time:.3f}s)")
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
    ax.set_title(f"{suite}: Strategy Comparison — Total Workload Execution Time", fontsize=13, fontweight="bold", pad=15)
    ax.grid(axis="x", linestyle=":", alpha=0.6)

    out_path = plots_dir / "total_comparison.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_per_query_heatmap(per_query: Dict[str, Dict[str, float]], plots_dir: Path, suite: str = "Benchmark") -> Optional[Path]:
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

    # Missing/failed cells must be masked, not filled with 0.0 -- zero renders
    # as the best possible time and makes a broken query look like the fastest.
    matrix = np.full((len(all_strategies), len(all_queries)), np.nan)
    for r, strat in enumerate(all_strategies):
        for c, q in enumerate(all_queries):
            matrix[r, c] = per_query[strat].get(q, np.nan)
    matrix = np.ma.masked_invalid(matrix)

    cmap = plt.get_cmap("YlGnBu_r").copy()
    cmap.set_bad(color="#bdbdbd")  # grey = no valid measurement

    fig, ax = plt.subplots(figsize=(max(10, len(all_queries) * 0.8), max(6, len(all_strategies) * 0.45)))
    cax = ax.matshow(matrix, cmap=cmap, aspect="auto")
    cbar = fig.colorbar(cax, label="Execution Time (seconds)")
    cbar.ax.tick_params(labelsize=9)

    ax.set_xticks(np.arange(len(all_queries)))
    ax.set_yticks(np.arange(len(all_strategies)))
    ax.set_xticklabels(all_queries, fontsize=9, fontweight="bold")
    ax.set_yticklabels(all_strategies, fontsize=9, fontweight="bold")
    ax.set_title(f"{suite}: Per-Query Execution Time Heatmap (seconds)", fontsize=12, fontweight="bold", pad=20)

    out_path = plots_dir / "heatmap_per_query.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _error_bars(labels: List[str], times: List[float], spread: Dict[str, Dict[str, float]]):
    """Asymmetric [min, p95] error bars, so noise-dominated results read as flat."""
    if not spread:
        return None
    lower, upper = [], []
    for lbl, t in zip(labels, times):
        s = spread.get(lbl, {})
        lo, hi = s.get("min", float("nan")), s.get("p95", float("nan"))
        lower.append(max(0.0, t - lo) if not np.isnan(lo) else 0.0)
        upper.append(max(0.0, hi - t) if not np.isnan(hi) else 0.0)
    if not any(lower) and not any(upper):
        return None
    return [lower, upper]


def plot_k_scaling(totals: Dict[str, float], plots_dir: Path,
                   spread: Optional[Dict[str, Dict[str, float]]] = None,
                   suite: str = "Benchmark") -> Optional[Path]:
    """Line plot showing speedup as index count k scales (config_sel)."""
    k_data: List[Tuple[int, float, str]] = []
    for lbl, t in totals.items():
        m = re.search(r"config_sel_k(\d+)", lbl)
        if m:
            k_data.append((int(m.group(1)), t, lbl))

    if not k_data:
        return None

    k_data.sort(key=lambda x: x[0])
    ks = [x[0] for x in k_data]
    times = [x[1] for x in k_data]
    labels = [x[2] for x in k_data]
    baseline_time = totals.get("baseline")

    fig, ax = plt.subplots(figsize=(8, 5))
    yerr = _error_bars(labels, times, spread or {})
    ax.errorbar(ks, times, yerr=yerr, marker="o", linewidth=2.2, color="#2ecc71",
                capsize=4, elinewidth=1.2, label="cg_rule_based + config_sel")

    if baseline_time is not None:
        ax.axhline(baseline_time, color="#e74c3c", linestyle="--", linewidth=1.5, label=f"Baseline — existing PK indexes only ({baseline_time:.3f}s)")

    for k, t in zip(ks, times):
        ax.annotate(f"{t:.3f}s", (k, t), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=9, fontweight="bold")

    ax.set_xlabel("Max Indexes Allowed (k)", fontsize=11, fontweight="bold")
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11, fontweight="bold")
    ax.set_title(f"{suite}: K-Parameter Scaling (GreedyMK)", fontsize=12, fontweight="bold", pad=12)
    ax.set_xticks(ks)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(frameon=True)

    out_path = plots_dir / "k_scaling.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_budget_scaling(totals: Dict[str, float], plots_dir: Path,
                        spread: Optional[Dict[str, Dict[str, float]]] = None,
                        suite: str = "Benchmark") -> Optional[Path]:
    """Line plot showing speedup as storage budget scales for Drop vs Extend."""
    series: Dict[str, Dict[float, Tuple[float, str]]] = {"drop": {}, "extend": {}}

    for lbl, t in totals.items():
        for key, pattern in (("drop", r"cs_drop_mb(\d+|inf)"), ("extend", r"cs_extend_mb(\d+|inf)")):
            m = re.search(pattern, lbl)
            if m:
                mb = float("inf") if m.group(1) == "inf" else float(m.group(1))
                series[key][mb] = (t, lbl)

    if not series["drop"] and not series["extend"]:
        return None

    # One shared, sorted budget axis for both series. Giving each series its own
    # np.arange while labelling the ticks from only one of them mislabels which
    # budget a point belongs to whenever the two differ in length.
    budgets = sorted(set(series["drop"]) | set(series["extend"]))
    x_idx = np.arange(len(budgets))
    tick_labels = [("∞" if b == float("inf") else str(int(b))) for b in budgets]

    fig, ax = plt.subplots(figsize=(9, 5))
    baseline_time = totals.get("baseline")

    if baseline_time is not None:
        ax.axhline(baseline_time, color="#e74c3c", linestyle="--", linewidth=1.5, label=f"Baseline — existing PK indexes only ({baseline_time:.3f}s)")

    style = {
        "drop": ("s", "#3498db", "cs_drop (Drop Heuristic)"),
        "extend": ("^", "#9b59b6", "cs_extend (Extend Algorithm)"),
    }
    for key, (marker, color, legend) in style.items():
        points = series[key]
        if not points:
            continue
        xs = [i for i, b in enumerate(budgets) if b in points]
        ys = [points[budgets[i]][0] for i in xs]
        labels = [points[budgets[i]][1] for i in xs]
        yerr = _error_bars(labels, ys, spread or {})
        ax.errorbar(xs, ys, yerr=yerr, marker=marker, linewidth=2.0, color=color,
                    capsize=4, elinewidth=1.2, label=legend)

    ax.set_xticks(x_idx)
    ax.set_xticklabels(tick_labels)
    ax.set_xlabel("Storage Budget (MB)", fontsize=11, fontweight="bold")
    ax.set_ylabel("Total Workload Execution Time (seconds)", fontsize=11, fontweight="bold")
    ax.set_title(f"{suite}: Storage Budget Scaling (Drop vs Extend)", fontsize=12, fontweight="bold", pad=12)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(frameon=True)

    out_path = plots_dir / "budget_scaling.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def find_target_run_dir(target: Path) -> Path:
    """
    Finds the run directory to plot, given either a specific run or a results root.

    Resolution order matters: `latest` and `run_*` are checked BEFORE `base/csv`.
    A results root can contain a stale top-level csv/ from an old run, and
    checking that first would let it shadow the newest run -- silently plotting
    old numbers. A specific run directory has no `latest` or nested `run_*`, so
    it falls through to the `base/csv` check and resolves to itself.
    """
    base = Path(target)

    latest = base / "latest"
    if latest.is_dir() and (latest / "csv").is_dir():
        return latest.resolve()

    run_dirs = sorted([d for d in base.glob("run_*") if d.is_dir()])
    if run_dirs:
        return run_dirs[-1]

    return base


def generate_all_plots(results_dir: Path, suite: str = "Benchmark") -> List[Path]:
    """
    Load all CSVs under `results_dir` and generate the comparison charts.

    `suite` prefixes the chart titles (e.g. "pgbench", "TPC-C") so the same
    plotting code serves every benchmark suite without mislabelling results.
    """
    run_dir = find_target_run_dir(results_dir)
    csv_dir = run_dir / "csv" if (run_dir / "csv").is_dir() else run_dir
    plots_dir = run_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Plotting] Loading CSV results from: {csv_dir}")
    print(f"[Plotting] Saving comparison charts to: {plots_dir}")

    results = load_all_strategy_results(csv_dir)
    totals, per_query, spread = results["totals"], results["per_query"], results["spread"]
    generated = []

    p1 = plot_total_comparison(totals, plots_dir, suite)
    if p1: generated.append(p1)

    p2 = plot_per_query_heatmap(per_query, plots_dir, suite)
    if p2: generated.append(p2)

    p3 = plot_k_scaling(totals, plots_dir, spread, suite)
    if p3: generated.append(p3)

    p4 = plot_budget_scaling(totals, plots_dir, spread, suite)
    if p4: generated.append(p4)

    print(f"[Plotting] Generated {len(generated)} comparison plots in {plots_dir}")
    return generated
