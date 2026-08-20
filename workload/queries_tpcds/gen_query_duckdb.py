import duckdb
import os

# Connect and activate the extension
con = duckdb.connect()
con.execute("INSTALL tpcds; LOAD tpcds;")

# Fetch query numbers and syntax 
queries = con.execute("SELECT query_nr, query FROM tpcds_queries();").fetchall()

# Loop and write individual files
for num, sql_text in queries:
    file_path = f"q{num}.sql"
    with open(file_path, "w") as f:
        # Strip trailing syntax quirks if needed, and finalize with a semi-colon
        f.write(sql_text.strip() + ";\n")

print(f"All {len(queries)} queries exported successfully!!!")

