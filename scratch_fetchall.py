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

with conn.cursor() as cur:
    q27 = "SELECT * FROM order_line ORDER BY ol_amount DESC;"
    
    start = time.perf_counter()
    cur.execute(q27)
    execute_time = time.perf_counter() - start
    
    start = time.perf_counter()
    cur.fetchall()
    fetch_time = time.perf_counter() - start
    
    print(f"Q27 (3M rows) - Execute: {execute_time:.3f}s, Fetchall: {fetch_time:.3f}s, Total: {execute_time + fetch_time:.3f}s")
    
conn.close()
