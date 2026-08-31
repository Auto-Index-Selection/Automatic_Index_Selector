"""
main.py

Entry point for auto_index_selector. Reads `config.toml` and dynamically
imports the module selected for each pluggable stage:
    - CandidateGeneration
    - ConfigSelection
    - Workload

Add new implementations by dropping a .py file into the matching
package (with a corresponding __init__.py already present) and
pointing config.toml at its module name (no ".py" extension).
"""

import sys
import importlib
from pathlib import Path
from typing import Optional
import os
import psycopg2
from dotenv import load_dotenv

import tomllib

from auto_index_selector.Workload.pgStatStatementsWorkload import take_snapshot, get_delta_workload

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.toml"

SECTION_TO_PACKAGE = {
    "candidate_generation": "auto_index_selector.CandidateGeneration",
    "config_selection":     "auto_index_selector.ConfigSelection",
}


def load_config(config_path: Optional[Path] = None) -> dict:
    """Load and parse config.toml directly from the project root or specified path."""
    target = Path(config_path) if config_path else CONFIG_PATH
    if not target.exists():
        raise FileNotFoundError(f"Config file not found at: {target}")
    with open(target, "rb") as f:
        return tomllib.load(f)


def import_selected_module(section: str, config: dict):
    """
    Given a config.toml section name (e.g. 'candidate_generation'),
    dynamically import and return the module selected under
    config[section]["module"].
    """
    if section not in config:
        raise KeyError(f"Missing '[{section}]' section in config.toml")
    if section not in SECTION_TO_PACKAGE:
        raise KeyError(f"Unknown config section: {section}")

    module_name = config[section].get("module")
    if not module_name:
        raise KeyError(f"'[{section}]' section is missing a 'module' key")

    package = SECTION_TO_PACKAGE[section]
    full_module_path = f"{package}.{module_name}"

    try:
        module = importlib.import_module(full_module_path)
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            f"Could not import '{full_module_path}'. Check that "
            f"'{module_name}.py' exists in '{package.replace('.', '/')}/' "
            f"and that the value in config.toml is correct."
        ) from e

    return module


def load_pipeline(config_path: Optional[Path] = None):
    """
    Load config.toml and import the algorithmic stage modules (CandidateGeneration, ConfigSelection).
    Returns a dict: {"candidate_generation": module, "config_selection": module}
    """
    config = load_config(config_path)

    pipeline = {}
    for section in SECTION_TO_PACKAGE:
        pipeline[section] = import_selected_module(section, config)

    return pipeline


TEST = False


def _as_storage_budget(value) -> float:
    """
    Coerce a storage budget into a real float (bytes).

    config.toml spells the unconstrained budget as the *string* "inf". The
    selection modules test the budget against float("inf") to detect "no
    limit", and a string never compares equal to a float, so an uncoerced
    "inf" makes them take the constrained branch and silently discard the
    caller's budget. Missing or unparseable values mean "no limit".
    """
    if value is None:
        return float("inf")
    try:
        return float(value)  # float() already parses "inf" and numeric strings
    except (TypeError, ValueError):
        return float("inf")


def _normalise_candidates(candidates):
    """Ensure candidate dictionary format is {table: [('col1',), ('col1', 'col2')]}."""
    if not isinstance(candidates, dict):
        return candidates
    normalised = {}
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


