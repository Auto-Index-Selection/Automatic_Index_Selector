import os
import psycopg2
import time
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

queries = {
    "q14": "SELECT i_id, i_name, i_price FROM item WHERE i_price BETWEEN 8.36 AND 45.64;",
    "q20": "SELECT o.o_id, o.o_entry_d, c.c_first, c.c_last FROM orders o JOIN customer c ON o.o_c_id=c.c_id AND o.o_w_id=c.c_w_id AND o.o_d_id=c.c_d_id WHERE o.o_w_id=8 AND o.o_d_id=6;",
    "q27": "SELECT * FROM order_line ORDER BY ol_amount DESC;"
}

with conn.cursor() as cur:
    # Set up the exact 150MB indexes
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cust ON customer(c_d_id,c_id,c_w_id);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ord ON orders(o_w_id);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_item_price ON item(i_price);")
    cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ol_amount ON order_line(ol_amount);")

    # Flush plans like in test_strategy.py
    cur.execute("DISCARD PLANS;")

    print("Running queries...")
    for name, q in queries.items():
        start = time.perf_counter()
        cur.execute(q)
        if cur.description is not None:
            cur.fetchall()
        elapsed = time.perf_counter() - start
        print(f"{name}: {elapsed:.3f}s")
        
    cur.execute("DROP INDEX IF EXISTS idx_cust;")
    cur.execute("DROP INDEX IF EXISTS idx_ord;")
    cur.execute("DROP INDEX IF EXISTS idx_item_price;")
    cur.execute("DROP INDEX IF EXISTS idx_ol_amount;")
    
conn.close()
