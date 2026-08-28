"""
tests/tpcc_standard/strategy_runner.py
Runs a single (CG, CS, params) combination on the official TPC-C workload.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .measure import measure_workload, write_csv, discard_session

INDEXES_DIR = Path(__file__).parent / "indexes"


def _normalise_candidates(candidates: Dict) -> Dict:
    if not isinstance(candidates, dict):
        return candidates

    normalised: Dict = {}
    for table, index_list in candidates.items():
        norm_list = []
        for item in index_list:
            if isinstance(item, tuple):
                norm_list.append(item)
            elif isinstance(item, list):
                norm_list.append(tuple(item))
            elif isinstance(item, str):
                norm_list.append((item,))
            else:
                norm_list.append(tuple(item))
        normalised[table] = norm_list
    return normalised


def build_index_name(table: str, cols: Tuple[str, ...]) -> str:
    return f"ais_tpcc_std_{table}_{'_'.join(cols)}"


def create_indexes(conn, config: frozenset) -> None:
    if not config:
        return
    with conn.cursor() as cur:
        for table, cols in sorted(config):
            idx_name = build_index_name(table, cols)
            sql = (
                f"CREATE INDEX IF NOT EXISTS {idx_name} "
                f"ON {table} ({', '.join(cols)});"
            )
            try:
                cur.execute(sql)
            except Exception as exc:
                print(f"  [WARN] Could not create {idx_name}: {exc}")
                conn.rollback()
    conn.commit()
    print(f"  Created {len(config)} index(es): "
          + ", ".join(f"{t}({','.join(c)})" for t, c in sorted(config)))


def drop_indexes(conn, config: frozenset) -> None:
    if not config:
        return
    with conn.cursor() as cur:
        for table, cols in sorted(config):
            idx_name = build_index_name(table, cols)
            try:
                cur.execute(f"DROP INDEX IF EXISTS {idx_name};")
            except Exception as exc:
                print(f"  [WARN] Could not drop {idx_name}: {exc}")
                conn.rollback()
    conn.commit()
    discard_session(conn)


def save_index_sql(
    config: frozenset,
    label: str,
    indexes_dir: Optional[Path] = None,
) -> None:
    out_dir = indexes_dir or INDEXES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    create_lines, drop_lines = [], []
    for table, cols in sorted(config):
        idx_name = build_index_name(table, cols)
        create_lines.append(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({', '.join(cols)});"
        )
        drop_lines.append(f"DROP INDEX IF EXISTS {idx_name};")

    (out_dir / f"{label}_create.sql").write_text("\n".join(create_lines) + "\n")
    (out_dir / f"{label}_drop.sql").write_text("\n".join(drop_lines) + "\n")


def run_strategy(
    conn,
    cg_name: str,
    cs_name: str,
    queries: List[Tuple[str, str]],
    schema: Dict,
    label: str,
    cs_kwargs: Optional[Dict] = None,
    iterations: int = 3,
    verbose: bool = True,
    results_dir: Optional[Path] = None,
    indexes_dir: Optional[Path] = None,
    warmup: bool = True,
) -> Dict:
    cs_kwargs = cs_kwargs or {}
    workload_sql = [sql for _, sql in queries]

    if verbose:
        print(f"\n{'='*60}")
        print(f"Strategy: {label}  (CG={cg_name}, CS={cs_name})")
        print(f"  Params: {cs_kwargs}")
        print(f"{'='*60}")

    cg_module = importlib.import_module(
        f"auto_index_selector.CandidateGeneration.{cg_name}"
    )

    if hasattr(cg_module, "generateCandidateIndexes"):
        try:
            candidates = cg_module.generateCandidateIndexes(workload_sql, schema)
        except TypeError:
            candidates = cg_module.generateCandidateIndexes(conn, workload_sql, schema)
    elif hasattr(cg_module, "generateCandidateIndexesWorkload"):
        candidates = cg_module.generateCandidateIndexesWorkload(conn, workload_sql)
    else:
        raise AttributeError(f"{cg_name} has no candidate generation function")

    candidates = _normalise_candidates(candidates)
    n_cands = sum(len(v) for v in candidates.values()) if isinstance(candidates, dict) else len(candidates)
    if verbose:
        print(f"  Candidates generated: {n_cands}")

    cs_module = importlib.import_module(
        f"auto_index_selector.ConfigSelection.{cs_name}"
    )
    config: frozenset = cs_module.selectConfiguration(
        conn, workload_sql, candidates, **cs_kwargs
    )
    if verbose:
        print(f"  Selected {len(config)} index(es):")
        for table, cols in sorted(config):
            print(f"    {table}({', '.join(cols)})")

    save_index_sql(config, label, indexes_dir=indexes_dir)
    create_indexes(conn, config)

    if verbose:
        print(f"  Measuring workload ({iterations} iterations)...")
    results = measure_workload(
        conn, queries, iterations=iterations, warmup=warmup,
    )

    if verbose:
        print(f"  Avg total time: {results['total']:.3f}s")

    qt_path, avg_path = write_csv(results, label, results_dir=results_dir)
    if verbose:
        print(f"  Results saved: {qt_path.name}, {avg_path.name}")

    drop_indexes(conn, config)
    if verbose:
        print(f"  Indexes dropped.")

    return results


def run_baseline(
    conn,
    queries: List[Tuple[str, str]],
    iterations: int = 3,
    verbose: bool = True,
    results_dir: Optional[Path] = None,
    warmup: bool = True,
) -> Dict:
    if verbose:
        print(f"\n{'='*60}")
        print("Strategy: BASELINE (no indexes)")
        print(f"{'='*60}")
        print(f"  Measuring workload ({iterations} iterations)...")

    results = measure_workload(
        conn, queries, iterations=iterations, warmup=warmup,
    )

    if verbose:
        print(f"  Avg total time: {results['total']:.3f}s")

    qt_path, avg_path = write_csv(results, "baseline", results_dir=results_dir)
    if verbose:
        print(f"  Results saved: {qt_path.name}, {avg_path.name}")

    return results