def run_auto_index_selector(
    conn=None,
    config_override: dict = None,
    verbose: bool = True,
):
    """
    Runs the full end-to-end index selection pipeline:
    1. Snapshot before (Reads & Writes)
    2. Observation window (Sleep / wait for traffic)
    3. Snapshot after & compute deltas
    4. Candidate generation
    5. Write penalty evaluation
    6. Configuration selection

    Returns
    -------
    tuple : (selected_config, W, query_weights, write_penalties)
    """
    cfg = load_config()
    if config_override:
        for k, v in config_override.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v

    cg_module = import_selected_module("candidate_generation", cfg)
    cs_module = import_selected_module("config_selection", cfg)

    if verbose:
        print(f"[CandidateGeneration] using module: {cg_module.__name__}")
        print(f"[ConfigSelection]     using module: {cs_module.__name__}")
        # print(f"[Workload]            using pg_stat_statements (live delta workload)")

    # connection setup
    close_conn_on_exit = False
    if conn is None:
        load_dotenv()
        conn = psycopg2.connect(
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT")
        )
        close_conn_on_exit = True
        if verbose:
            print("Connection established successfully!")

    try:
        # --- 1. Snapshot Before Window (Writes & Reads) ---
        wp_config = cfg.get("write_penalty", {})
        wp_enabled = wp_config.get("enabled", False)
        wp_estimator = None
        snap_before_writes = None

        if wp_enabled:
            from auto_index_selector.CostEstimator.write_penalty_estimator import WritePenaltyEstimator
            write_scale = float(wp_config.get("write_scale", 1.0))
            wp_estimator = WritePenaltyEstimator(conn, write_scale=write_scale)
            try:
                wp_estimator.ensure_extension()
                snap_before_writes = wp_estimator.snapshot()
            except Exception as e:
                print(f"[WritePenalty]  Error: Failed to capture write stats before-snapshot: {e}")
                return set(), [], {}, None

            if verbose:
                print(f"[WritePenalty] Captured write before-snapshot (scale={write_scale})")

        try:
            snap_before_reads = take_snapshot(conn)
        except Exception as e:
            print(f"[Workload]  Error: Failed to capture pg_stat_statements before-snapshot: {e}")
            return set(), [], {}, None

        if verbose:
            print(f"[Workload] Captured pg_stat_statements before-snapshot ({len(snap_before_reads.entries)} queries tracked)")

        # --- 2. Observation Window (Wait for background application/simulator traffic) ---
        duration = int(wp_config.get("window_duration_seconds", 0))
        if duration > 0:
            import time
            if verbose:
                print(f"[Observer] Monitoring database for {duration}s observation window...")
            time.sleep(duration)

        # --- Capture both after-snapshots immediately when observation window ends ---
        try:
            snap_after_reads = take_snapshot(conn)
        except Exception as e:
            print(f"[Workload] Error: Failed to capture pg_stat_statements after-snapshot: {e}")
            return set(), [], {}, None

        snap_after_writes = None
        if wp_enabled and wp_estimator and snap_before_writes:
            try:
                snap_after_writes = wp_estimator.snapshot()
            except Exception as e:
                print(f"[WritePenalty] Error: Failed to capture write stats after-snapshot: {e}")
                return set(), [], {}, None

        # --- 3. Extract Read & Write Workload Deltas Immediately ---
        W, schema, query_weights = get_delta_workload(conn, snap_before_reads, snap_after_reads)
        if not W:
            if duration == 0:
                print("[Workload] Error: Observation window duration is 0s and no active queries were observed between snapshots.")
                print("           Cannot select indexes without observed query traffic. Exiting.")
            else:
                print(f"[Workload] Error: 0 queries were observed during the {duration}s observation window.")
                print("           Cannot select indexes without observed query traffic. Exiting.")
            return set(), [], {}, None

        if verbose:
            print(f"Loaded Workload: {len(W)} active queries loaded (weighted by pg_stat_statements call counts).")
            print("\n--- [Workload] Parsed & Resolved Active Queries ---")
            for i, q in enumerate(W, 1):
                calls = int(query_weights.get(q, 1.0))
                print(f"  [{i}] (calls={calls}): {q}")

        write_penalties = None
        if wp_enabled and wp_estimator and snap_before_writes and snap_after_writes:
            write_delta = wp_estimator.compute_delta(snap_before_writes, snap_after_writes)
            write_penalties = wp_estimator.get_penalty_function(write_delta)
            total_dml = sum(d.delta_inserts + d.delta_updates + d.delta_deletes for d in write_delta.values())
            if verbose:
                print(f"\n[WritePenalty] Initialized dynamic penalty evaluator across {len(write_delta)} tables ({total_dml} total DML modifications).")

        # --- 4. Candidate Generation ---
        raw_cands = cg_module.generateCandidateIndexes(W, schema)
        candidateIndexes = _normalise_candidates(raw_cands)
        total_candidates = sum(len(v) for v in candidateIndexes.values()) if isinstance(candidateIndexes, dict) else len(candidateIndexes)
        if verbose:
            print(f"Candidate Indexes Generated: {total_candidates} candidates across tables.")

        # --- Log Candidate Write Penalties ---
        # if write_penalties and candidateIndexes and verbose:
        #     print("\n--- [Write Penalty] Candidate Indexes & Calculated Penalties ---")
        #     all_candidates = []
        #     if isinstance(candidateIndexes, dict):
        #         for t, col_lists in candidateIndexes.items():
        #             for cols in col_lists:
        #                 all_candidates.append((t, tuple(cols)))
        #     elif isinstance(candidateIndexes, (list, set, frozenset)):
        #         for item in candidateIndexes:
        #             if isinstance(item, tuple) and len(item) == 2:
        #                 t, cols = item
        #                 all_candidates.append((t, tuple(cols) if isinstance(cols, (list, tuple)) else (cols,)))

        #     for table, cols in sorted(all_candidates):
        #         pen = write_penalties(table, cols) if callable(write_penalties) else write_penalties.get((table, cols), 0.0)
        #         print(f"  {table}({', '.join(cols)}): write_penalty = {pen:.4f}")

        # --- 5. Configuration Selection ---
        # cs_config is already config.toml merged with config_override (see the
        # top of this function), so it is the single source for the selection
        # kwargs. Forward everything except 'module' first, then normalise the
        # values the selection modules are type-sensitive about. Coercing before
        # the copy would be pointless: the copy would overwrite it with the raw
        # config value.
        cs_config = cfg.get("config_selection", {})

        cs_kwargs = {
            "write_penalties": write_penalties,
            "query_weights": query_weights,
        }
        for k, v in cs_config.items():
            if k not in ["module"]:
                cs_kwargs[k] = v

        m_val = int(cs_kwargs.get("m", 2))
        cs_kwargs["m"] = m_val
        cs_kwargs["k"] = int(cs_kwargs.get("k", 10))
        cs_kwargs["storage_budget"] = _as_storage_budget(cs_kwargs.get("storage_budget"))
        cs_kwargs.setdefault("max_group", m_val)

        selected = cs_module.selectConfiguration(
            conn, W, candidateIndexes,
            **cs_kwargs
        )

        if verbose:
            print("\nSelected Index Configuration:")
            for table, cols in selected:
                pen = write_penalties(table, tuple(cols)) if write_penalties and callable(write_penalties) else 0.0
                print(f"  CREATE INDEX ON {table}({', '.join(cols)});  [write_penalty = {pen:.4f}]")

        return selected, W, query_weights, write_penalties
    finally:
        if close_conn_on_exit and conn:
            conn.close()


def main():
    run_auto_index_selector()
    return 0


if __name__ == "__main__":
    sys.exit(main())
