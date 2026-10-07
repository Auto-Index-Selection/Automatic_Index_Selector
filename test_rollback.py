import psycopg2
import time
from dotenv import load_dotenv
import os

load_dotenv()
conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT")
)

def get_stats(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT n_tup_ins FROM pg_stat_user_tables WHERE relname = 'test_table'")
        row = cur.fetchone()
        return row[0] if row else 0

with conn.cursor() as cur:
    cur.execute("CREATE TABLE IF NOT EXISTS test_table (id serial, val int)")
conn.commit()

time.sleep(1) # let stats collector catch up
before = get_stats(conn)
print(f"Before: {before}")

try:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO test_table(val) VALUES (1), (2), (3)")
        # deliberately rollback
        conn.rollback()
except Exception as e:
    conn.rollback()

print("Rolled back.")
time.sleep(1) # wait for stats collector

after = get_stats(conn)
print(f"After: {after}")

with conn.cursor() as cur:
    cur.execute("INSERT INTO test_table(val) VALUES (1), (2), (3)")
conn.commit()
print("Committed.")
time.sleep(1) # wait for stats collector

after_commit = get_stats(conn)
print(f"After Commit: {after_commit}")

