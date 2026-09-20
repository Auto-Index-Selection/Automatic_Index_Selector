"""
main.py

Entry point for auto_index_selector. Reads `config.toml` and dynamically
imports the module selected for each pluggable stage:
    - CandidateGeneration
    - ConfigSelection
    - Workload (query_logger-based capture with pluggable runners)

Add new implementations by dropping a .py file into the matching
package (with a corresponding __init__.py already present) and
pointing config.toml at its module name (no ".py" extension).
"""

import copy
import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import psycopg2
from dotenv import load_dotenv
import tomllib

from auto_index_selector.Workload.queryLoggerWorkload import (
    detect_log_path,
    extract_workload,
    flush_and_wait,
    reset_stats,
    setup_query_logger,
    take_snapshot as ql_take_snapshot,
    truncate_log,
)
from auto_index_selector.CostEstimator.costEstimator import clearHypotheticalIndexes

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
        return float(value)
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


@dataclass
class WorkloadObservation:
    """
    What a single observation window measured.

    Produced by `observe_workload`, consumed by `select_indexes`. Splitting the
    pipeline this way lets a caller observe ONCE and then evaluate many
    (CandidateGeneration, ConfigSelection) combinations against the *identical*
    workload -- so differences between them are attributable to the algorithms
    rather than to workload sampling noise.
    """
    W: list = field(default_factory=list)
    schema: dict = field(default_factory=dict)
    query_weights: dict = field(default_factory=dict)
    write_penalties: Optional[Callable] = None
    write_delta: Optional[dict] = None

    def __bool__(self) -> bool:
        return bool(self.W)


def _merge_config(config_override: Optional[dict] = None) -> dict:
    """Load config.toml and overlay any caller-supplied overrides."""
    cfg = load_config()
    if config_override:
        for k, v in config_override.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def _connect_from_env(cfg: Optional[dict] = None):
    """Open a connection from config [database] or .env settings."""
    load_dotenv()

    db_cfg = cfg.get("database", {}) if cfg else {}

    dbname = db_cfg.get("dbname") or os.getenv("DB_NAME", "postgres")
    user = db_cfg.get("user") or os.getenv("DB_USER", "postgres")
    password = db_cfg.get("password") or os.getenv("DB_PASSWORD", "")
    host = db_cfg.get("host") or os.getenv("DB_HOST", "localhost")
    port = str(db_cfg.get("port") or os.getenv("DB_PORT", "5432"))

    return psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port,
    )


def take_snapshot(conn):
    """Capture a snapshot of the workload capture state."""
    log_file = detect_log_path(conn)
    return ql_take_snapshot(conn, log_path=log_file)


def get_delta_workload(
    conn,
    snap_before,
    snap_after,
) -> Tuple[List[Any], Dict[str, Dict[str, str]], Dict[str, float]]:
    """Extract workload queries and weights captured between snapshots."""
    start_offset = getattr(snap_before, "file_offset", 0) if snap_before else 0
    db_name = (
        conn.info.dbname
        if hasattr(conn, "info") and conn.info.dbname
        else os.getenv("DB_NAME", "tpch_db")
    )
    log_file = detect_log_path(conn)
    workload = extract_workload(log_file, db_name, conn, start_offset=start_offset)
    return workload.queries, workload.schema, workload.query_weights


