#######################
# Candidate Selection #
#######################
from typing import List
from auto_index_selector.CandidateGeneration.dexter import runDexter



def generateCandidateIndexesWorkload(
    conn,
    workload: List[str]
):
    """
    Generate candidate indexes using Dexter.
    """

    candidates = runDexter(conn, workload)

    return candidates

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