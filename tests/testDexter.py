import psycopg2
from ilp_imp.candidateSelection.candidateSelection import *
import os
from dotenv import load_dotenv

load_dotenv()

try:
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    print("Connection established successfully!")

    # test 
    candidateIndexes = generateCandidateIndexes(conn, 1)
    
    indexSet = numericIndex(candidateIndexes)
    print(indexSet)

    conn.close()
    
except Exception as error:
    print(f"Error connecting to the database: {error}")