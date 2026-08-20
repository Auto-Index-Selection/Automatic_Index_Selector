from pathlib import Path
from pyprojroot import here

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
def getWorkload():
    workloadPath = str(here() /"workload"  /  "queries_tpch")
    print(workloadPath)
    queries = []
    workloadPath = Path(workloadPath)
    for sql_file in sorted(workloadPath.glob("*.sql")):

        with open(sql_file, "r") as f:
            query = f.read().strip()

        if query:
            queries.append(query)
        # print(sql_file)
    # print(queries)
    return queries, tpch_schema