def observe_workload(
    conn,
    cfg: dict,
    observation_hook: Optional[Callable[[], None]] = None,
    verbose: bool = True,
) -> WorkloadObservation:
    """
    Capture the workload across an observation window using query_logger and advisor_write_stats.

    1. Enable query_logger for target database, truncate log, reset stats.
    2. Capture before-snapshots for writes and reads.
    3. Run the observation window (custom hook, live timer, or simulated runner).
    4. Capture after-snapshots for writes and reads.
    5. Extract clean workload with actual parameters and compute write penalty deltas.
    """
    workload_cfg = cfg.get("workload", {})
    wp_config = cfg.get("write_penalty", {})
    wp_enabled = wp_config.get("enabled", False)

    # Resolve database name and log file destination
    db_name = (
        conn.info.dbname
        if hasattr(conn, "info") and conn.info.dbname
        else os.getenv("DB_NAME", "tpch_db")
    )
    log_file = detect_log_path(conn, workload_cfg.get("log_file"))

    # --- Setup query_logger extension & truncate log ---
    if conn:
        try:
            setup_query_logger(conn, db_name)
            truncated = truncate_log(log_file, conn=conn)
            reset_stats(conn)
            if verbose:
                if truncated:
                    print(f"[Workload:query_logger] Enabled for {db_name}, log truncated to 0 bytes: {log_file}")
                else:
                    print(f"[Workload:query_logger] Enabled for {db_name}, log not truncated (using offset tracking): {log_file}")
        except Exception as e:
            if verbose:
                print(f"[Workload:query_logger] Warning during setup: {e}")

    # --- 1. Write penalty before-snapshot ---
    wp_estimator = None
    snap_before_writes = None

    if wp_enabled and conn:
        from auto_index_selector.CostEstimator.write_penalty_estimator import WritePenaltyEstimator
        write_scale = float(wp_config.get("write_scale", 1.0))
        wp_estimator = WritePenaltyEstimator(conn, write_scale=write_scale)
        try:
            wp_estimator.ensure_extension()
            snap_before_writes = wp_estimator.snapshot()
        except Exception as e:
            if conn:
                conn.rollback()
            clearHypotheticalIndexes(conn)
            print(f"\n[WritePenalty] FATAL ERROR: advisor_write_stats extension is unavailable: {e}")
            print("[WritePenalty] Cleaned up state. Halting pipeline.")
            raise SystemExit(1)
        if verbose:
            print(f"[WritePenalty] Captured write before-snapshot (scale={write_scale})")

    # --- 2. Read workload before-snapshot ---
    try:
        snap_before_reads = take_snapshot(conn)
    except Exception as e:
        if conn:
            conn.rollback()
        clearHypotheticalIndexes(conn)
        print(f"[Workload] FATAL ERROR: Failed to capture read before-snapshot: {e}")
        print("[Workload] Cleaned up state. Halting pipeline.")
        raise SystemExit(1)

    # --- 3. Observation execution ---
    mode = workload_cfg.get("mode", "timer")
    timer_duration = int(
        workload_cfg.get("timer_seconds", wp_config.get("window_duration_seconds", 60))
    )

    if observation_hook is not None:
        if verbose:
            print("[Observer] Running workload inline for the observation window...")
        observation_hook()
    elif mode in ("timer", "live"):
        if timer_duration > 0:
            import time
            if verbose:
                print(f"[Observer] Monitoring database for {timer_duration}s observation window...")
            time.sleep(timer_duration)
    elif mode == "custom":
        from auto_index_selector.workload_runner import WorkloadRunner
        if verbose:
            print("[Observer] Executing workload_runner...")
        runner = WorkloadRunner(config_dict=workload_cfg)
        runner.run(conn)
    else:
        raise ValueError(
            f"Unknown workload mode: '{mode}'. "
            f"Supported options: 'timer' (or 'live') and 'custom'."
        )

    # Allow query_logger background worker to flush the final batch
    if conn is not None and observation_hook is None:
        flush_and_wait(0.3)

    # --- 4. Read & write after-snapshots ---
    try:
        snap_after_reads = take_snapshot(conn)
    except Exception as e:
        if conn:
            conn.rollback()
        clearHypotheticalIndexes(conn)
        print(f"[Workload] FATAL ERROR: Failed to capture read after-snapshot: {e}")
        print("[Workload] Cleaned up state. Halting pipeline.")
        raise SystemExit(1)

    snap_after_writes = None
    if wp_enabled and wp_estimator and snap_before_writes:
        try:
            snap_after_writes = wp_estimator.snapshot()
        except Exception as e:
            if conn:
                conn.rollback()
            clearHypotheticalIndexes(conn)
            print(f"\n[WritePenalty] FATAL ERROR: Failed to capture write stats after-snapshot: {e}")
            print("[WritePenalty] Cleaned up state. Halting pipeline.")
            raise SystemExit(1)

    # --- 5. Extract workload deltas ---
    W, schema, query_weights = get_delta_workload(conn, snap_before_reads, snap_after_reads)

    if not W:
        if conn:
            conn.rollback()
        clearHypotheticalIndexes(conn)
        source = (
            "the observation hook"
            if observation_hook
            else f"the {mode} runner ({timer_duration}s window)"
        )
        print(f"\n[Workload] FATAL ERROR: 0 queries were observed during {source}.")
        print("Cannot select indexes without observed query traffic. Halting pipeline.")
        raise SystemExit(1)

    if verbose:
        param_count = sum(1 for q in W if getattr(q, "params", None))
        print(f"Loaded Workload: {len(W)} active queries loaded "
              f"({param_count} parameterized with real values).")
        print("\n--- [Workload] Parsed & Resolved Active Queries ---")
        for i, q in enumerate(W, 1):
            calls = int(query_weights.get(q, 1.0))
            params_desc = f" ({len(q.params)} params)" if getattr(q, "params", None) else ""
            print(f"  [{i}] (calls={calls}{params_desc}): {q}")

    # --- 6. Compute analytical write penalties ---
    write_penalties = None
    write_delta = None
    if wp_enabled and wp_estimator and snap_before_writes and snap_after_writes:
        write_delta = wp_estimator.compute_delta(snap_before_writes, snap_after_writes)
        write_penalties = wp_estimator.get_penalty_function(write_delta)
        total_dml = sum(
            d.delta_inserts + d.delta_updates + d.delta_deletes
            for d in write_delta.values()
        )
        if verbose:
            print(f"\n[WritePenalty] Initialized dynamic penalty evaluator across "
                  f"{len(write_delta)} tables ({total_dml} total DML modifications).")

    return WorkloadObservation(
        W=W,
        schema=schema,
        query_weights=query_weights,
        write_penalties=write_penalties,
        write_delta=write_delta,
    )


