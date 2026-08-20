from auto_index_selector.CandidateGeneration.ruleBasedCS import *
# import psycopg2
from auto_index_selector.workload import *

from auto_index_selector.workload import getWorkload
from pyprojroot import here
import os
from dotenv import load_dotenv

load_dotenv()
workloadPath = str(here() / "workload" / "queries_tpch")

W = getWorkload(workloadPath=workloadPath)

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
# conn = psycopg2.connect(
#         dbname=os.getenv("DB_NAME"),
#         user=os.getenv("DB_USER"),
#         password=os.getenv("DB_PASSWORD"),
#         host=os.getenv("DB_HOST"),
#         port=os.getenv("DB_PORT")
#     )
# print("Connection established successfully!")
# print(W[4])
# result = getJoinCols(W[4], tpch_schema)
# print(result)
# # print(W[1])
# print(getEqCols(W[1], tpch_schema))

# print(getRangeCols(W[1], tpch_schema))
# print(getOCols(W[1], tpch_schema))
print(generateCandidateIndexes(W, tpch_schema))

# applyRule3(set(), '', tpch_schema)