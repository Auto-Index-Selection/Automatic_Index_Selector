
import sys
from .test_strategy import test_strategy
import importlib
import psycopg2
from dotenv import load_dotenv
import os

CG = ['cg_auto_admin', 'cg_dta', 'cg_rule_based', 'cg_extend']
CS = ['cs_extend', 'cs_greedy', 'cs_drop']
W = [
        'tpchWorkload',
        'tpcdsWorkload',
        # 'tpccWorkload',
        'jobWorkload',
    ]

def test():
    for w_name in W :
            wl_module = importlib.import_module(f"auto_index_selector.Workload.{w_name}")
            w, DB_NAME, schema = wl_module.getWorkload()
            load_dotenv()
            
            conn = psycopg2.connect(
                dbname=DB_NAME,
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT")
            )
            for cg in CG:
                cg_module = importlib.import_module(f"auto_index_selector.CadidateGeneration.{cg}")
                candidate_indexes = cg_module.generateCandidateIndexes(conn, w, schema)
                for cs in CS:
                    if( (cs=='cs_extend' and cg!='cg_extend') or (cs!='cs_extend' and cg=='cg_extend') ):
                        continue
                    test_strategy(cg, cs, w_name, w, candidate_indexes)

def plot():
    pass

def main():
    test()
    plot()    

if __name__ == '__main__':
    sys.exit(main)