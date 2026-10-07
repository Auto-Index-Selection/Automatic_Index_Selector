#!/usr/bin/env python3
"""
scripts/run_safe_simulation.py
------------------------------
Automates Option 2: 
1. Clones the target database to a pristine scratch database.
2. Runs the Automatic Index Selector on the clone (which opens the observation window).
3. Concurrently runs the simulated write workload on the clone, committing real writes.
4. Drops the clone when finished, leaving your original database untouched.
"""

import os
import sys
import time
import subprocess
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from dotenv import load_dotenv

def main():
    load_dotenv()
    db_name = os.getenv("DB_NAME")
    if not db_name:
        print("Error: DB_NAME not set in .env")
        sys.exit(1)
        
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")

    clone_db_name = f"{db_name}_clone"

    print(f"Connecting to default 'postgres' database to clone '{db_name}'...")
    try:
        conn = psycopg2.connect(dbname="postgres", user=user, password=password, host=host, port=port)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    except Exception as e:
        print(f"Error connecting to postgres: {e}")
        sys.exit(1)
        
    with conn.cursor() as cur:
        # Terminate existing connections to the original DB to allow cloning
        cur.execute(f"SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = '{db_name}' AND pid <> pg_backend_pid();")
        
        cur.execute(f"DROP DATABASE IF EXISTS {clone_db_name}")
        print(f"Cloning {db_name} to {clone_db_name} (this might take a moment)...")
        cur.execute(f"CREATE DATABASE {clone_db_name} WITH TEMPLATE {db_name}")
        
    conn.close()

    print("\n--- Starting pipeline on clone database ---")
    
    # We must run subsequent processes pointing to the clone
    env = os.environ.copy()
    env["DB_NAME"] = clone_db_name
    env["PYTHONPATH"] = "src"

    # Start auto_index_selector in the background
    selector_proc = subprocess.Popen(
        ["python", "-m", "auto_index_selector"], 
        env=env
    )

    # Wait for the selector to establish connection and take the before-snapshot
    # The selector prints a few lines and then sleeps for window_duration_seconds
    print("Waiting 5 seconds for auto_index_selector to take its before-snapshot...")
    time.sleep(5)

    print("\n--- Starting continuous simulated workload for 5 minutes ---")
    # Run the simulator in a loop to keep traffic flowing for the entire window
    # We use all three workload directories (Write, Read, Balance)
    while selector_proc.poll() is None:
        procs = []
        
        # 1. TPCC Write (DML only)
        procs.append(subprocess.Popen(
            ["python", "scripts/simulate_workload.py", 
             "--dml-dir", "workload/queries_tpcc_write",
             "--no-reads", "--rounds", "20"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        
        # 2. TPCC Read (Reads only)
        procs.append(subprocess.Popen(
            ["python", "scripts/simulate_workload.py", 
             "--reads-dir", "workload/queries_tpcc_read",
             "--rounds", "0"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        
        # 3. TPCC Balance (Both)
        procs.append(subprocess.Popen(
            ["python", "scripts/simulate_workload.py", 
             "--dml-dir", "workload/queries_tpcc_balance",
             "--reads-dir", "workload/queries_tpcc_balance",
             "--rounds", "20"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        
        for p in procs:
            p.wait()
            
    print("Observation window completed! auto_index_selector is now generating candidates and processing...")

    print("\n--- Cleaning up ---")
    print(f"Dropping clone database '{clone_db_name}'...")
    conn = psycopg2.connect(dbname="postgres", user=user, password=password, host=host, port=port)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        # Terminate any lingering connections to the clone
        cur.execute(f"SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = '{clone_db_name}' AND pid <> pg_backend_pid();")
        cur.execute(f"DROP DATABASE {clone_db_name}")
    conn.close()
    
    print("Done! Original database is completely pristine.")

if __name__ == "__main__":
    main()
