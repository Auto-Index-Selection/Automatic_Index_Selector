import psycopg2
import time
import os
from dotenv import load_dotenv

load_dotenv()
dbname = os.getenv("DB_NAME")
user = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")
host = os.getenv("DB_HOST", "localhost")
port = os.getenv("DB_PORT", "5432")

conn = psycopg2.connect(dbname=dbname, user=user, password=password, host=host, port=port)
with conn.cursor() as cur:
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("Before pg_stat:", cur.fetchone())
    cur.execute("SELECT sum(update_query_count) FROM advisor_get_column_set_stats()")
    print("Before advisor:", cur.fetchone())

os.system("PYTHONPATH=src venv/bin/python scripts/simulate_workload.py --dml-dir workload/queries_tpcc_write --no-reads --rounds 50")

print("Sleeping for 10 seconds...")
time.sleep(10)

with conn.cursor() as cur:
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("After pg_stat:", cur.fetchone())
    cur.execute("SELECT sum(update_query_count) FROM advisor_get_column_set_stats()")
    print("After advisor:", cur.fetchone())

