import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    dbname='tpcc',
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
)
conn.autocommit = True

def run_explain(cur, query, label):
    print(f"\n--- {label} ---")
    cur.execute(f"EXPLAIN {query}")
    for row in cur.fetchall():
        print(row[0])

with conn.cursor() as cur:
    q27 = "SELECT * FROM order_line ORDER BY ol_amount DESC;"
    
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ol_amount ON order_line(ol_amount);")
    
    # 1. Default (4MB work_mem, random_page_cost = 4.0)
    cur.execute("SET work_mem = '4MB';")
    cur.execute("SET random_page_cost = 4.0;")
    run_explain(cur, q27, "Default: work_mem=4MB, random_page_cost=4.0")
    
    # 2. Increase work_mem
    cur.execute("SET work_mem = '256MB';")
    cur.execute("SET random_page_cost = 4.0;")
    run_explain(cur, q27, "Tuned: work_mem=256MB, random_page_cost=4.0")
    
    # 3. Increase work_mem and SSD random_page_cost
    cur.execute("SET work_mem = '256MB';")
    cur.execute("SET random_page_cost = 1.1;")
    run_explain(cur, q27, "Tuned: work_mem=256MB, random_page_cost=1.1")

    # 4. HDD random_page_cost
    cur.execute("SET work_mem = '256MB';")
    cur.execute("SET random_page_cost = 6.0;")
    run_explain(cur, q27, "Tuned: work_mem=256MB, random_page_cost=6.0")

    cur.execute("DROP INDEX IF EXISTS idx_ol_amount;")

conn.close()
