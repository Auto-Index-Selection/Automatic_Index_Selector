#######################
# Candidate Selection #
#######################
from typing import List
from .dexter import runDexter



from itertools import combinations

def generateCandidateIndexes(W=None, schema=None, max_width: int = 2, conn=None):
    """
    Generate naive candidate indexes (combinations of single and pair columns across schema).
    Supports (W, schema) or (conn).
    """
    # If called as generateCandidateIndexes(conn)
    if hasattr(W, "cursor"):
        conn = W
        schema = None

    if schema:
        candidates = {}
        for table, cols in schema.items():
            if table.startswith("_") or table.startswith("hypopg") or table.startswith("pg_stat"):
                continue
            col_names = list(cols.keys()) if isinstance(cols, dict) else list(cols)
            indexes = []
            for size in range(1, min(max_width, len(col_names)) + 1):
                indexes.extend([list(c) for c in combinations(col_names, size)])
            candidates[table] = indexes
        return candidates
    elif conn:
        from .dexter import generateCandidateIndexes as gen_cand
        return gen_cand(conn, max_width=max_width)
    return {}


def generateCandidateIndexesWorkload(
    conn,
    workload: List[str] = None
):
    return generateCandidateIndexes(conn=conn, max_width=2)

def numericIndex(candidates):
    """
    Returns : dict(index : [number, table])
    """
    i = 1
    indexSet = dict()
    for (key, value) in candidates.items():
        for index in value:
            
            indexSet['['+','.join(index)+']'] =  [i, key]
            i+=1
    return indexSet