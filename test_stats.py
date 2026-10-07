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

# clone the db first
conn = psycopg2.connect(dbname="postgres", user=user, password=password, host=host, port=port)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid();")
    cur.execute(f"DROP DATABASE IF EXISTS tpcc_clone_test;")
    cur.execute(f"CREATE DATABASE tpcc_clone_test WITH TEMPLATE {dbname};")
conn.close()

conn = psycopg2.connect(dbname="tpcc_clone_test", user=user, password=password, host=host, port=port)
with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats;")
    conn.commit()
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("Before pg_stat:", cur.fetchone())
    cur.execute("SELECT count(*) FROM advisor_get_column_set_stats()")
    print("Before advisor:", cur.fetchone())

# run a script to write
os.system("PYTHONPATH=src DB_NAME=tpcc_clone_test venv/bin/python scripts/simulate_workload.py --dml-dir workload/queries_tpcc_write --no-reads --rounds 5")

time.sleep(1)

with conn.cursor() as cur:
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("After pg_stat:", cur.fetchone())
    cur.execute("SELECT count(*) FROM advisor_get_column_set_stats()")
    print("After advisor:", cur.fetchone())

conn.close()
conn = psycopg2.connect(dbname="postgres", user=user, password=password, host=host, port=port)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("DROP DATABASE tpcc_clone_test;")
