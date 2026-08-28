"""
tests/tpch/strategy_runner.py
Runs a single (CG, CS, params) combination:
  1. Generate candidates
  2. Select configuration
  3. CREATE indexes in PostgreSQL
  4. Measure TPC-H workload (actual wall-clock time)
  5. Write CSV results
  6. DROP indexes in PostgreSQL
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .measure import measure_workload, write_csv, discard_session

INDEXES_DIR = Path(__file__).parent / "indexes"


# ---------------------------------------------------------------------------
# Candidate format normalisation
# ---------------------------------------------------------------------------

def _normalise_candidates(candidates: Dict) -> Dict:
    """
    Ensure candidates are in the format {table: [(col,), (col1, col2), ...]}
    (tuples of column names).

    Some CG modules return {table: ["col1", "col2"]} (flat strings from
    cg_auto_admin's listToDict) or {table: [["col1", "col2"]]} (lists).
    This function converts all of those to the standard tuple format.
    """
    if not isinstance(candidates, dict):
        return candidates

    normalised: Dict = {}
    for table, index_list in candidates.items():
        norm_list = []
        for idx in index_list:
            if isinstance(idx, str):
                # "col1" → ("col1",)
                norm_list.append((idx,))
            elif isinstance(idx, (list, tuple)):
                # ["col1", "col2"] or ("col1", "col2") → ("col1", "col2")
                norm_list.append(tuple(str(c) for c in idx))
            else:
                norm_list.append((str(idx),))
        normalised[table] = norm_list
    return normalised


# ---------------------------------------------------------------------------
# Index SQL helpers
# ---------------------------------------------------------------------------

def build_index_name(table: str, cols: Tuple[str, ...]) -> str:
    return f"ais_test_{table}_{'_'.join(cols)}"


def create_indexes(conn, config: frozenset) -> None:
    """CREATE all indexes in the given config."""
    if not config:
        return
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
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
    """DROP all indexes that were created for this config and reset session."""
    if not config:
        return
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
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
    """Save CREATE and DROP SQL to indexes/<label>_{create,drop}.sql."""
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


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_strategy(
    conn,
    cg_name: str,
    cs_name: str,
    queries: List[Tuple[str, str]],       # [(label, sql), ...]
    schema: Dict,
    label: str,                            # unique result label, e.g. "config_sel_k5"
    cs_kwargs: Optional[Dict] = None,
    iterations: int = 3,
    statement_timeout_ms: Optional[int] = 30_000,
    skip_timed_out: bool = True,
    verbose: bool = True,
    results_dir: Optional[Path] = None,
    indexes_dir: Optional[Path] = None,
    warmup: bool = True,
) -> Dict:
    """
    Full lifecycle for one (CG, CS, params) combination.

    Returns the measurement dict from measure_workload().
    """
    cs_kwargs = cs_kwargs or {}
    workload_sql = [sql for _, sql in queries]

    if verbose:
        print(f"\n{'='*60}")
        print(f"Strategy: {label}  (CG={cg_name}, CS={cs_name})")
        print(f"  Params: {cs_kwargs}")
        print(f"{'='*60}")

    # ---- 1. Generate candidates ----------------------------------------
    cg_module = importlib.import_module(
        f"auto_index_selector.CandidateGeneration.{cg_name}"
    )

    # Dispatch: different CG modules have different function names/signatures
    if hasattr(cg_module, "generateCandidateIndexes"):
        try:
            candidates = cg_module.generateCandidateIndexes(workload_sql, schema)
        except TypeError:
            # cg_auto_admin accepts (conn, W, schema)
            candidates = cg_module.generateCandidateIndexes(conn, workload_sql, schema)
    elif hasattr(cg_module, "generateCandidateIndexesWorkload"):
        # cg_naive uses this name and needs (conn, workload)
        candidates = cg_module.generateCandidateIndexesWorkload(conn, workload_sql)
    else:
        raise AttributeError(
            f"{cg_name} has no generateCandidateIndexes or "
            "generateCandidateIndexesWorkload function"
        )

    # Normalise candidate format: some modules return {table: ["col"]}
    # (flat strings); the cost estimator expects {table: [("col",), ("col1","col2")]}
    candidates = _normalise_candidates(candidates)

    n_cands = sum(len(v) for v in candidates.values()) if isinstance(candidates, dict) else len(candidates)
    if verbose:
        print(f"  Candidates generated: {n_cands}")

    # ---- 2. Select configuration ----------------------------------------
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

    # ---- 3. Save SQL files & CREATE indexes -----------------------------
    save_index_sql(config, label, indexes_dir=indexes_dir)
    create_indexes(conn, config)

    # ---- 4. Measure workload -------------------------------------------
    if verbose:
        print(f"  Measuring workload ({iterations} iterations)...")
    results = measure_workload(
        conn, queries, iterations=iterations,
        statement_timeout_ms=statement_timeout_ms,
        skip_timed_out=skip_timed_out,
        warmup=warmup,
    )

    if verbose:
        print(f"  Avg total time: {results['total']:.3f}s")

    # ---- 5. Write CSV --------------------------------------------------
    qt_path, avg_path = write_csv(results, label, results_dir=results_dir)
    if verbose:
        print(f"  Results saved: {qt_path.name}, {avg_path.name}")

    # ---- 6. DROP indexes -----------------------------------------------
    drop_indexes(conn, config)
    if verbose:
        print(f"  Indexes dropped.")

    return results


def run_baseline(
    conn,
    queries: List[Tuple[str, str]],
    iterations: int = 3,
    statement_timeout_ms: Optional[int] = 30_000,
    skip_timed_out: bool = True,
    verbose: bool = True,
    results_dir: Optional[Path] = None,
    warmup: bool = True,
) -> Dict:
    """Measure the workload with NO indexes at all (baseline)."""
    if verbose:
        print(f"\n{'='*60}")
        print("Strategy: BASELINE (no indexes)")
        print(f"{'='*60}")
        print(f"  Measuring workload ({iterations} iterations)...")

    results = measure_workload(
        conn, queries, iterations=iterations,
        statement_timeout_ms=statement_timeout_ms,
        skip_timed_out=skip_timed_out,
        warmup=warmup,
    )

    if verbose:
        print(f"  Avg total time: {results['total']:.3f}s")

    qt_path, avg_path = write_csv(results, "baseline", results_dir=results_dir)
    if verbose:
        print(f"  Results saved: {qt_path.name}, {avg_path.name}")

    return results
