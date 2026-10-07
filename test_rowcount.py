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
    for filename in os.listdir("workload/queries_tpcc_write"):
        if not filename.endswith(".sql"): continue
        with open(os.path.join("workload/queries_tpcc_write", filename)) as f:
            sql = f.read().strip()
        sql = '\n'.join(line for line in sql.split('\n') if not line.startswith('--'))
        cur.execute(sql)
        print(f"{filename}: {cur.rowcount}")
        conn.commit()
