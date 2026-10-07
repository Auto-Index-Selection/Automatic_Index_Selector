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
    q20 = "SELECT o.o_id, o.o_entry_d, c.c_first, c.c_last FROM orders o JOIN customer c ON o.o_c_id=c.c_id AND o.o_w_id=c.c_w_id AND o.o_d_id=c.c_d_id WHERE o.o_w_id=8 AND o.o_d_id=6;"
    
    # Baseline Q20
    run_explain(cur, q20, "Q20 Baseline (No Extra Indexes)")
    
    # With Indexes Q20
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cust ON customer(c_d_id,c_id,c_w_id);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ord ON orders(o_w_id);")
    run_explain(cur, q20, "Q20 With Indexes")
    cur.execute("DROP INDEX IF EXISTS idx_cust;")
    cur.execute("DROP INDEX IF EXISTS idx_ord;")

conn.close()
