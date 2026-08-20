from auto_index_selector.workload import getWorkload
from pyprojroot import here

workloadPath = str(here() / "workload" / "queries_tpch")

print(getWorkload(workloadPath=workloadPath))