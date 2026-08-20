from pathlib import Path
from pyprojroot import here

tpcds_schema = {
    
}
def getWorkload():
    workloadPath = str(here() /"workload"  /  "queries_tpcds")
    # print(workloadPath)
    queries = []
    workloadPath = Path(workloadPath)
    for sql_file in sorted(workloadPath.glob("*.sql")):

        with open(sql_file, "r") as f:
            query = f.read().strip()

        if query:
            queries.append(query)

    return queries, tpcds_schema
