import sys
import importlib
from pathlib import Path
from pyprojroot import here
import psycopg2
from dotenv import load_dotenv
import os
from auto_index_selector.utility import *
from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)


try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


import time
import csv

from tests.plot_results import (
    collect_qt_files,
    collect_avg_files,
    collect_strategy_time_files,
    collect_hypopg_cost_files,
    collect_baseline_files,
    plot_query_bars,
    plot_total_time,
    plot_index_size,
    plot_strategy_comparison,
    plot_strategy_time,
    plot_hypopg_cost,
    plot_hypopg_cost_comparison,
    PLOTS_DIR,
    RESULTS_DIR,
)

ITERATIONS = 1
# Number of parallel HypoPG worker processes used by cs_greedy / cs_drop.
# Set to 1 to force fully serial execution (useful for profiling / debugging).
# None means cpu_count()-1.
N_WORKERS = 8
# Maximum wall-clock seconds for one config point (warmup + all iterations).
# If exceeded, the config is abandoned and marked ">3 hrs" in plots.
SINGLE_CONFIG_TIMEOUT = 3 * 3600  # 3 hours
DEFAULT_QUERY_TIMINGS_PATH = Path(str(here() / "results" / "rb_g" / "tpcds"))
DEFAULT_TOTAL_TIMINGS_PATH = Path(str(here() / "results" / "rb_g" / "tpcds_total"))


class ConfigTimeout(Exception):
    """Raised when a single configuration exceeds SINGLE_CONFIG_TIMEOUT."""

    pass


def execute_sql_file(conn, path: Path) -> None:
    """
    Execute every statement in a .sql file against `conn` and commit.
    Executes statement-by-statement to handle individual Postgres index limits gracefully.
    """
    sql_text = Path(path).read_text()
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    for stmt in statements:
        try:
            with conn.cursor() as cur:
                cur.execute(stmt + ";")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"Warning: Failed to execute '{stmt}': {e}")


def measure_index_size(conn) -> int:
    """
    Return the total actual on-disk size (in bytes) of all non-system indexes
    currently present in the database, using pg_relation_size().

    Covers only indexes in the 'public' schema (adjust schemaname filter if needed).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(pg_relation_size(indexrelid)), 0)
            FROM pg_indexes
            JOIN pg_class idx ON idx.relname = pg_indexes.indexname
            JOIN pg_index pi ON pi.indexrelid = idx.oid
            WHERE schemaname = 'public'
        """)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def measure_workload(conn, W, iterations, deadline=None):
    """
    Run the workload `iterations` times and return per-query timing dicts.
    A single un-timed warm-up pass is executed first so that OS page-cache
    cold-start effects don't skew the first measured iteration.

    If `deadline` (a time.monotonic() timestamp) is given, a ConfigTimeout
    is raised as soon as wall-clock time exceeds it — even during warmup.
    """

    def _check_deadline(phase: str):
        if deadline is not None and time.monotonic() > deadline:
            raise ConfigTimeout(
                f"Exceeded {SINGLE_CONFIG_TIMEOUT}s timeout during {phase}"
            )

    # Warm-up: one un-timed pass to prime the buffer pool and plan cache.
    print("Warm-up pass … ", end="", flush=True)
    with conn.cursor() as cur:
        for q in W:
            _check_deadline("warmup")
            cur.execute(getattr(q, "query", q))
            if cur.description is not None:
                cur.fetchall()
    conn.rollback()
    print("done")
    _check_deadline("warmup")

    results = []
    for it in range(1, iterations + 1):
        print(f"Iteration {it}", end=" : ")
        timings = {}
        iter_total = 0.0
        with conn.cursor() as cur:
            for i, q in enumerate(W):
                _check_deadline(f"iteration {it}")
                query_sql = getattr(q, "query", q)
                label = (
                    getattr(q, "query_id", None)
                    or getattr(q, "qid", None)
                    or f"q{i + 1}"
                )

                start = time.perf_counter()
                cur.execute(query_sql)
                if (
                    cur.description is not None
                ):  # drain results so timing covers full execution
                    cur.fetchall()
                elapsed = time.perf_counter() - start

                timings[label] = elapsed
                iter_total += elapsed
                print(f"{i}", end=", ")
        conn.rollback()
        print("")
        print(f"[Iteration {it}] total: {iter_total:.4f}s")
        # for label, elapsed in timings.items():
        #     print(f"    {label}: {elapsed:.4f}s")
        # print("")
        results.append({"iteration": it, "timings": timings, "total": iter_total})

    return results


