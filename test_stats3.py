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

# connect to clone
conn = psycopg2.connect(dbname="tpcc_clone_test", user=user, password=password, host=host, port=port)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("Before:", cur.fetchone())
    
    # execute a known update
    cur.execute("UPDATE stock SET s_quantity = s_quantity - 5 WHERE s_w_id=1 AND s_i_id=97197;")
    print("Rows updated manually:", cur.rowcount)
    
    print("Sleeping 2 seconds for stats collector...")
    time.sleep(2)
    
    cur.execute("SELECT sum(n_tup_upd), sum(n_tup_ins) FROM pg_stat_user_tables;")
    print("After:", cur.fetchone())
    
conn.close()