def select_indexes(
    conn,
    cfg: dict,
    observation: WorkloadObservation,
    verbose: bool = True,
):
    """
    Choose an index configuration C* for an already-observed workload.

    Pure with respect to the database's contents: it reads `observation` and the
    configured CandidateGeneration/ConfigSelection modules, so the same
    observation can be replayed against many algorithm combinations.
    """
    if not observation:
        return set()

    cg_module = import_selected_module("candidate_generation", cfg)
    cs_module = import_selected_module("config_selection", cfg)
    if verbose:
        print(f"[CandidateGeneration] using module: {cg_module.__name__}")
        print(f"[ConfigSelection]     using module: {cs_module.__name__}")

    W = list(observation.W)
    schema = copy.deepcopy(observation.schema) if observation.schema else {}
    query_weights = dict(observation.query_weights)

    raw_cands = cg_module.generateCandidateIndexes(W, schema)
    candidateIndexes = _normalise_candidates(raw_cands)
    total_candidates = (sum(len(v) for v in candidateIndexes.values())
                        if isinstance(candidateIndexes, dict) else len(candidateIndexes))
    if verbose:
        print(f"Candidate Indexes Generated: {total_candidates} candidates across tables.")

    cs_config = cfg.get("config_selection", {})
    cs_kwargs = {
        "write_penalties": observation.write_penalties,
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
        conn, W, candidateIndexes, **cs_kwargs
    )

    if verbose:
        wp = observation.write_penalties
        print("\nSelected Index Configuration:")
        for table, cols in selected:
            pen = wp(table, tuple(cols)) if wp and callable(wp) else 0.0
            print(f"  CREATE INDEX ON {table}({', '.join(cols)});  [write_penalty = {pen:.4f}]")

    return selected


def run_auto_index_selector(
    conn=None,
    config_override: dict = None,
    verbose: bool = True,
    observation_hook: Optional[Callable[[], None]] = None,
):
    """
    Observe the workload and select indexes in one call.

    A thin wrapper over `observe_workload` + `select_indexes`, kept for callers
    that want the whole pipeline in a single step.
    """
    cfg = _merge_config(config_override)

    close_conn_on_exit = False
    if conn is None:
        conn = _connect_from_env(cfg)
        close_conn_on_exit = True
        if verbose:
            print("Connection established successfully!")

    try:
        observation = observe_workload(conn, cfg, observation_hook, verbose)
        if not observation:
            return set(), [], {}, None
        selected = select_indexes(conn, cfg, observation, verbose)
        return (selected, observation.W, observation.query_weights,
                observation.write_penalties)
    finally:
        if close_conn_on_exit and conn:
            conn.close()


def main():
    run_auto_index_selector()
    return 0


if __name__ == "__main__":
    sys.exit(main())
