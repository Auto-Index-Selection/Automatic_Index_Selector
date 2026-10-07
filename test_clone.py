import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()
dbname = os.getenv("DB_NAME")
user = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")
host = os.getenv("DB_HOST")
port = os.getenv("DB_PORT")

# connect to postgres db to clone
conn = psycopg2.connect(dbname='postgres', user=user, password=password, host=host, port=port)
conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {dbname}_clone")
        cur.execute(f"CREATE DATABASE {dbname}_clone WITH TEMPLATE {dbname}")
    print("Cloned successfully")
except Exception as e:
    print(f"Error cloning: {e}")
