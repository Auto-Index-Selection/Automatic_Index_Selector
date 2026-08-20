import psycopg2
from ilp_imp.candidateSelection.candidateSelection import *
from ilp_imp.candidateSelection.ruleBasedCS import *
from ilp_imp.configGeneration import *
from ilp_imp.costEstimator import *
import os
from ilp_imp.workload import *
from pyprojroot import here
import os
from dotenv import load_dotenv

load_dotenv()
workloadPath = str(here() / "workload" / "queries_tpch")

W = getWorkload(workloadPath=workloadPath)[0:3]

tpch_schema = {
    "region": {
        "r_regionkey": "INT",
        "r_name": "VARCHAR",
        "r_comment": "VARCHAR"
    },
    "nation": {
        "n_nationkey": "INT",
        "n_name": "VARCHAR",
        "n_regionkey": "INT",
        "n_comment": "VARCHAR"
    },
    "part": {
        "p_partkey": "INT",
        "p_name": "VARCHAR",
        "p_mfgr": "VARCHAR",
        "p_brand": "VARCHAR",
        "p_type": "VARCHAR",
        "p_size": "INT",
        "p_container": "VARCHAR",
        "p_retailprice": "DOUBLE",
        "p_comment": "VARCHAR"
    },
    "supplier": {
        "s_suppkey": "INT",
        "s_name": "VARCHAR",
        "s_address": "VARCHAR",
        "s_nationkey": "INT",
        "s_phone": "VARCHAR",
        "s_acctbal": "DOUBLE",
        "s_comment": "VARCHAR"
    },
    "partsupp": {
        "ps_partkey": "INT",
        "ps_suppkey": "INT",
        "ps_availqty": "INT",
        "ps_supplycost": "DOUBLE",
        "ps_comment": "VARCHAR"
    },
    "customer": {
        "c_custkey": "INT",
        "c_name": "VARCHAR",
        "c_address": "VARCHAR",
        "c_nationkey": "INT",
        "c_phone": "VARCHAR",
        "c_acctbal": "DOUBLE",
        "c_mktsegment": "VARCHAR",
        "c_comment": "VARCHAR"
    },
    "orders": {
        "o_orderkey": "INT",
        "o_custkey": "INT",
        "o_orderstatus": "VARCHAR",
        "o_totalprice": "DOUBLE",
        "o_orderdate": "DATE",
        "o_orderpriority": "VARCHAR",
        "o_clerk": "VARCHAR",
        "o_shippriority": "INT",
        "o_comment": "VARCHAR"
    },
    "lineitem": {
        "l_orderkey": "INT",
        "l_partkey": "INT",
        "l_suppkey": "INT",
        "l_linenumber": "INT",
        "l_quantity": "DOUBLE",
        "l_extendedprice": "DOUBLE",
        "l_discount": "DOUBLE",
        "l_tax": "DOUBLE",
        "l_returnflag": "VARCHAR",
        "l_linestatus": "VARCHAR",
        "l_shipdate": "DATE",
        "l_commitdate": "DATE",
        "l_receiptdate": "DATE",
        "l_shipinstruct": "VARCHAR",
        "l_shipmode": "VARCHAR",
        "l_comment": "VARCHAR"
    }
}

if True:
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    print("Connection established successfully!")

    # test 
    # print(1)
    candidateIndexes = generateCandidateIndexes(W, tpch_schema)
    # print(2)
    # print(candidateIndexes)
    configs = generateConfigurations(candidate_indexes=candidateIndexes, workload=W)
    # print(3)
    indexSet = numericIndex(candidateIndexes)
    # print((indexSet))
    # print(4)
    configSet = numericConfig(configurations=configs, indexSet=indexSet)

    storage = storageEstimate(conn, indexSet)
    print("="*80)
    cost_init, cost_final = estimateWorkloadCost(conn, W, configs, indexSet)
    print("="*80)
    
    print(cost_init)
    print(cost_final)
    print(storage)
    conn.close()
    
# except Exception as error:
#     print(f"Error connecting to the database: {error}")