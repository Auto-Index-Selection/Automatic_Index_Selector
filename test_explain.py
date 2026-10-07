import psycopg2

conn = psycopg2.connect("dbname=tpcc user=akash host=localhost")
conn.autocommit = True

query = """-- type: write
UPDATE stock SET s_quantity = s_quantity - 5, s_ytd = s_ytd + 5 WHERE s_w_id=1 AND s_i_id=97197;"""
explain_query = f"EXPLAIN (FORMAT JSON) {query}"

print(repr(explain_query))

with conn.cursor() as cur:
    try:
        cur.execute(explain_query)
        result = cur.fetchone()
        print("Result:", result)
    except Exception as e:
        print("Exception:", type(e), e)
