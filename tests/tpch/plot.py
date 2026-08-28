"""
tests/tpch/plot.py
Reads all result CSVs from tests/tpch/results/ and draws four comparison charts:
  1. total_comparison.png   — bar chart of total workload time per strategy
  2. per_query_heatmap.png  — heatmap of per-query avg time across strategies
  3. config_sel_k_curve.png — line chart: total time vs k for config_sel
  4. budget_curves.png      — line chart: total time vs storage budget (cs_drop vs cs_extend)
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # headless backend — no display needed
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

RESULTS_DIR = Path(__file__).parent / "results"
PLOTS_DIR = RESULTS_DIR / "plots"


# ---------------------------------------------------------------------------
# CSV loaders (supports results/run_TIMESTAMP/csv/ or results/)
# ---------------------------------------------------------------------------

def find_csv_dir(results_dir: Path = RESULTS_DIR) -> Path:
    """
    Locate the folder containing CSVs.
    Supports:
      1. run_dir / "csv"
      2. run_dir
      3. latest run_* directory under results_dir
    """
    if (results_dir / "csv").is_dir():
        return results_dir / "csv"

    # Check if results_dir directly has CSVs
    if list(results_dir.glob("*_avg.csv")):
        return results_dir

    # Check for latest run_* folder
    run_folders = sorted([d for d in results_dir.glob("run_*") if d.is_dir()])
    if run_folders:
        latest = run_folders[-1]
        if (latest / "csv").is_dir():
            return latest / "csv"
        return latest

    return results_dir


def _load_total(results_dir: Path = RESULTS_DIR) -> Dict[str, float]:
    """Return {strategy_label: avg_total_seconds} from all *_avg.csv files."""
    csv_dir = find_csv_dir(results_dir)
    totals: Dict[str, float] = {}
    for f in sorted(csv_dir.glob("*_avg.csv")):
        with open(f) as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                totals[row["strategy"]] = float(row["avg_total_time_seconds"])
    return totals


def _load_per_query(results_dir: Path = RESULTS_DIR) -> Dict[str, Dict[str, float]]:
    """Return {strategy_label: {query_label: avg_seconds}} from *_qt.csv files.
    Timed-out queries (marked TIMED_OUT in the CSV) are skipped.
    """
    csv_dir = find_csv_dir(results_dir)
    data: Dict[str, Dict[str, float]] = {}
    for f in sorted(csv_dir.glob("*_qt.csv")):
        strategy = f.stem.replace("_qt", "")
        per_q: Dict[str, float] = {}
        with open(f) as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                val = row["avg_time_seconds"]
                if val == "TIMED_OUT" or row.get("status", "OK") == "TIMED_OUT":
                    continue  # exclude timed-out queries from plots
                try:
                    per_q[row["query"]] = float(val)
                except ValueError:
                    continue
        data[strategy] = per_q
    return data


def _sort_queries(labels) -> List[str]:
    def _key(lbl):
        digits = "".join(c for c in str(lbl) if c.isdigit())
        return (int(digits) if digits else 0, str(lbl))
    return sorted(labels, key=_key)


def _k_from_label(label: str) -> Optional[int]:
    """Extract k value from label like 'config_sel_k5' -> 5."""
    m = re.search(r"_k(\d+)", label)
    return int(m.group(1)) if m else None


def _budget_from_label(label: str) -> Optional[int]:
    """Extract budget value from label like 'cs_drop_mb100' -> 100."""
    m = re.search(r"_mb(\d+|inf)", label)
    if m:
        v = m.group(1)
        return float("inf") if v == "inf" else int(v)
    return None


# ---------------------------------------------------------------------------
# Chart 1: Total workload time bar chart
# ---------------------------------------------------------------------------

def plot_total_comparison(
    totals: Dict[str, float],
    plots_dir: Path = PLOTS_DIR,
    workload_name: str = "TPC-H",
) -> Path:
    plots_dir.mkdir(parents=True, exist_ok=True)
    if not totals:
        print("[plot] No total timing data found, skipping total_comparison.png")
        return plots_dir / "total_comparison.png"

    labels = list(totals.keys())
    values = [totals[l] for l in labels]
    baseline = totals.get("baseline", None)

    fig, ax = plt.subplots(figsize=(max(10, len(labels) * 0.9), 6))
    colors = ["#d9534f" if l == "baseline" else "#5b9bd5" for l in labels]
    bars = ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.7)

    if baseline:
        ax.axhline(baseline, color="#d9534f", linestyle="--", linewidth=1.2,
                   label=f"Baseline ({baseline:.2f}s)")

    ax.set_xlabel("Strategy", fontsize=11)
    ax.set_ylabel("Avg Total Time (seconds)", fontsize=11)
    ax.set_title(f"{workload_name} Workload — Total Execution Time by Strategy", fontsize=13, fontweight="bold")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)

    for bar, val in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(values) * 0.01,
            f"{val:.2f}s",
            ha="center", va="bottom", fontsize=7,
        )

    if baseline:
        ax.legend(fontsize=9)

    ax.grid(axis="y", linestyle="--", alpha=0.5)
    fig.tight_layout()
    out = plots_dir / "total_comparison.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[plot] Saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Chart 2: Per-query heatmap
# ---------------------------------------------------------------------------

def plot_per_query_heatmap(
    per_query: Dict[str, Dict[str, float]],
    plots_dir: Path = PLOTS_DIR,
    workload_name: str = "TPC-H",
) -> Path:
    plots_dir.mkdir(parents=True, exist_ok=True)
    if not per_query:
        print("[plot] No per-query data, skipping heatmap.")
        return plots_dir / "per_query_heatmap.png"

    # Build sorted axes
    all_queries = _sort_queries({q for pq in per_query.values() for q in pq})
    strategies = list(per_query.keys())

    matrix = np.full((len(strategies), len(all_queries)), np.nan)
    for r, strat in enumerate(strategies):
        for c, q in enumerate(all_queries):
            v = per_query[strat].get(q, np.nan)
            matrix[r, c] = v

    fig, ax = plt.subplots(figsize=(max(12, len(all_queries) * 0.55),
                                    max(5, len(strategies) * 0.45)))
    im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn_r", interpolation="nearest")

    ax.set_xticks(range(len(all_queries)))
    ax.set_xticklabels(all_queries, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(strategies)))
    ax.set_yticklabels(strategies, fontsize=8)
    ax.set_xlabel(f"{workload_name} Query", fontsize=10)
    ax.set_ylabel("Strategy", fontsize=10)
    ax.set_title("Per-Query Avg Execution Time (seconds) — lower is better",
                 fontsize=12, fontweight="bold")

    cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
    cbar.set_label("Avg Time (s)", fontsize=9)

    fig.tight_layout()
    out = plots_dir / "per_query_heatmap.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[plot] Saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Chart 3: config_sel k-curve
# ---------------------------------------------------------------------------

def plot_k_curve(
    totals: Dict[str, float],
    plots_dir: Path = PLOTS_DIR,
    workload_name: str = "TPC-H",
) -> Path:
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Collect points from labels like "cg_X_config_sel_k<N>"
    points: Dict[str, List] = {}  # cg_name -> [(k, total)]
    for label, total in totals.items():
        k = _k_from_label(label)
        if k is None:
            continue
        # group by CG name prefix
        cg = re.sub(r"_config_sel_k\d+$", "", label)
        points.setdefault(cg, []).append((k, total))

    if not points:
        print("[plot] No config_sel k-curve data found, skipping.")
        return plots_dir / "config_sel_k_curve.png"

    fig, ax = plt.subplots(figsize=(8, 5))
    for cg, pts in sorted(points.items()):
        pts.sort()
        ks, tots = zip(*pts)
        ax.plot(ks, tots, marker="o", label=cg)
        for k, t in zip(ks, tots):
            ax.annotate(f"{t:.2f}s", (k, t), textcoords="offset points",
                        xytext=(4, 4), fontsize=7)

    if "baseline" in totals:
        ax.axhline(totals["baseline"], color="red", linestyle="--", linewidth=1.2,
                   label=f"Baseline ({totals['baseline']:.2f}s)")

    ax.set_xlabel("k (max indexes)", fontsize=11)
    ax.set_ylabel("Avg Total Time (seconds)", fontsize=11)
    ax.set_title(f"config_sel — Effect of k on {workload_name} Workload Time", fontsize=12, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(linestyle="--", alpha=0.5)
    fig.tight_layout()
    out = plots_dir / "config_sel_k_curve.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[plot] Saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Chart 4: Storage budget curves (cs_drop vs cs_extend)
# ---------------------------------------------------------------------------

def plot_budget_curves(
    totals: Dict[str, float],
    plots_dir: Path = PLOTS_DIR,
    workload_name: str = "TPC-H",
) -> Path:
    plots_dir.mkdir(parents=True, exist_ok=True)

    series: Dict[str, List] = {}  # algo_name -> [(budget_mb, total)]
    for label, total in totals.items():
        budget = _budget_from_label(label)
        if budget is None:
            continue
        for algo in ("cs_drop", "cs_extend"):
            if algo in label:
                series.setdefault(algo, []).append((budget, total))

    if not series:
        print("[plot] No budget-curve data found, skipping.")
        return plots_dir / "budget_curves.png"

    fig, ax = plt.subplots(figsize=(8, 5))
    for algo, pts in sorted(series.items()):
        pts_finite = [(b, t) for b, t in pts if b != float("inf")]
        pts_finite.sort()
        if pts_finite:
            bs, ts = zip(*pts_finite)
            ax.plot(bs, ts, marker="o", label=algo)
            for b, t in zip(bs, ts):
                ax.annotate(f"{t:.2f}s", (b, t), textcoords="offset points",
                            xytext=(4, 4), fontsize=7)
        # Add unconstrained point at the right edge
        inf_pts = [(b, t) for b, t in pts if b == float("inf")]
        if inf_pts and pts_finite:
            x_inf = max(b for b, _ in pts_finite) * 1.15
            _, t_inf = inf_pts[0]
            ax.scatter([x_inf], [t_inf], marker="*", s=120, zorder=5)
            ax.annotate(f"∞ {t_inf:.2f}s", (x_inf, t_inf),
                        textcoords="offset points", xytext=(4, 4), fontsize=7)

    if "baseline" in totals:
        ax.axhline(totals["baseline"], color="red", linestyle="--", linewidth=1.2,
                   label=f"Baseline ({totals['baseline']:.2f}s)")

    ax.set_xlabel("Storage Budget (MB)", fontsize=11)
    ax.set_ylabel("Avg Total Time (seconds)", fontsize=11)
    ax.set_title(f"cs_drop vs cs_extend — Effect of Storage Budget on {workload_name} Time",
                 fontsize=12, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(linestyle="--", alpha=0.5)
    fig.tight_layout()
    out = plots_dir / "budget_curves.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[plot] Saved: {out}")
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_all_plots(
    results_dir: Path = RESULTS_DIR,
    plots_dir: Optional[Path] = None,
    workload_name: str = "TPC-H",
) -> None:
    csv_dir = find_csv_dir(results_dir)
    target_plots_dir = plots_dir or (results_dir / "plots" if results_dir.name.startswith("run_") else PLOTS_DIR)
    target_plots_dir.mkdir(parents=True, exist_ok=True)

    totals    = _load_total(results_dir)
    per_query = _load_per_query(results_dir)

    plot_total_comparison(totals,     plots_dir=target_plots_dir, workload_name=workload_name)
    plot_per_query_heatmap(per_query, plots_dir=target_plots_dir, workload_name=workload_name)
    plot_k_curve(totals,              plots_dir=target_plots_dir, workload_name=workload_name)
    plot_budget_curves(totals,        plots_dir=target_plots_dir, workload_name=workload_name)

    print(f"\n[plot] All charts saved to: {target_plots_dir}")
