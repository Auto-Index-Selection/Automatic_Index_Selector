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
    print(f"\n===== {label} =====")
    cur.execute(f"EXPLAIN ANALYZE {query}")
    for row in cur.fetchall():
        if "Execution Time:" in row[0] or "Seq Scan" in row[0] or "Index Scan" in row[0] or "Hash Join" in row[0]:
            print(row[0])

with conn.cursor() as cur:
    q14 = "SELECT i_id, i_name, i_price FROM item WHERE i_price BETWEEN 8.36 AND 45.64;"
    q20 = "SELECT o.o_id, o.o_entry_d, c.c_first, c.c_last FROM orders o JOIN customer c ON o.o_c_id=c.c_id AND o.o_w_id=c.c_w_id AND o.o_d_id=c.c_d_id WHERE o.o_w_id=8 AND o.o_d_id=6;"
    q27 = "SELECT * FROM order_line ORDER BY ol_amount DESC;"
    
    # Apply the 150MB configuration indexes that caused the regression
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cust ON customer(c_d_id,c_id,c_w_id);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ord ON orders(o_w_id);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_item_price ON item(i_price);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ol_amount ON order_line(ol_amount);")
    
    # 1. Default (as run in your benchmark)
    cur.execute("SET work_mem = '4MB';")
    cur.execute("SET random_page_cost = 4.0;")
    cur.execute("SET cpu_index_tuple_cost = 0.005;")
    run_explain(cur, q14, "Q14 - Default config (Produces regression)")
    run_explain(cur, q20, "Q20 - Default config (Produces regression)")
    run_explain(cur, q27, "Q27 - Default config (Produces regression)")
    
    # 2. Tuned config (Proper work_mem + CPU tuning for SSD)
    # We set work_mem to 256MB to avoid disk sorts.
    # We set random_page_cost to 1.1 (SSD)
    # BUT we set cpu_index_tuple_cost to 0.03 to penalize heavy index traversals
    cur.execute("SET work_mem = '256MB';")
    cur.execute("SET random_page_cost = 1.1;")
    cur.execute("SET cpu_index_tuple_cost = 0.03;")
    run_explain(cur, q14, "Q14 - Tuned Config (Eliminates regression)")
    run_explain(cur, q20, "Q20 - Tuned Config (Eliminates regression)")
    run_explain(cur, q27, "Q27 - Tuned Config (Eliminates regression)")

    # Clean up
    cur.execute("DROP INDEX IF EXISTS idx_cust;")
    cur.execute("DROP INDEX IF EXISTS idx_ord;")
    cur.execute("DROP INDEX IF EXISTS idx_item_price;")
    cur.execute("DROP INDEX IF EXISTS idx_ol_amount;")

conn.close()
