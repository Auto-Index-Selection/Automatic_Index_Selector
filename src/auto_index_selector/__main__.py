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

# Maps each config.toml section -> the Python package it should import from.
# Adjust these dotted paths if your package/src layout changes.
SECTION_TO_PACKAGE = {
    "candidate_generation": "auto_index_selector.CandidateGeneration",
    "config_selection": "auto_index_selector.ConfigSelection",
    "workload": "auto_index_selector.Workload",
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
    Load config.toml and import the three selected stage modules.
    Returns a dict: {"candidate_generation": module, "config_selection": module, "workload": module}
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
    wl_module = pipeline["workload"]

    print(f"[CandidateGeneration] using module: {cg_module.__name__}")
    print(f"[ConfigSelection]     using module: {cs_module.__name__}")
    print(f"[Workload]            using module: {wl_module.__name__}")

    # --- Wire the pipeline together below ---
    # These calls assume each stage module exposes a conventional entry
    # point (e.g. a function named `run(...)`). Adjust to match your
    # actual module APIs (cg_auto_admin.py, config_sel.py, tpchWorkload.py, etc.)
    #
    # workload = wl_module.load_workload()
    # candidates = cg_module.generate_candidates(workload)
    # selected_config = cs_module.select_config(candidates, workload)
    # print(selected_config)
    
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

    # --- Write penalty estimator setup (optional) ---
    wp_config = cfg.get("write_penalty", {})
    wp_enabled = wp_config.get("enabled", False)
    wp_estimator = None
    snap_before = None

    if wp_enabled:
        import time
        from auto_index_selector.CostEstimator.write_penalty_estimator import WritePenaltyEstimator
        mode = wp_config.get("mode", "simulate")
        write_scale = float(wp_config.get("write_scale", 1.0))
        wp_estimator = WritePenaltyEstimator(conn, write_scale=write_scale)
        wp_estimator.ensure_extension()
        snap_before = wp_estimator.snapshot()
        print(f"[WritePenalty] Mode='{mode}', before-snapshot captured (scale={write_scale})")

        # In SIMULATE mode: run synthetic DML workload replay for demo/offline with timeout
        if mode == "simulate":
            from auto_index_selector.Workload.dml_runner import DMLWorkloadRunner
            sim_timeout = int(wp_config.get("simulation_timeout", 30))
            if sim_timeout > 0:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SET statement_timeout = {sim_timeout * 1000};")
                    print(f"[Simulation] Set PostgreSQL statement_timeout = {sim_timeout}s")
                except Exception as e:
                    pass

            dml_runner = DMLWorkloadRunner(
                conn=conn,
                dml_dir=wp_config.get("dml_dir", "workload/sql/dml"),
                mode=wp_config.get("simulation_mode", "random"),
                rounds=int(wp_config.get("simulation_rounds", 50)),
                seed=int(wp_config.get("simulation_seed", 42)),
                statement_timeout_ms=sim_timeout * 1000 if sim_timeout > 0 else 0,
            )
            rounds = wp_config.get("simulation_rounds", 50)
            print(f"[WritePenalty] Simulating {rounds} DML operations...")
            dml_runner.run()

            # Reset timeout back to default after simulation completes
            if sim_timeout > 0:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SET statement_timeout = 0;")
                except Exception:
                    pass

        # In LIVE mode: optionally sleep for observation window
        elif mode == "live":
            duration = int(wp_config.get("window_duration_seconds", 0))
            if duration > 0:
                print(f"[WritePenalty] Monitoring live database for {duration}s...")
                time.sleep(duration)

    # workload
    import inspect
    sig = inspect.signature(wl_module.getWorkload)
    if 'conn' in sig.parameters:
        W, schema = wl_module.getWorkload(conn=conn)
    else:
        W, schema = wl_module.getWorkload()
    print("Loaded Workload...........")

    # candidate generation
    candidateIndexes = cg_module.generateCandidateIndexes(W, schema)
    print(candidateIndexes)
    print("Candidate Indexex Generated.........")

    # --- Write penalty computation (if enabled) ---
    write_penalties = {}
    if wp_enabled and wp_estimator and snap_before:
        snap_after = wp_estimator.snapshot()
        delta = wp_estimator.compute_delta(snap_before, snap_after)
        write_penalties = wp_estimator.estimate_penalties(candidateIndexes, delta)
        print(f"[WritePenalty] Computed penalties for {len(write_penalties)} candidate indexes")
        for idx_key, penalty in sorted(write_penalties.items(), key=lambda x: -x[1]):
            if penalty > 0:
                print(f"  {idx_key[0]}.({','.join(idx_key[1])}): penalty = {penalty:.4f}")

    # config selection
    selected = cs_module.greedyMK(conn, W, candidateIndexes, m=2, k=10, write_penalties=write_penalties)
    print(selected)


    # todo
    # generate :  create_index.sql, delete_index.sql

    if TEST:
        pass

if __name__ == "__main__":
    sys.exit(main())