import sys
import time
from tests.test_strategy import test_strategy
import importlib
import psycopg2
from dotenv import load_dotenv
import os
from pyprojroot import here
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

def tune_connection(conn):
    """Applies hardware-specific query planner parameters to prevent the planner trap."""
    with conn.cursor() as cur:
        cur.execute("SET work_mem = '4MB';")
        # Penalize random fetches so index scans aren't over-selected for bulk queries
        cur.execute("SET random_page_cost = 4.0;") 
        # Penalize CPU overhead of fetching millions of tuples via index
        cur.execute("SET cpu_index_tuple_cost = 0.005;")
    conn.commit() # <--- Closes the transaction started by cur.execute()
    return conn

CG = [
    # "cg_dta", 
    # "cg_rule_based", 
    # "cg_ben_knap",
    "cg_extend", 
]
CS = [
    # "cs_ben_knap",
    "cs_extend",
    # "cs_greedy",
    # "cs_drop",
]
W = [
    # 'tpchWorkload',
    # 'tpcdsWorkload',
    # "tpccWorkloadRead",
    # "tpccWorkloadWrite",
    "tpccWorkloadBalance",
    # 'jobWorkload',
]


def test():
    for w_name in W:
        wl_module = importlib.import_module(f"auto_index_selector.Workload.{w_name}")
        w, DB_NAME, schema = wl_module.getWorkload()
        load_dotenv()
        print(f"Workload loaded: {w_name}")
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn.autocommit = True
        print(f"Connected to database: {DB_NAME}")
        
        # --- Initialize Required Extensions ---
        print("Initializing required extensions...")
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg;")
            cur.execute("LOAD 'advisor_write_stats';")
            cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats;")
        
        # --- Create pristine backup before simulation ---
        # The simulation will permanently modify the database to capture stats.
        # We backup the DB so we can restore it for the actual index testing.
        backup_db = f"{DB_NAME}_backup"
        print(f"Creating a pristine backup of {DB_NAME} to {backup_db}...")
        conn.close()
        
        conn_pg = psycopg2.connect(
            dbname="postgres",
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn_pg.autocommit = True
        with conn_pg.cursor() as cur:
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{DB_NAME}', '{backup_db}') AND pid <> pg_backend_pid();")
            cur.execute(f"DROP DATABASE IF EXISTS {backup_db};")
            cur.execute(f"CREATE DATABASE {backup_db} WITH TEMPLATE {DB_NAME};")
        conn_pg.close()
        
        # Reconnect to DB_NAME to run the simulation
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn.autocommit = True
        conn = tune_connection(conn)
        
        # --- Capture Write Penalty via Simulation ---
        from auto_index_selector.CostEstimator.write_penalty_estimator import WritePenaltyEstimator
        import subprocess
        
        print("\n--- Initializing Write Penalty Simulation ---")
        # Scale 5 minutes of writes to ~8.3 hours of writes (100 * 5 min = 500 min)
        # Formula: write_scale = (Time window of Read Workload) / (Time window of Write snapshot)
        write_scale = 1.0 
        wp_estimator = WritePenaltyEstimator(conn, write_scale=write_scale)
        wp_estimator.ensure_extension()
        snap_before_writes = wp_estimator.snapshot()
        print("[WritePenalty] Captured before-snapshot.")
        
        print(f"Injecting DML traffic for 5 minutes (Write/Balance)...")
        env = os.environ.copy()
        env["DB_NAME"] = DB_NAME
        
        procs = []
        # # TPC-C Write (DML only)
        # procs.append(subprocess.Popen(
        #     [sys.executable, "scripts/simulate_workload.py", 
        #      "--dml-dir", "workload/queries_tpcc_write",
        #      "--no-reads", "--rounds", "10000000"],
        #     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        # ))
        
        # TPC-C Balance (Read/Write)
        procs.append(subprocess.Popen(
            [sys.executable, "scripts/simulate_workload.py", 
             "--dml-dir", "workload/queries_tpcc_balance",
             "--rounds", "10000000"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        
        # TPC-C Read 
        # procs.append(subprocess.Popen(
        #     [sys.executable, "scripts/simulate_workload.py", 
        #      "--dml-dir", "workload/queries_tpcc_read",
        #      "--rounds", "10000000"],
        #     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        # ))

        # Wait for the observation window duration
        time.sleep(300)
        
        for p in procs:
            p.terminate()
            p.wait()
            
        print("Simulation complete! Reconnecting for after-snapshot...")
        
        # The connection might have been dropped after being idle for 5 minutes. 
        # Re-establish it to safely take the snapshot.
        try:
            conn.close()
        except:
            pass
            
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn.autocommit = True
        conn = tune_connection(conn)
        wp_estimator._conn = conn
        
        snap_after_writes = wp_estimator.snapshot()
        write_delta = wp_estimator.compute_delta(snap_before_writes, snap_after_writes)
        write_penalties = wp_estimator.get_penalty_function(write_delta)
        total_dml = sum(d.delta_inserts + d.delta_updates + d.delta_deletes for d in write_delta.values())
        print(f"[WritePenalty] Captured write after-snapshot. {total_dml} total DML modifications.")
        
        # --- RESTORE PRISTINE CLONE AFTER SIMULATION ---
        # The simulation permanently inserted rows to record stats. We must 
        # reset the database from the backup before running test_strategy!
        conn.close()
        
        backup_db = f"{DB_NAME}_backup"
        print(f"Restoring pristine test database from {backup_db}...")
        
        conn_pg = psycopg2.connect(
            dbname="postgres",
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn_pg.autocommit = True
        with conn_pg.cursor() as cur:
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{DB_NAME}' AND pid <> pg_backend_pid();")
            cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME};")
            cur.execute(f"CREATE DATABASE {DB_NAME} WITH TEMPLATE {backup_db};")
        conn_pg.close()
        
        # Reconnect to the fresh test database
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        conn = tune_connection(conn)
        
        # Update wp_estimator to use the new connection
        wp_estimator._conn = conn
        
        # Disable autocommit so test_strategy's transaction rollbacks work!
        conn.autocommit = False
        
        for cg in CG:
            cg_module = importlib.import_module(
                f"auto_index_selector.CandidateGeneration.{cg}"
            )
            cg_start = time.perf_counter()
            candidate_indexes = cg_module.generateCandidateIndexes(conn, w, schema)
            cg_elapsed = time.perf_counter() - cg_start
            print(
                f"[TIMING] Candidate generation ({cg}): {cg_elapsed:.4f}s  "
                f"({len(candidate_indexes)} candidates)"
            )
            for cs in CS:
                if (cs == "cs_extend" and cg != "cg_extend") or (
                    cs != "cs_extend" and cg == "cg_extend"
                ):
                    continue
                if (cs == "cs_ben_knap" and cg == "cg_extend"):
                    continue
                if (cs == "cs_ben_knap" and cg == "cg_dta"):
                    continue
                if (cs in ["cs_drop", "cs_greedy"] and cg == "cg_ben_knap"):
                    continue

                print(f"Running test_strategy with cg={cg}, cs={cs}, w_name={w_name}")
                test_strategy(
                    conn,
                    cg,
                    cs,
                    w_name,
                    w,
                    candidate_indexes,
                    db_name=DB_NAME,
                    cg_elapsed=cg_elapsed,
                    write_penalties=write_penalties,
                )


def plot():
    """
    Re-generate all plots for every (workload, cg, cs) combination
    defined in W / CG / CS above.
    """
    qt_groups = collect_qt_files(RESULTS_DIR)
    avg_data = collect_avg_files(RESULTS_DIR)
    st_data = collect_strategy_time_files(RESULTS_DIR)
    hc_data = collect_hypopg_cost_files(RESULTS_DIR)
    baseline_data = collect_baseline_files(RESULTS_DIR)

    for w_name in W:
        for cg in CG:
            for cs in CS:
                key = (w_name, cg, cs)
                qt_filtered = {k: v for k, v in qt_groups.items() if k == key}
                avg_filtered = {k: v for k, v in avg_data.items() if k == key}
                hc_filtered = {k: v for k, v in hc_data.items() if k == key}
                if not qt_filtered and not avg_filtered:
                    print(f"  [skip] no data for {key}")
                    continue
                print(f"  Plotting {w_name} / {cg} / {cs} …")
                plot_query_bars(
                    qt_filtered,
                    baseline_data,
                    PLOTS_DIR,
                    show=False,
                    workload_filter=w_name,
                )
                plot_total_time(
                    avg_filtered,
                    baseline_data,
                    PLOTS_DIR,
                    show=False,
                    workload_filter=w_name,
                )
                plot_index_size(
                    avg_filtered, PLOTS_DIR, show=False, workload_filter=w_name
                )
                plot_hypopg_cost(
                    hc_filtered, PLOTS_DIR, show=False, workload_filter=w_name
                )

        # Cross-strategy plots once per workload (all strategies combined)
        avg_all_strats = {k: v for k, v in avg_data.items() if k[0] == w_name}
        st_all_strats = {k: v for k, v in st_data.items() if k[0] == w_name}
        hc_all_strats = {k: v for k, v in hc_data.items() if k[0] == w_name}
        plot_strategy_comparison(
            avg_all_strats, baseline_data, PLOTS_DIR, show=False, workload_filter=w_name
        )
        plot_strategy_time(st_all_strats, PLOTS_DIR, show=False, workload_filter=w_name)
        plot_hypopg_cost_comparison(
            hc_all_strats, PLOTS_DIR, show=False, workload_filter=w_name
        )

    print(f"Plots saved to {PLOTS_DIR}")


def main():
    test()
    # plot()


if __name__ == "__main__":
    sys.exit(main())
