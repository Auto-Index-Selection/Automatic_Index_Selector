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
from pyprojroot import here
import psycopg2
from dotenv import load_dotenv
import os
from auto_index_selector.utility import *

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

# DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
DEFAULT_CONFIG_PATH = Path(str(here() / "config.toml"))


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
    pipeline = load_pipeline()

    cg_module = pipeline["candidate_generation"]
    cs_module = pipeline["config_selection"]
    wl_module = pipeline["workload"]

    # print(f"[CandidateGeneration] using module: {cg_module.__name__}")
    # print(f"[ConfigSelection]     using module: {cs_module.__name__}")
    # print(f"[Workload]            using module: {wl_module.__name__}")

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

    # workload
    # import wl_modul
    W, schema = wl_module.getWorkload()
    # print(W)
    # print(schema)
    print("Loaded Workload...........")

    # candidate generation
    candidateIndexes = cg_module.generateCandidateIndexes(W, schema)
    # print(candidateIndexes)
    print("Candidate Indexex Generated.........")

    # config selection
    config = cs_module.greedyMK(conn, W, candidateIndexes, m=2, k=10)
    # print(config)


    # generate :  create_index.sql, delete_index.sql
    create_path = generate_create_index_sql(config)
    delete_path = generate_delete_index_sql(config)
    print(f"Wrote {create_path}")
    print(f"Wrote {delete_path}")

    if TEST:
        pass

if __name__ == "__main__":
    sys.exit(main())