"""
plot_results.py
===============
Generates four sets of plots from the results/ directory CSV files:

  1. Query time bar chart — avg time per query, bars grouped by k / storage_mb.
     One chart per (workload, cg, cs).

  2. Total workload time vs k / storage_mb — line chart.
     One chart per (workload, cg, cs).

  3. Actual index size vs k / storage_mb — line chart.
     One chart per (workload, cg, cs).
     (Only plotted when index_size_mb column exists in the _avg.csv.)

  4. Strategy comparison — total workload time vs k / storage_mb,
     one line per (cg, cs) combination, one chart per workload.

Usage
-----
  python plot_results.py                     # plots for all results
  python plot_results.py --workload tpch     # filter by workload name substring
  python plot_results.py --show              # display interactively instead of saving
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")           # non-interactive backend; overridden by --show
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

REPO_ROOT   = Path(__file__).parent.parent   # repo root (one level up from tests/)
RESULTS_DIR = REPO_ROOT / "results"
PLOTS_DIR   = REPO_ROOT / "plots"

# ── colour palette ─────────────────────────────────────────────────────────────
PALETTE = [
    "#4C72B0", "#DD8452", "#55A868", "#C44E52",
    "#8172B2", "#937860", "#DA8BC3", "#8C8C8C",
    "#CCB974", "#64B5CD",
]

# Sentinel value written by test_strategy when a config exceeds the timeout.
# Must match test_strategy.TIMEOUT_SENTINEL.
TIMEOUT_SENTINEL = -1

# ── filename patterns ──────────────────────────────────────────────────────────
# Per-k / per-storage query-timing files
QT_GREEDY_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_greedy)_(?P<k>\d+)_(?P<m>\d+)_qt\.csv$"
)
QT_SIZE_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_(?P<storage>\d+)_qt\.csv$"
)
# Consolidated avg files
AVG_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_avg\.csv$"
)
# Strategy timing files
STRATEGY_TIME_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_strategy_time\.csv$"
)
# HypoPG cost files
HYPOPG_COST_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_hypopg_cost\.csv$"
)
# Baseline files
BASELINE_RE = re.compile(
    r"^(?P<w>.+?)_baseline\.csv$"
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _label(cg: str, cs: str) -> str:
    return f"{cg} / {cs}"


def _key_label(key_col: str) -> str:
    return "k (index count)" if key_col == "k" else "Storage budget (MB)"


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _strip_cr(s: str) -> str:
    """Strip Windows-style carriage returns that may appear in values."""
    return s.strip().rstrip("\r")


def _natural_sort_key(s: str):
    parts = re.split(r"(\d+)", str(s))
    return [int(p) if p.isdigit() else p for p in parts]


def safe_float(v):
    try:
        return float(_strip_cr(str(v)))
    except (ValueError, TypeError):
        return None


# ── data collection ────────────────────────────────────────────────────────────

def collect_qt_files(results_dir: Path):
    """
    Returns {(w, cg, cs) -> [(sort_key, key_value_str, {query: seconds})]}.
    """
    groups = defaultdict(list)
    for p in sorted(results_dir.glob("*_qt.csv")):
        m = QT_GREEDY_RE.match(p.name)
        if m:
            gk = (m["w"], m["cg"], m["cs"])
            key_col = "k"
            key_val = int(m["k"])
            key_str = str(key_val)
        else:
            m = QT_SIZE_RE.match(p.name)
            if not m:
                continue
            gk = (m["w"], m["cg"], m["cs"])
            key_col = "storage_mb"
            key_val = int(m["storage"])
            key_str = str(key_val)

        rows = _read_csv(p)
        timings = {
            _strip_cr(r["query"]): safe_float(r["avg_time_seconds"])
            for r in rows if "query" in r and "avg_time_seconds" in r
        }
        groups[gk].append((key_val, key_str, key_col, timings))

    # sort each group by numeric key
    for gk in groups:
        groups[gk].sort(key=lambda e: e[0])

    return groups


def collect_avg_files(results_dir: Path):
    """
    Returns {(w, cg, cs) -> {
        "key_col": str,
        "keys":    [str],
        "total":   [float],
        "size_mb": [float | None],
    }}.
    """
    result = {}
    for p in sorted(results_dir.glob("*_avg.csv")):
        m = AVG_RE.match(p.name)
        if not m:
            continue
        gk = (m["w"], m["cg"], m["cs"])
        rows = _read_csv(p)
        if not rows:
            continue

        # detect key column
        first = rows[0]
        key_col = "k" if "k" in first else "storage_mb" if "storage_mb" in first else None
        if key_col is None:
            continue

        keys, totals, sizes = [], [], []
        for r in sorted(rows, key=lambda x: safe_float(x.get(key_col, 0)) or 0):
            keys.append(_strip_cr(r[key_col]))
            totals.append(safe_float(r.get("avg_total_time_seconds")))
            sizes.append(safe_float(r.get("index_size_mb")))

        result[gk] = {
            "key_col": key_col,
            "keys":    keys,
            "total":   totals,
            "size_mb": sizes,
        }

    return result


def collect_strategy_time_files(results_dir: Path):
    """
    Returns {(w, cg, cs) -> {
        "cg_time": float | None,
        "cs_time": float,
        "total_time": float,
    }}.
    """
    result = {}
    for p in sorted(results_dir.glob("*_strategy_time.csv")):
        m = STRATEGY_TIME_RE.match(p.name)
        if not m:
            continue
        gk = (m["w"], m["cg"], m["cs"])
        rows = _read_csv(p)
        if not rows:
            continue
        r = rows[0]
        result[gk] = {
            "cg_time": safe_float(r.get("cg_time_seconds")),
            "cs_time": safe_float(r.get("cs_time_seconds")) or 0.0,
            "total_time": safe_float(r.get("total_time_seconds")) or 0.0,
        }
    return result


def collect_hypopg_cost_files(results_dir: Path):
    """
    Returns {(w, cg, cs) -> {
        "key_col": str,
        "keys":    [str],
        "costs":   [float],
    }}.
    """
    result = {}
    for p in sorted(results_dir.glob("*_hypopg_cost.csv")):
        m = HYPOPG_COST_RE.match(p.name)
        if not m:
            continue
        gk = (m["w"], m["cg"], m["cs"])
        rows = _read_csv(p)
        if not rows:
            continue

        # Detect key column
        first = rows[0]
        key_col = "k" if "k" in first else "storage_mb" if "storage_mb" in first else None
        if key_col is None:
            continue

        keys, costs = [], []
        for r in sorted(rows, key=lambda x: safe_float(x.get(key_col, 0)) or 0):
            keys.append(_strip_cr(r[key_col]))
            costs.append(safe_float(r.get("hypopg_cost")) or 0.0)

        result[gk] = {
            "key_col": key_col,
            "keys":    keys,
            "costs":   costs,
        }

    return result


def collect_baseline_files(results_dir: Path):
    """
    Returns {w -> {"timings": {query: seconds}, "total": total_seconds}}
    """
    result = {}
    for p in sorted(results_dir.glob("*_baseline.csv")):
        m = BASELINE_RE.match(p.name)
        if not m:
            continue
        w = m["w"]
        rows = _read_csv(p)
        timings = {}
        total = 0.0
        for r in rows:
            if "query" in r and "avg_time_seconds" in r:
                q = _strip_cr(r["query"])
                t = safe_float(r["avg_time_seconds"])
                timings[q] = t
                if t is not None and t >= 0:
                    total += t
        result[w] = {
            "timings": timings,
            "total": total,
        }
    return result


# ── plot 1: query-time bar chart ───────────────────────────────────────────────

def plot_query_bars(qt_groups: dict, baseline_data: dict, out_dir: Path, show: bool, workload_filter: str):
    for (w, cg, cs), entries in sorted(qt_groups.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue
        if not entries:
            continue

        key_col = entries[0][2]
        key_label = _key_label(key_col)

        b_data = baseline_data.get(w)
        plot_entries = list(entries)
        if b_data:
            plot_entries.insert(0, (0, "Baseline", "Baseline", b_data["timings"]))

        # union of all query labels, sorted naturally
        all_queries = sorted(
            {q for _, _, _, t in plot_entries for q in t},
            key=_natural_sort_key,
        )
        n_queries = len(all_queries)
        n_bars    = len(plot_entries)

        fig, ax = plt.subplots(figsize=(max(14, n_queries * 0.6 * n_bars * 0.25), 6))

        bar_w = 0.8 / n_bars
        x = np.arange(n_queries)

        for i, (_, key_str, _, timings) in enumerate(plot_entries):
            vals = [timings.get(q) or 0.0 for q in all_queries]
            if key_str == "Baseline":
                color = "#333333"
                label = "Baseline"
            else:
                # shift palette index if we added Baseline so config colors stay consistent
                color_idx = i - 1 if b_data else i
                color = PALETTE[color_idx % len(PALETTE)]
                label = f"{key_col}={key_str}"
                
            bars = ax.bar(
                x + i * bar_w - (n_bars - 1) * bar_w / 2,
                vals, bar_w,
                label=label,
                color=color, alpha=0.85, edgecolor="white", linewidth=0.5,
            )

        ax.set_xticks(x)
        ax.set_xticklabels(all_queries, rotation=45, ha="right", fontsize=8)
        ax.set_xlabel("Query", fontsize=11)
        ax.set_ylabel("Avg time (s)", fontsize=11)
        ax.set_title(
            f"Query times — {w}  |  {_label(cg, cs)}",
            fontsize=12, fontweight="bold",
        )
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.legend(title=key_label, bbox_to_anchor=(1.01, 1), loc="upper left",
                  fontsize=8, title_fontsize=9)

        _finalise(fig, out_dir / f"{w}_{cg}_{cs}_query_bars.png", show)


# ── plot 2: total time vs k / storage ─────────────────────────────────────────

def plot_total_time(avg_data: dict, baseline_data: dict, out_dir: Path, show: bool, workload_filter: str):
    for (w, cg, cs), d in sorted(avg_data.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        keys    = d["keys"]
        totals  = d["total"]
        key_col = d["key_col"]
        xs      = np.arange(len(keys))

        # Detect timed-out configs (sentinel = -1).
        is_timeout = [t is not None and t < 0 for t in totals]
        # Height for timed-out bars: taller than the tallest real bar.
        real_vals = [t for t in totals if t is not None and t >= 0]
        marker_h  = max(real_vals) * 1.3 if real_vals else 10800.0
        plot_vals = [marker_h if to else (t or 0) for t, to in zip(totals, is_timeout)]

        fig, ax = plt.subplots(figsize=(9, 5))
        colors = ["#C44E52" if to else PALETTE[0] for to in is_timeout]
        bars = ax.bar(xs, plot_vals, color=colors, alpha=0.85,
                      edgecolor="white", linewidth=0.5)

        # Hatch timed-out bars and annotate ">3 hrs".
        for bar, to in zip(bars, is_timeout):
            if to:
                bar.set_hatch("//")

        labels = [">3 hrs" if to else f"{v:.2f}" for v, to in zip(plot_vals, is_timeout)]
        ax.bar_label(bars, labels=labels, padding=3, fontsize=7)

        ax.set_xlabel(_key_label(key_col), fontsize=11)
        ax.set_ylabel("Avg total workload time (s)", fontsize=11)
        ax.set_title(
            f"Total workload time — {w}  |  {_label(cg, cs)}",
            fontsize=12, fontweight="bold",
        )
        ax.set_xticks(xs)
        ax.set_xticklabels(keys, rotation=45, ha="right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
        
        b_data = baseline_data.get(w)
        if b_data:
            ax.axhline(y=b_data["total"], color="#333333", linestyle="--", label="Baseline", alpha=0.7)
            ax.legend(fontsize=9, loc="upper right")

        _finalise(fig, out_dir / f"{w}_{cg}_{cs}_total_time.png", show)


# ── plot 3: index size vs k / storage ─────────────────────────────────────────

def plot_index_size(avg_data: dict, out_dir: Path, show: bool, workload_filter: str):
    for (w, cg, cs), d in sorted(avg_data.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        sizes = d["size_mb"]
        if all(s is None for s in sizes):
            continue          # old data without size column — skip

        keys    = d["keys"]
        key_col = d["key_col"]
        xs      = np.arange(len(keys))

        # Detect timed-out configs.
        is_timeout = [s is not None and s < 0 for s in sizes]
        real_vals = [s for s in sizes if s is not None and s >= 0]
        marker_h  = max(real_vals) * 1.3 if real_vals else 100.0
        plot_vals = [marker_h if to else (s or 0) for s, to in zip(sizes, is_timeout)]

        fig, ax = plt.subplots(figsize=(9, 5))
        colors = ["#C44E52" if to else PALETTE[2] for to in is_timeout]
        bars = ax.bar(xs, plot_vals, color=colors, alpha=0.85,
                      edgecolor="white", linewidth=0.5)

        for bar, to in zip(bars, is_timeout):
            if to:
                bar.set_hatch("//")

        labels = ["N/A" if to else f"{v:.1f}" for v, to in zip(plot_vals, is_timeout)]
        ax.bar_label(bars, labels=labels, padding=3, fontsize=7)

        ax.set_xlabel(_key_label(key_col), fontsize=11)
        ax.set_ylabel("Actual index size (MB)", fontsize=11)
        ax.set_title(
            f"Index size — {w}  |  {_label(cg, cs)}",
            fontsize=12, fontweight="bold",
        )
        ax.set_xticks(xs)
        ax.set_xticklabels(keys, rotation=45, ha="right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())

        _finalise(fig, out_dir / f"{w}_{cg}_{cs}_index_size.png", show)


# ── plot 4: strategy comparison ────────────────────────────────────────────────

def plot_strategy_comparison(avg_data: dict, baseline_data: dict, out_dir: Path, show: bool, workload_filter: str):
    """
    One chart per workload.  Grouped bar chart — one bar per strategy per
    configuration point.  Because strategies may sweep different numbers of
    configurations (k vs storage_mb), each strategy uses its own x-positions
    within a shared group index space up to max(len) across all strategies.
    """
    by_workload = defaultdict(dict)
    for (w, cg, cs), d in avg_data.items():
        by_workload[w][(cg, cs)] = d

    for w, strategies in sorted(by_workload.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        sorted_strats = sorted(strategies.items())
        n_strats  = len(sorted_strats)
        max_pts   = max(len(d["keys"]) for _, d in sorted_strats)
        bar_w     = 0.8 / n_strats
        xs        = np.arange(max_pts)

        fig, ax = plt.subplots(figsize=(max(11, max_pts * 0.5 * n_strats * 0.3), 6))

        for i, ((cg, cs), d) in enumerate(sorted_strats):
            keys   = d["keys"]
            totals = d["total"]
            color  = PALETTE[i % len(PALETTE)]
            label  = _label(cg, cs)

            # Detect timed-out configs.
            is_timeout = [t is not None and t < 0 for t in totals]
            real_vals = [t for t in totals if t is not None and t >= 0]
            marker_h  = max(real_vals) * 1.3 if real_vals else 10800.0
            plot_vals = [marker_h if to else (t or 0)
                         for t, to in zip(totals, is_timeout)]

            n = len(keys)
            offsets = np.arange(n) + i * bar_w - (n_strats - 1) * bar_w / 2
            bars = ax.bar(offsets, plot_vals, bar_w, label=label,
                          color=color, alpha=0.85, edgecolor="white", linewidth=0.5)

            for bar, to in zip(bars, is_timeout):
                if to:
                    bar.set_hatch("//")

            bar_labels = [">3 hrs" if to else f"{v:.1f}"
                          for v, to in zip(plot_vals, is_timeout)]
            ax.bar_label(bars, labels=bar_labels, padding=2, fontsize=6, rotation=90)

        ax.set_xlabel(_key_label("mixed"), fontsize=11)
        ax.set_ylabel("Avg total workload time (s)", fontsize=11)
        ax.set_title(
            f"Strategy comparison — {w}",
            fontsize=13, fontweight="bold",
        )
        # use the strategy with most points for x tick labels
        ref_keys = max((d["keys"] for _, d in sorted_strats), key=len)
        ax.set_xticks(np.arange(len(ref_keys)))
        ax.set_xticklabels(ref_keys, rotation=45, ha="right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
        ax.legend(title="Strategy (cg / cs)", fontsize=9, title_fontsize=9,
                  bbox_to_anchor=(1.01, 1), loc="upper left")

        _finalise(fig, out_dir / f"{w}_strategy_comparison.png", show)


# ── plot 5: strategy timing (candidate gen + config selection) ─────────────────

def plot_strategy_time(strategy_time_data: dict, out_dir: Path, show: bool,
                       workload_filter: str):
    """
    Horizontal stacked bar chart showing how long each strategy
    (cg + cs combination) took — broken down into candidate
    generation time and config selection time.

    One chart per workload.
    """
    by_workload = defaultdict(dict)
    for (w, cg, cs), d in strategy_time_data.items():
        by_workload[w][(cg, cs)] = d

    for w, strategies in sorted(by_workload.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        sorted_strats = sorted(strategies.items())
        labels   = [_label(cg, cs) for (cg, cs), _ in sorted_strats]
        cg_times = [d["cg_time"] or 0.0 for _, d in sorted_strats]
        cs_times = [d["cs_time"] for _, d in sorted_strats]

        y = np.arange(len(labels))
        bar_h = 0.5

        fig, ax = plt.subplots(figsize=(10, max(4, len(labels) * 0.7)))

        bars_cg = ax.barh(y, cg_times, bar_h, label="Candidate generation",
                          color=PALETTE[0], alpha=0.85, edgecolor="white", linewidth=0.5)
        bars_cs = ax.barh(y, cs_times, bar_h, left=cg_times,
                          label="Config selection",
                          color=PALETTE[1], alpha=0.85, edgecolor="white", linewidth=0.5)

        # Annotate total time at the end of each bar.
        for i, (cg_t, cs_t) in enumerate(zip(cg_times, cs_times)):
            total = cg_t + cs_t
            ax.text(total + max(cg_times + cs_times) * 0.01, i,
                    f"{total:.1f}s", va="center", fontsize=8)

        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("Time (s)", fontsize=11)
        ax.set_title(
            f"Strategy timing breakdown — {w}",
            fontsize=12, fontweight="bold",
        )
        ax.grid(axis="x", linestyle="--", alpha=0.4)
        ax.xaxis.set_minor_locator(ticker.AutoMinorLocator())
        ax.legend(fontsize=9, loc="lower right")
        ax.invert_yaxis()  # top-to-bottom order

        _finalise(fig, out_dir / f"{w}_strategy_time.png", show)


# ── plot 6: HypoPG estimated cost vs budget / k ───────────────────────────

def plot_hypopg_cost(hypopg_data: dict, out_dir: Path, show: bool,
                     workload_filter: str):
    """
    Bar chart showing the HypoPG-estimated workload cost for each
    configuration point (k or storage_mb).  One chart per (workload, cg, cs).
    """
    for (w, cg, cs), d in sorted(hypopg_data.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        keys     = d["keys"]
        costs    = d["costs"]
        key_col  = d["key_col"]
        xs       = np.arange(len(keys))

        fig, ax = plt.subplots(figsize=(9, 5))
        bars = ax.bar(xs, costs, color=PALETTE[4], alpha=0.85,
                      edgecolor="white", linewidth=0.5)
        ax.bar_label(bars, labels=[f"{c:,.0f}" for c in costs],
                     padding=3, fontsize=7)

        ax.set_xlabel(_key_label(key_col), fontsize=11)
        ax.set_ylabel("HypoPG estimated cost", fontsize=11)
        ax.set_title(
            f"HypoPG cost — {w}  |  {_label(cg, cs)}",
            fontsize=12, fontweight="bold",
        )
        ax.set_xticks(xs)
        ax.set_xticklabels(keys, rotation=45, ha="right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())

        _finalise(fig, out_dir / f"{w}_{cg}_{cs}_hypopg_cost.png", show)


# ── plot 7: HypoPG cost strategy comparison ───────────────────────────────

def plot_hypopg_cost_comparison(hypopg_data: dict, out_dir: Path, show: bool,
                                workload_filter: str):
    """
    Grouped bar chart comparing HypoPG-estimated workload costs across all
    strategies for a given workload.  One chart per workload.
    """
    by_workload = defaultdict(dict)
    for (w, cg, cs), d in hypopg_data.items():
        by_workload[w][(cg, cs)] = d

    for w, strategies in sorted(by_workload.items()):
        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        sorted_strats = sorted(strategies.items())
        n_strats = len(sorted_strats)
        max_pts  = max(len(d["keys"]) for _, d in sorted_strats)
        bar_w    = 0.8 / n_strats
        xs       = np.arange(max_pts)

        fig, ax = plt.subplots(figsize=(max(11, max_pts * 0.5 * n_strats * 0.3), 6))

        for i, ((cg, cs), d) in enumerate(sorted_strats):
            keys  = d["keys"]
            costs = d["costs"]
            color = PALETTE[i % len(PALETTE)]
            label = _label(cg, cs)

            n = len(keys)
            offsets = np.arange(n) + i * bar_w - (n_strats - 1) * bar_w / 2
            bars = ax.bar(offsets, costs, bar_w, label=label,
                          color=color, alpha=0.85, edgecolor="white", linewidth=0.5)
            ax.bar_label(bars, labels=[f"{c:,.0f}" for c in costs],
                         padding=2, fontsize=6, rotation=90)

        ax.set_xlabel(_key_label("mixed"), fontsize=11)
        ax.set_ylabel("HypoPG estimated cost", fontsize=11)
        ax.set_title(
            f"HypoPG cost comparison — {w}",
            fontsize=13, fontweight="bold",
        )
        ref_keys = max((d["keys"] for _, d in sorted_strats), key=len)
        ax.set_xticks(np.arange(len(ref_keys)))
        ax.set_xticklabels(ref_keys, rotation=45, ha="right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
        ax.legend(title="Strategy (cg / cs)", fontsize=9, title_fontsize=9,
                  bbox_to_anchor=(1.01, 1), loc="upper left")

        _finalise(fig, out_dir / f"{w}_hypopg_cost_comparison.png", show)


# ── shared finalise ────────────────────────────────────────────────────────────

def _finalise(fig, path: Path, show: bool):
    fig.tight_layout()
    if show:
        plt.show()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        print(f"  saved → {path.relative_to(RESULTS_DIR.parent)}")
    plt.close(fig)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Plot index-selection experiment results.")
    ap.add_argument("--workload", default="", help="Filter by workload name substring.")
    ap.add_argument("--show", action="store_true", help="Display plots interactively.")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR),
                    help="Path to results directory (default: results/ next to this script).")
    ap.add_argument("--plots-dir", default=str(PLOTS_DIR),
                    help="Where to save plots (default: plots/ at the repo root).")
    args = ap.parse_args()

    if args.show:
        matplotlib.use("TkAgg")   # switch to interactive backend

    results_dir = Path(args.results_dir)
    out_dir     = Path(args.plots_dir)

    print("Collecting data …")
    qt_groups = collect_qt_files(results_dir)
    avg_data  = collect_avg_files(results_dir)
    baseline_data = collect_baseline_files(results_dir)

    print(f"  {len(qt_groups)} qt groups,  {len(avg_data)} avg groups")
    print(f"  output → {out_dir}\n")

    print("Plot 1: Query time bar charts …")
    plot_query_bars(qt_groups, baseline_data, out_dir, args.show, args.workload)

    print("Plot 2: Total workload time …")
    plot_total_time(avg_data, baseline_data, out_dir, args.show, args.workload)

    print("Plot 3: Index size …")
    plot_index_size(avg_data, out_dir, args.show, args.workload)

    print("Plot 4: Strategy comparison …")
    plot_strategy_comparison(avg_data, baseline_data, out_dir, args.show, args.workload)

    print("Plot 5: Strategy timing breakdown …")
    st_data = collect_strategy_time_files(results_dir)
    plot_strategy_time(st_data, out_dir, args.show, args.workload)

    print("Plot 6: HypoPG estimated cost …")
    hc_data = collect_hypopg_cost_files(results_dir)
    plot_hypopg_cost(hc_data, out_dir, args.show, args.workload)

    print("Plot 7: HypoPG cost comparison …")
    plot_hypopg_cost_comparison(hc_data, out_dir, args.show, args.workload)

    print("\nAll done.")


if __name__ == "__main__":
    main()
