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
    cur.execute(f"EXPLAIN ANALYZE {query}")
    for row in cur.fetchall():
        print(row[0])

with conn.cursor() as cur:
    q14 = "SELECT i_id, i_name, i_price FROM item WHERE i_price BETWEEN 8.36 AND 45.64;"
    
    # 1. Baseline Q14 (Seq Scan)
    run_explain(cur, q14, "Q14 Baseline (No Index)")
    
    # 2. With Index Q14
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_item_price ON item(i_price);")
    run_explain(cur, q14, "Q14 With Index (item(i_price))")
    cur.execute("DROP INDEX IF EXISTS idx_item_price;")
    
    q27 = "SELECT * FROM order_line ORDER BY ol_amount DESC;"
    
    # 3. Baseline Q27 (Seq Scan + Sort)
    # Adding LIMIT 1000 so it doesn't take forever to output, but the planner issues remain the same.
    # Actually let's just do the exact query to be authentic. 
    # But wait, EXPLAIN ANALYZE on a full table sort might take 7s.
    run_explain(cur, q27, "Q27 Baseline (No Index)")
    
    # 4. With Index Q27
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ol_amount ON order_line(ol_amount);")
    run_explain(cur, q27, "Q27 With Index (order_line(ol_amount))")
    cur.execute("DROP INDEX IF EXISTS idx_ol_amount;")

conn.close()
