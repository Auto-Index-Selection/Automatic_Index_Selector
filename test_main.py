import importlib
from pathlib import Path
import os
import sys

def main():
    print("Testing importing tpccWorkloadWrite")
    sys.path.append('src')
    wl_module = importlib.import_module("auto_index_selector.Workload.tpccWorkloadWrite")
    res = wl_module.getWorkload()
    print("Length of res:", len(res))
    if isinstance(res[1], str):
        print("Second item is string:", res[1])
        print("Queries len:", len(res[0]))
        print("Schema tables:", list(res[2].keys()))

main()
