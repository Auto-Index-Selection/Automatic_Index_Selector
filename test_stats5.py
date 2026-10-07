import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()
dbname = os.getenv("DB_NAME")
user = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")
host = os.getenv("DB_HOST", "localhost")
port = os.getenv("DB_PORT", "5432")

conn = psycopg2.connect(dbname="tpcc_clone_test", user=user, password=password, host=host, port=port)
with conn.cursor() as cur:
    cur.execute("SELECT relname, n_tup_upd FROM pg_stat_user_tables;")
    print("All tables:")
    for row in cur.fetchall():
        print(row)
