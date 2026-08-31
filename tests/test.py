
import sys
from tests.test_strategy import test_strategy
import importlib
import psycopg2
from dotenv import load_dotenv
import os
from pyprojroot import here
print("Current working directory:", here())
CG = [
    #  'cg_dta', 
    #  'cg_rule_based', 
     'cg_extend'
     ]
CS = [
     'cs_extend', 
# 'cs_greedy',
#  'cs_drop'
 ]
W = [
        # 'tpchWorkload',
        # 'tpcdsWorkload',
        # 'tpccWorkload',
        'jobWorkload',
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
                    # if( (cs=='cs_extend' and cg!='cg_extend') or (cs!='cs_extend' and cg=='cg_extend') ):
                    #     continue
                    print(f"Running test_strategy with cg={cg}, cs={cs}, w_name={w_name}")
                    test_strategy(conn, cg, cs, w_name, w, candidate_indexes, db_name=DB_NAME)

def plot():
    pass

def main():
    test()
    plot()    

if __name__ == '__main__':
    sys.exit(main())
