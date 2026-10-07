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
    cur.execute("SHOW track_counts;")
    print(cur.fetchone())
    cur.execute("SHOW track_activities;")
    print(cur.fetchone())