def write_query_timings_csv(
    results: list, output_path: Path = DEFAULT_QUERY_TIMINGS_PATH
) -> Path:
    """
    Write the average run time per query, averaged across all
    iterations, to a CSV file (one row per query).

    `results` is the list returned by measure_workload():
        [{"iteration": 1, "timings": {label: seconds, ...}, "total": seconds}, ...]

    Output columns: query, avg_time_seconds
    """
    sums: dict = {}
    counts: dict = {}
    for r in results:
        for label, elapsed in r["timings"].items():
            sums[label] = sums.get(label, 0.0) + elapsed
            counts[label] = counts.get(label, 0) + 1

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "avg_time_seconds"])
        for label in sorted(sums):
            avg = sums[label] / counts[label]
            writer.writerow([label, f"{avg:.6f}"])
    return output_path


def write_total_timings_csv(
    rows: list,
    output_path: Path,
    key_col: str = "k",
) -> Path:
    """
    Write all per-configuration total timing rows to a single CSV.

    Each row in `rows` is a dict produced by exp_k / exp_size:
        {key_col: value, "avg_total_time_seconds": float,
         "index_size_bytes": int, "index_size_mb": float}

    Output columns: <key_col>, avg_total_time_seconds, index_size_bytes, index_size_mb
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                key_col,
                "avg_total_time_seconds",
                "index_size_bytes",
                "index_size_mb",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return output_path


def _sort_query_labels(labels) -> list:
    """Sort query labels like 'Q1', 'q10', 'Q2' in natural numeric order."""

    def key(label):
        digits = "".join(ch for ch in str(label) if ch.isdigit())
        return (int(digits) if digits else 0, str(label))

    return sorted(labels, key=key)


# Sentinel value written to CSV when a config is skipped due to timeout.
# Plotting code checks for this to render ">3 hrs" annotations.
TIMEOUT_SENTINEL = -1


def _compute_hypopg_cost(conn, w, config, write_penalties=None):
    """
    Compute the HypoPG-estimated total workload cost for a selected config.

    Note: The actual timing code uses native pg_stat_statements. This
    function is only used to dump out the CSV for the theoretical
    estimated cost — the same metric the config selection algorithms optimise.
    """
    queries = [getattr(q, "query", q) for q in w]
    return estimateWorkloadCostForConfig(conn, queries, config, write_penalties=write_penalties)


def _write_hypopg_cost_csv(
    rows: list,
    output_path: Path,
    key_col: str = "k",
) -> Path:
    """
    Write per-configuration HypoPG estimated costs to a CSV.

    Each row is a dict: {key_col: value, "hypopg_cost": float}.
    Output columns: <key_col>, hypopg_cost
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[key_col, "hypopg_cost"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return output_path


def exp_run(conn, cg, cs, w, w_name, key_col: str, key_val) -> dict:
    """
    Measure workload timing and actual index size for one configuration.
    key_col : "k" (greedy) or "storage_mb" (drop / extend)
    key_val : the numeric value for that column

    Returns one result dict (one row for the consolidated total CSV).
    Per-query timings are written to their own file.

    If the measurement exceeds SINGLE_CONFIG_TIMEOUT the config is
    abandoned and sentinel value -1 is recorded in the CSV so that
    plots can annotate it as "> 3 hrs".
    """
    deadline = time.monotonic() + SINGLE_CONFIG_TIMEOUT
    try:
        all_results = measure_workload(
            conn, w, iterations=ITERATIONS, deadline=deadline
        )
    except ConfigTimeout as exc:
        print(f"\n⏰ TIMEOUT: {exc}")
        print(f"Skipping config {key_col}={key_val} — marking as >3 hrs")
        return {
            key_col: key_val,
            "avg_total_time_seconds": str(TIMEOUT_SENTINEL),
            "index_size_bytes": TIMEOUT_SENTINEL,
            "index_size_mb": str(TIMEOUT_SENTINEL),
        }

    avg_total = sum(r["total"] for r in all_results) / len(all_results)
    print(f"\nAverage total time over {ITERATIONS} iterations: {avg_total:.4f}s")

    idx_size = measure_index_size(conn)
    print(f"Actual index size: {idx_size / (1024**2):.2f} MB ({idx_size:,} bytes)")

    # Build CSV filename suffix from key_col / key_val
    suffix = f"{key_val}" if key_col == "storage_mb" else f"{key_val}_2"
    query_csv_path = write_query_timings_csv(
        all_results,
        Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_{suffix}_qt.csv")),
    )
    print(f"Wrote per-query average timings to {query_csv_path}")

    return {
        key_col: key_val,
        "avg_total_time_seconds": f"{avg_total:.6f}",
        "index_size_bytes": idx_size,
        "index_size_mb": f"{idx_size / (1024**2):.4f}",
    }


# Keep old names as thin aliases so any external callers still work.
def exp_k(conn, cg, cs, w, w_name, k, m) -> dict:
    return exp_run(conn, cg, cs, w, w_name, key_col="k", key_val=k)


def exp_size(conn, cg, cs, w, w_name, storage) -> dict:
    return exp_run(conn, cg, cs, w, w_name, key_col="storage_mb", key_val=storage)


def _plot_for_run(w_name: str, cg: str, cs: str) -> None:
    """
    Re-generate all plot types for the just-completed (w_name, cg, cs) run.
    Only files matching this configuration are plotted, so other experiments
    in results/ are not re-processed.
    """
    print("\nGenerating plots …")
    qt_groups = collect_qt_files(RESULTS_DIR)
    avg_data = collect_avg_files(RESULTS_DIR)
    st_data = collect_strategy_time_files(RESULTS_DIR)
    hc_data = collect_hypopg_cost_files(RESULTS_DIR)
    baseline_data = collect_baseline_files(RESULTS_DIR)

    # Filter to only the current (w_name, cg, cs) key
    qt_filtered = {k: v for k, v in qt_groups.items() if k == (w_name, cg, cs)}
    avg_filtered = {k: v for k, v in avg_data.items() if k == (w_name, cg, cs)}
    hc_filtered = {k: v for k, v in hc_data.items() if k == (w_name, cg, cs)}
    # Strategy/comparison plots need all strategies for this workload
    avg_all_strats = {k: v for k, v in avg_data.items() if k[0] == w_name}
    st_all_strats = {k: v for k, v in st_data.items() if k[0] == w_name}
    hc_all_strats = {k: v for k, v in hc_data.items() if k[0] == w_name}

    plot_query_bars(
        qt_filtered, baseline_data, PLOTS_DIR, show=False, workload_filter=w_name
    )
    plot_total_time(
        avg_filtered, baseline_data, PLOTS_DIR, show=False, workload_filter=w_name
    )
    plot_index_size(avg_filtered, PLOTS_DIR, show=False, workload_filter=w_name)
    plot_strategy_comparison(
        avg_all_strats, baseline_data, PLOTS_DIR, show=False, workload_filter=w_name
    )
    plot_strategy_time(st_all_strats, PLOTS_DIR, show=False, workload_filter=w_name)
    plot_hypopg_cost(hc_filtered, PLOTS_DIR, show=False, workload_filter=w_name)
    plot_hypopg_cost_comparison(
        hc_all_strats, PLOTS_DIR, show=False, workload_filter=w_name
    )
    print(f"Plots saved to {PLOTS_DIR}")


def _write_strategy_timing_csv(
    w_name: str,
    cg: str,
    cs: str,
    *,
    cg_elapsed: float | None,
    cs_elapsed: float,
    total_elapsed: float,
) -> Path:
    """
    Write a one-row CSV capturing how long the strategy took to
    generate candidates (cg_elapsed) and select configurations (cs_elapsed).

    Output: results/<w_name>_<cg>_<cs>_strategy_time.csv
    Columns: workload, candidate_generation, config_selection,
             cg_time_seconds, cs_time_seconds, total_time_seconds
    """
    out = Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_strategy_time.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workload",
                "candidate_generation",
                "config_selection",
                "cg_time_seconds",
                "cs_time_seconds",
                "total_time_seconds",
            ]
        )
        writer.writerow(
            [
                w_name,
                cg,
                cs,
                f"{cg_elapsed:.6f}" if cg_elapsed is not None else "",
                f"{cs_elapsed:.6f}",
                f"{total_elapsed:.6f}",
            ]
        )
    print(f"Wrote strategy timing to {out}")
    return out


