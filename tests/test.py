
import sys
from tests.test_strategy import test_strategy
import importlib
import psycopg2
from dotenv import load_dotenv
import os
from pyprojroot import here
from tests.plot_results import (
    collect_qt_files,
    collect_avg_files,
    plot_query_bars,
    plot_total_time,
    plot_index_size,
    plot_strategy_comparison,
    PLOTS_DIR,
    RESULTS_DIR,
)
CG = [
    #  'cg_dta', 
     'cg_rule_based', 
     'cg_extend'
     ]
CS = [
     'cs_extend', 
'cs_greedy',
 'cs_drop'
 ]
W = [
        # 'tpchWorkload',
        'tpcdsWorkload',
        # 'tpccWorkload',
        # 'jobWorkload',
    ]

def test():
    for w_name in W :
            wl_module = importlib.import_module(f"auto_index_selector.Workload.{w_name}")
            w, DB_NAME, schema = wl_module.getWorkload()
            load_dotenv()
            print(f"Workload loaded: {w_name}")
            conn = psycopg2.connect(
                dbname=DB_NAME,
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT")
            )
            print(f"Connected to database: {DB_NAME}")
            for cg in CG:
                cg_module = importlib.import_module(f"auto_index_selector.CandidateGeneration.{cg}")
                candidate_indexes = cg_module.generateCandidateIndexes(conn, w, schema)
                for cs in CS:
                    if( (cs=='cs_extend' and cg!='cg_extend') or (cs!='cs_extend' and cg=='cg_extend') ):
                        continue
                    print(f"Running test_strategy with cg={cg}, cs={cs}, w_name={w_name}")
                    test_strategy(conn, cg, cs, w_name, w, candidate_indexes, db_name=DB_NAME)

def plot():
    """
    Re-generate all plots for every (workload, cg, cs) combination
    defined in W / CG / CS above.
    """
    qt_groups = collect_qt_files(RESULTS_DIR)
    avg_data  = collect_avg_files(RESULTS_DIR)

    for w_name in W:
        for cg in CG:
            for cs in CS:
                key = (w_name, cg, cs)
                qt_filtered  = {k: v for k, v in qt_groups.items() if k == key}
                avg_filtered = {k: v for k, v in avg_data.items()  if k == key}
                if not qt_filtered and not avg_filtered:
                    print(f"  [skip] no data for {key}")
                    continue
                print(f"  Plotting {w_name} / {cg} / {cs} …")
                plot_query_bars(qt_filtered,  PLOTS_DIR, show=False, workload_filter=w_name)
                plot_total_time(avg_filtered, PLOTS_DIR, show=False, workload_filter=w_name)
                plot_index_size(avg_filtered, PLOTS_DIR, show=False, workload_filter=w_name)

        # Strategy comparison once per workload (all strategies combined)
        avg_all_strats = {k: v for k, v in avg_data.items() if k[0] == w_name}
        plot_strategy_comparison(avg_all_strats, PLOTS_DIR, show=False, workload_filter=w_name)

    print(f"Plots saved to {PLOTS_DIR}")

def main():
    test()
    plot()    

if __name__ == '__main__':
    sys.exit(main())
