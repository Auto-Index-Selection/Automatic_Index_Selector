import psycopg2
from pathlib import Path

conn = psycopg2.connect("dbname=tpcc user=akash host=localhost")
conn.autocommit = True

workloadPath = Path("/home/akash/repos/write_also/Automatic_Index_Selector/workload/queries_tpcc_write")
for sql_file in sorted(workloadPath.glob("*.sql")):
    with open(sql_file, "r") as f:
        query = f.read().strip()
    
    if query:
        explain_query = f"EXPLAIN (FORMAT JSON) {query}"
        try:
            with conn.cursor() as cur:
                cur.execute(explain_query)
                result = cur.fetchone()
                if not result:
                    print(f"{sql_file.name}: No result")
        except Exception as e:
            print(f"{sql_file.name}: {type(e).__name__} - {e}")
            conn.rollback()
