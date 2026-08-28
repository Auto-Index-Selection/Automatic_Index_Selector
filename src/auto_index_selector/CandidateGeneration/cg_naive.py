#######################
# Candidate Selection #
#######################
from typing import List
from .dexter import runDexter



def generateCandidateIndexesWorkload(
    conn,
    workload: List[str] = None
):
    """
    Generate naive candidate indexes (combinations of single and pair columns across schema).
    """
    from .dexter import generateCandidateIndexes
    return generateCandidateIndexes(conn, max_width=2)

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