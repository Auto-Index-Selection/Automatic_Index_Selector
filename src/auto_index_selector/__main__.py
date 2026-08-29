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
try:
    from pyprojroot import here
except ImportError:
    def here() -> Path:
        return Path(__file__).resolve().parent.parent.parent.parent

import psycopg2
from dotenv import load_dotenv
import os

# tomllib is stdlib from Python 3.11+; fall back to the tomli backport
# for older interpreters.
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from auto_index_selector.Workload.pgStatStatementsWorkload import (
    take_snapshot,
    get_delta_workload,
)

# Maps each config.toml section -> the Python package it should import from.
SECTION_TO_PACKAGE = {
    "candidate_generation": "auto_index_selector.CandidateGeneration",
    "config_selection": "auto_index_selector.ConfigSelection",
}

def _find_config_path() -> Path:
    candidates = [
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent.parent.parent / "config.toml",
        Path(str(here() / "Automatic_Index_Selector" / "config.toml")),
        Path(str(here() / "config.toml")),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]

DEFAULT_CONFIG_PATH = _find_config_path()

def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load and parse config.toml."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "rb") as f:
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


def load_pipeline(config_path: Path = DEFAULT_CONFIG_PATH):
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

def main():
    cfg = load_config()
    pipeline = load_pipeline()

    cg_module = pipeline["candidate_generation"]
    cs_module = pipeline["config_selection"]

    print(f"[CandidateGeneration] using module: {cg_module.__name__}")
    print(f"[ConfigSelection]     using module: {cs_module.__name__}")
    print(f"[Workload]            using pg_stat_statements (live delta workload)")

    # connection setup
    load_dotenv()
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    print("Connection established successfully!")

    # --- 1. Snapshot Before Window (Writes & Reads) ---
    wp_config = cfg.get("write_penalty", {})
    wp_enabled = wp_config.get("enabled", False)
    wp_estimator = None
    snap_before_writes = None

    if wp_enabled:
        from auto_index_selector.CostEstimator.write_penalty_estimator import WritePenaltyEstimator
        write_scale = float(wp_config.get("write_scale", 1.0))
        wp_estimator = WritePenaltyEstimator(conn, write_scale=write_scale)
        wp_estimator.ensure_extension()
        snap_before_writes = wp_estimator.snapshot()
        print(f"[WritePenalty] Captured write before-snapshot (scale={write_scale})")

    snap_before_reads = take_snapshot(conn)
    print(f"[Workload] Captured pg_stat_statements before-snapshot ({len(snap_before_reads.entries)} queries tracked)")

    # --- 2. Observation Window (Wait for background application/simulator traffic) ---
    duration = int(wp_config.get("window_duration_seconds", 0))
    if duration > 0:
        import time
        print(f"[Observer] Monitoring database for {duration}s observation window...")
        time.sleep(duration)

    # --- Capture both after-snapshots immediately when observation window ends ---
    snap_after_reads = take_snapshot(conn)

    snap_after_writes = None
    if wp_enabled and wp_estimator and snap_before_writes:
        snap_after_writes = wp_estimator.snapshot()

    # --- 3. Extract Read Workload (Delta Queries & Execution Weights) ---
    W, schema, query_weights = get_delta_workload(conn, snap_before_reads, snap_after_reads)
    print(f"Loaded Workload: {len(W)} active queries loaded (weighted by pg_stat_statements call counts).")

    # --- 4. Candidate Generation ---
    candidateIndexes = cg_module.generateCandidateIndexes(W, schema)
    total_candidates = sum(len(v) for v in candidateIndexes.values()) if isinstance(candidateIndexes, dict) else len(candidateIndexes)
    print(f"Candidate Indexes Generated: {total_candidates} candidates across tables.")

    # --- 5. Write Penalty Computation (Delta Writes) ---
    write_penalties = {}
    if wp_enabled and wp_estimator and snap_before_writes and snap_after_writes:
        delta = wp_estimator.compute_delta(snap_before_writes, snap_after_writes)
        write_penalties = wp_estimator.estimate_penalties(candidateIndexes, delta)
        active_penalties = {k: v for k, v in write_penalties.items() if v > 0}
        print(f"[WritePenalty] Computed penalties for {len(write_penalties)} candidate indexes ({len(active_penalties)} penalized)")
        for idx_key, penalty in sorted(active_penalties.items(), key=lambda x: -x[1]):
            print(f"  {idx_key[0]}.({','.join(idx_key[1])}): penalty = {penalty:.4f}")

    # --- 6. Configuration Selection ---
    cs_config = cfg.get("config_selection", {})
    m_val = int(cs_config.get("m", 2))
    k_val = int(cs_config.get("k", 10))
    storage_budget = cs_config.get("storage_budget", float("inf"))
    if isinstance(storage_budget, str) and storage_budget.lower() != "inf":
        storage_budget = float(storage_budget)

    if hasattr(cs_module, "selectConfiguration"):
        selected = cs_module.selectConfiguration(
            conn, W, candidateIndexes,
            m=m_val, k=k_val,
            storage_budget=storage_budget,
            max_group=m_val,
            write_penalties=write_penalties,
            query_weights=query_weights
        )
    elif hasattr(cs_module, "greedyMK"):
        selected = cs_module.greedyMK(
            conn, W, candidateIndexes,
            m=m_val, k=k_val,
            write_penalties=write_penalties,
            query_weights=query_weights
        )
    elif hasattr(cs_module, "dropHeuristic"):
        selected = cs_module.dropHeuristic(
            conn, W, candidateIndexes,
            storage_budget=storage_budget,
            max_group=m_val,
            write_penalties=write_penalties,
            query_weights=query_weights
        )
    else:
        raise AttributeError(f"Module {cs_module.__name__} has no supported selection function")

    print("\nSelected Index Configuration:")
    for table, cols in selected:
        print(f"  CREATE INDEX ON {table}({','.join(cols)});")


    # todo
    # generate :  create_index.sql, delete_index.sql

    if TEST:
        pass

if __name__ == "__main__":
    sys.exit(main())