import sys
import importlib
from pathlib import Path
from pyprojroot import here
import psycopg2
from dotenv import load_dotenv
import os
from auto_index_selector.utility import *


try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import auto_index_selector.Workload.tpcdsWorkload as wl_module
import time
import csv
import importlib

ITERATIONS = 3
DEFAULT_QUERY_TIMINGS_PATH = Path(str(here() / 'results' / 'rb_g'  / 'tpcds'))
DEFAULT_TOTAL_TIMINGS_PATH = Path(str(here() / 'results' / 'rb_g'  / 'tpcds_total'))
def execute_sql_file(conn, path: Path) -> None:
    """
    Execute every statement in a .sql file (e.g. create_index.sql or
    delete_index.sql) against `conn` and commit.
    """
    sql_text = Path(path).read_text()
    with conn.cursor() as cur:
        cur.execute(sql_text)
    conn.commit()


def measure_workload(conn, W, iterations):
    results = []
    for it in range(1, iterations + 1):
        print(f"Iteration {it}", end=" : ")
        timings = {}
        iter_total = 0.0
        with conn.cursor() as cur:
            for i, q in enumerate(W):
                query_sql = getattr(q, "query", q)
                label = getattr(q, "query_id", None) or getattr(q, "qid", None) or f"q{i + 1}"
 
                start = time.perf_counter()
                cur.execute(query_sql)
                if cur.description is not None:  # drain results so timing covers full execution
                    cur.fetchall()
                elapsed = time.perf_counter() - start
 
                timings[label] = elapsed
                iter_total += elapsed
                print(f"{i}", end=', ')
        conn.commit()
        print("")
        print(f"[Iteration {it}] total: {iter_total:.4f}s")
        for label, elapsed in timings.items():
            print(f"    {label}: {elapsed:.4f}s")
        print("")
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
    results: list, output_path: Path = DEFAULT_TOTAL_TIMINGS_PATH
) -> Path:
    """
    Write the average total workload run time, averaged across all
    iterations, to a CSV file (single row).
 
    `results` is the list returned by measure_workload().
 
    Output columns: avg_total_time_seconds
    """
    avg_total = sum(r["total"] for r in results) / len(results)
 
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["avg_total_time_seconds"])
        writer.writerow([f"{avg_total:.6f}"])
    return output_path

def _sort_query_labels(labels) -> list:
    """Sort query labels like 'Q1', 'q10', 'Q2' in natural numeric order."""
 
    def key(label):
        digits = "".join(ch for ch in str(label) if ch.isdigit())
        return (int(digits) if digits else 0, str(label))
 
    return sorted(labels, key=key)


def exp_k(conn, cg, cs, w, w_name, k, m):
    all_results = measure_workload(conn, w, iterations=ITERATIONS)
    avg_total = sum(r["total"] for r in all_results) / len(all_results)
    print(f"\nAverage total time over {ITERATIONS} iterations: {avg_total:.4f}s")

    query_csv_path = write_query_timings_csv(all_results, Path(str(here() / 'results' / f'{w_name}_{cg}_{cs}_{k}_{m}_qt.csv')))
    total_csv_path = write_total_timings_csv(all_results, Path(str(here() / 'results' / f'{w_name}_{cg}_{cs}_{k}_{m}_avg.csv')))
    print(f"Wrote per-query average timings to {query_csv_path}")
    print(f"Wrote average total workload timing to {total_csv_path}")
    pass

def exp_size(conn, cg, cs, w, w_name, storage):
    all_results = measure_workload(conn, w, iterations=ITERATIONS)
    avg_total = sum(r["total"] for r in all_results) / len(all_results)
    print(f"\nAverage total time over {ITERATIONS} iterations: {avg_total:.4f}s")

    query_csv_path = write_query_timings_csv(all_results, Path(str(here() / 'results' / f'{w_name}_{cg}_{cs}_{storage}_qt.csv')))
    total_csv_path = write_total_timings_csv(all_results, Path(str(here() / 'results' / f'{w_name}_{cg}_{cs}_{storage}_avg.csv')))
    print(f"Wrote per-query average timings to {query_csv_path}")
    print(f"Wrote average total workload timing to {total_csv_path}")
    pass

def test_strategy(conn, cg, cs, w_name, w, candidate_indexes):
    cs_module = importlib.import_module(f"auto_index_selector.ConfigSelection.{cs}")
    
    if cs in ['cs_drop', 'cs_extend']:
        for storage in [50, 100, 150, 200, 250, 300, 350, 400, 450, 500]:
            config = cs_module.selectConfiguration(conn, w, candidate_indexes, storage)
            create_path = generate_create_index_sql(config, Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{storage}_create.sql')))
            delete_path = generate_delete_index_sql(config, Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{storage}_delete.sql')))

            execute_sql_file(conn, create_path)
            print(f"Created indexes from {create_path}")

            exp_size(conn, cg, cs, w, w_name, storage)
            print("Experiment done")

            execute_sql_file(conn, delete_path)
            print(f"Dropped indexes using {delete_path}")
    elif cs in ['cs_greedy']:
        for k in [2, 3, 4, 5, 6, 7, 8, 9, 10]:
            config = cs_module.selectConfiguration(conn, w, candidate_indexes, k=k, m=2)
            create_path = generate_create_index_sql(config, Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{k}_{2}_create.sql')))
            delete_path = generate_delete_index_sql(config, Path(str(here() / 'indexes' / f'{w_name}_{cg}_{cs}_{k}_{2}_delete.sql')))

            execute_sql_file(conn, create_path)
            print(f"Created indexes from {create_path}")

            exp_k(conn, cg, cs, w, w_name, k, m=2)
            print("Experiment done")

            execute_sql_file(conn, delete_path)
            print(f"Dropped indexes using {delete_path}")
    else:
        print(f"Error: {cs} doesn't exist.")