def test_strategy(
    conn, cg, cs, w_name, w, candidate_indexes, db_name=None, cg_elapsed=None, write_penalties=None
):
    cs_module = importlib.import_module(f"auto_index_selector.ConfigSelection.{cs}")
    if cs in ["cs_drop", "cs_extend", "cs_ben_knap"]:
        budgets_mb = []
        if w_name == "tpchWorkload":
            budgets_mb = [
                50,
                100,
                150,
                200,
                250,
                300,
                350,
                400,
                450,
                500,
            ]
        elif w_name == "jobWorkload":
            budgets_mb = [1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000]
        elif w_name == "tpcdsWorkload":
            budgets_mb = [500, 1000, 1500, 2000, 2500, 3000]
        elif w_name.startswith("tpccWorkload"):
            budgets_mb = [
                50,
                100,
                150,
                200,
                250,
                300,
                # 350,
                # 400,
                # 450,
                # 500,
            ][::-1]
        # One pce session + shared cost_cache for all budgets — much faster.
        cs_start = time.perf_counter()
        configs = cs_module.selectConfigurations(
            conn,
            w,
            candidate_indexes,
            storage_budgets_mb=budgets_mb,
            db_name=db_name,
            n_workers=N_WORKERS,
            write_penalties=write_penalties,
        )
        conn.rollback()  # Release any AccessShareLocks held by cost estimation during selectConfigurations
        cs_elapsed = time.perf_counter() - cs_start
        total_strategy_time = (cg_elapsed or 0) + cs_elapsed
        print(f"\n[TIMING] Config selection ({cs}): {cs_elapsed:.4f}s")
        if cg_elapsed is not None:
            print(f"[TIMING] Candidate generation ({cg}): {cg_elapsed:.4f}s")
        print(f"[TIMING] Total strategy time ({cg}+{cs}): {total_strategy_time:.4f}s")
        _write_strategy_timing_csv(
            w_name,
            cg,
            cs,
            cg_elapsed=cg_elapsed,
            cs_elapsed=cs_elapsed,
            total_elapsed=total_strategy_time,
        )

        total_rows = []
        hypopg_rows = []
        for budget_bytes, config in sorted(configs.items()):
            # for budget_bytes in budgets_mb[::-1]:
            storage = budget_bytes // (1024 * 1024)  # MB label used in file names
            # storage = budget_bytes

            print(f"\n--- Budget {storage} MB ---")
            print(f"config: {config}, {len(config)}, {type(config)}")

            # HypoPG estimated cost for this config
            hcost = _compute_hypopg_cost(conn, w, config, write_penalties=write_penalties)
            conn.rollback()  # Release AccessShareLocks from EXPLAIN so fresh_conn can CREATE INDEX
            print(f"[HYPOPG] Estimated workload cost: {hcost:,.2f}")
            hypopg_rows.append({"storage_mb": storage, "hypopg_cost": f"{hcost:.4f}"})

            create_path = generate_create_index_sql(
                config,
                Path(
                    str(here() / "indexes" / f"{w_name}_{cg}_{cs}_{storage}_create.sql")
                ),
            )
            delete_path = generate_delete_index_sql(
                config,
                Path(
                    str(here() / "indexes" / f"{w_name}_{cg}_{cs}_{storage}_delete.sql")
                ),
            )
            # create_path = Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{storage}_create.sql'))
            # delete_path = Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{storage}_delete.sql'))

            if len(config) == 0:
                print("No indexes selected")
                continue
                
            # Create a fresh connection to avoid PostgreSQL backend memory leaks across budgets
            dsn = conn.get_dsn_parameters()
            dsn.pop("tty", None)  # psycopg2 sometimes returns unsupported kwargs
            fresh_conn = psycopg2.connect(**dsn)
            fresh_conn.autocommit = False
            with fresh_conn.cursor() as cur:
                cur.execute("SET work_mem = '4MB';")
                cur.execute("SET random_page_cost = 4.0;")
                cur.execute("SET cpu_index_tuple_cost = 0.005;")
                cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg;")
                cur.execute("LOAD 'advisor_write_stats';")
                cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats;")

            execute_sql_file(fresh_conn, create_path)
            print(f"Created indexes from {create_path}")

            # Flush cached query plans so the planner picks up the new indexes.
            with fresh_conn.cursor() as cur:
                cur.execute("DISCARD PLANS;")
            fresh_conn.commit()

            row = exp_run(
                fresh_conn, cg, cs, w, w_name, key_col="storage_mb", key_val=storage
            )
            total_rows.append(row)
            print("Experiment done")

            execute_sql_file(fresh_conn, delete_path)
            print(f"Dropped indexes using {delete_path}")
            
            fresh_conn.close()

        # Write HypoPG cost CSV
        hypopg_csv_path = _write_hypopg_cost_csv(
            hypopg_rows,
            Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_hypopg_cost.csv")),
            key_col="storage_mb",
        )
        print(f"Wrote HypoPG cost data to {hypopg_csv_path}")

        # Write one consolidated total CSV for this (cg, cs, workload) run.
        total_csv_path = write_total_timings_csv(
            total_rows,
            Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_avg.csv")),
            key_col="storage_mb",
        )
        print(f"\nWrote consolidated total timings to {total_csv_path}")
        _plot_for_run(w_name, cg, cs)

    elif cs in ["cs_greedy"]:
        k_list = [
            2, 3, 4, 5, 6, 7, 
            # 8, 9, 10
        ]
        # One pce session + shared cost_cache for all k values — single forward pass.
        cs_start = time.perf_counter()
        configs = cs_module.selectConfigurations(
            conn,
            w,
            candidate_indexes,
            k_list=k_list,
            m=2,
            db_name=db_name,
            n_workers=N_WORKERS,
            write_penalties=write_penalties,
        )
        cs_elapsed = time.perf_counter() - cs_start
        total_strategy_time = (cg_elapsed or 0) + cs_elapsed
        print(f"\n[TIMING] Config selection ({cs}): {cs_elapsed:.4f}s")
        if cg_elapsed is not None:
            print(f"[TIMING] Candidate generation ({cg}): {cg_elapsed:.4f}s")
        print(f"[TIMING] Total strategy time ({cg}+{cs}): {total_strategy_time:.4f}s")
        _write_strategy_timing_csv(
            w_name,
            cg,
            cs,
            cg_elapsed=cg_elapsed,
            cs_elapsed=cs_elapsed,
            total_elapsed=total_strategy_time,
        )
        total_rows = []
        hypopg_rows = []
        for k, config in sorted(configs.items()):
            print(f"\n--- Greedy k={k}, m=2 ---")

            # HypoPG estimated cost for this config
            hcost = _compute_hypopg_cost(conn, w, config, write_penalties=write_penalties)
            print(f"[HYPOPG] Estimated workload cost: {hcost:,.2f}")
            hypopg_rows.append({"k": k, "hypopg_cost": f"{hcost:.4f}"})

            create_path = generate_create_index_sql(
                config,
                Path(
                    str(here() / "indexes" / f"{w_name}_{cg}_{cs}_{k}_{2}_create.sql")
                ),
            )
            delete_path = generate_delete_index_sql(
                config,
                Path(
                    str(here() / "indexes" / f"{w_name}_{cg}_{cs}_{k}_{2}_delete.sql")
                ),
            )

            execute_sql_file(conn, create_path)
            print(f"Created indexes from {create_path}")

            # Flush cached query plans so the planner picks up the new indexes.
            with conn.cursor() as cur:
                cur.execute("DISCARD PLANS;")
            conn.commit()

            row = exp_run(conn, cg, cs, w, w_name, key_col="k", key_val=k)
            total_rows.append(row)
            print("Experiment done")

            execute_sql_file(conn, delete_path)
            print(f"Dropped indexes using {delete_path}")

        # Write HypoPG cost CSV
        hypopg_csv_path = _write_hypopg_cost_csv(
            hypopg_rows,
            Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_hypopg_cost.csv")),
            key_col="k",
        )
        print(f"Wrote HypoPG cost data to {hypopg_csv_path}")

        # Write one consolidated total CSV for this (cg, cs, workload) run.
        total_csv_path = write_total_timings_csv(
            total_rows,
            Path(str(here() / "results" / f"{w_name}_{cg}_{cs}_avg.csv")),
            key_col="k",
        )
        print(f"\nWrote consolidated total timings to {total_csv_path}")
        _plot_for_run(w_name, cg, cs)
    else:
        print(f"Error: {cs} doesn't exist.")


