import psycopg2

conn = psycopg2.connect("dbname=tpcc user=akash host=localhost")
conn.autocommit = True

query = "-- just a comment"
explain_query = f"EXPLAIN (FORMAT JSON) {query}"

with conn.cursor() as cur:
    try:
        cur.execute(explain_query)
        result = cur.fetchone()
        print("Result:", result)
    except Exception as e:
        print("Exception:", type(e), e)
