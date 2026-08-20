import duckdb
import os

# Connect and activate the extension
con = duckdb.connect()
con.execute("INSTALL tpch; LOAD tpch;")

# Create a folder to hold your queries
os.makedirs("tpch_queries_psql", exist_ok=True)

# Fetch query numbers and syntax 
queries = con.execute("SELECT query_nr, query FROM tpch_queries();").fetchall()

# Loop and write individual files
for num, sql_text in queries:
    file_path = f"tpch_queries_psql/q{num}.sql"
    with open(file_path, "w") as f:
        # Strip trailing syntax quirks if needed, and finalize with a semi-colon
        f.write(sql_text.strip() + ";\n")

print("All 22 queries exported successfully to the 'tpch_queries_psql' folder!")

