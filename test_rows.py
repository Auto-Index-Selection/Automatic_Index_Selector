import psycopg2
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
    cur.execute("UPDATE stock SET s_quantity = s_quantity - 5, s_ytd = s_ytd + 5 WHERE s_w_id=1 AND s_i_id=97197;")
    print("Rows updated:", cur.rowcount)
    
    # Try another query
    with open("workload/queries_tpcc_write/q2.sql") as f:
        sql = f.read().strip()
    # It might have a comment, strip it
    sql = '\n'.join(line for line in sql.split('\n') if not line.startswith('--'))
    cur.execute(sql)
    print("q2 Rows updated:", cur.rowcount)

