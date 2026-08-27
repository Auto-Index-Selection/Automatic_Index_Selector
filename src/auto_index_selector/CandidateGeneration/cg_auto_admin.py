from typing import *
import sqlglot
from ..CostEstimator.costEstimator import *
from itertools import combinations

def getTablesIn(query: str) -> List:
    parsedQuery = sqlglot.parse_one(query, read='postgres') # parse specifically postgres query

    tables = [table for table in parsedQuery.find_all(sqlglot.exp.Table)]
    return tables

def singleIndexableColumnsIn(query: str, schema: Dict) -> List:
    indexable_columns = set() # to be returned
    parsedQuery = sqlglot.parse_one(query, read='postgres')  # parses the query

    # get all columns in query
    cols_in_query = []
    for col in parsedQuery.find_all(sqlglot.exp.Column):
        cols_in_query.append(col.name)
    

    for table in parsedQuery.find_all(sqlglot.exp.Table):
        if table.name not in schema.keys():
            continue
        # print(f'{type(table.name)}')
        for col in schema[table.name]:
            if col in cols_in_query:
                indexable_columns.add(f'{table.name}.{col}')
    print('done indexable columns')
    return list(indexable_columns)

def enumerateConfigsHelperExhaustive(conn, I: List, total_columns: int, W: list) -> List: 
    query = W[0]
    max_benefit = 0
    best_conf = []
    i = 0
    while i < pow(2, total_columns):
        configuration = list()
        for bit in range(total_columns):
            if (i & (1<<bit)):
                configuration.append(I[bit])
        # print(configuration)
        cost_init, cost_fin = estimateConfigurationCost(conn, query, configuration)
        # print(f'{cost_fin} {cost_init}')
        benefit = cost_init - cost_fin
        if benefit > max_benefit:
            max_benefit = benefit
            best_conf = configuration
        if benefit == max_benefit and len(configuration) < len(best_conf):
            best_conf = configuration
        i+=1
    
    return best_conf

def enumerateConfigsHelperGreedy(conn, I: List, total_columns: int, W: list) -> List:
    """
    AutoAdmin Greedy(m,k) algorithm for configuration enumeration.
    
    m = 1 (seed size for exhaustive search - small for per-query candidate selection)
    k = total_columns (max indexes to select, unbounded for candidate selection)
    
    Algorithm:
    1. Start with empty set S
    2. Pick index I from remaining that maximizes benefit when added to S
    3. If benefit > 0, add I to S, remove from remaining
    4. Repeat until no more benefit or all indexes selected
    """
    query = W[0]
    
    # Phase 1: Greedy selection (equivalent to Greedy(0,k) - pure greedy)
    # For candidate selection, m=0 (no exhaustive seed), just greedy
    selected = []
    remaining = list(I)  # Copy of candidate indexes
    
    while remaining:
        best_idx = None
        best_benefit = 0
        
        # Evaluate each remaining index by adding it to current selection
        for idx in remaining:
            test_config = selected + [idx]
            cost_init, cost_fin = estimateConfigurationCost(conn, query, test_config)
            benefit = cost_init - cost_fin
            
            # Track the index with maximum benefit
            if benefit > best_benefit:
                best_benefit = benefit
                best_idx = idx
        
        # Stop if no beneficial index found
        if best_idx is None or best_benefit <= 0:
            break
        
        # Add best index to selection
        selected.append(best_idx)
        remaining.remove(best_idx)
    
    return selected

def enumerateConfigsHelperGreedyWithSeed(conn, I: List, total_columns: int, W: list, m: int = 1) -> List:
    """
    Full Greedy(m,k) with exhaustive seed of size m.
    
    For use in final configuration enumeration (not candidate selection).
    m = seed size for exhaustive search
    k = total_columns (max indexes)
    
    1. Find best m-index configuration via exhaustive search
    2. Greedily expand until no more benefit or k reached
    """
    print(f"seed (m) = {m}")
    query = W[0]
    k = total_columns  # Or pass as parameter
    
    # Phase 1: Exhaustive search for best seed of size m
    selected = []
    remaining = list(I)
    
    if m > 0 and len(I) >= m:
        best_seed_cost = float('inf')
        best_seed = []
        
        # Try all combinations of size m
        for combo in combinations(range(len(I)), m):
            seed = [I[i] for i in combo]
            cost_init, cost_fin = estimateConfigurationCost(conn, query, seed)
            total_cost = cost_fin  # We want to minimize cost
            
            if total_cost < best_seed_cost:
                best_seed_cost = total_cost
                best_seed = seed
        
        selected = best_seed
        remaining = [idx for idx in I if idx not in selected]
    
    # Phase 2: Greedy expansion
    while len(selected) < k and remaining:
        best_idx = None
        best_benefit = 0
        
        for idx in remaining:
            test_config = selected + [idx]
            cost_init, cost_fin = estimateConfigurationCost(conn, query, test_config)
            benefit = cost_init - cost_fin
            
            if benefit > best_benefit:
                best_benefit = benefit
                best_idx = idx
        
        if best_idx is None or best_benefit <= 0:
            break
        
        selected.append(best_idx)
        remaining.remove(best_idx)
    
    return selected

def enumerateConfigs(conn, I: List, W: List) -> List :
    total_columns = len(I)
    print(total_columns)
    bestIndexes = enumerateConfigsHelperGreedy(conn, I, total_columns, W)
    print("done enumerate configs")
    return bestIndexes
    

def listToDict(candidateIndexes: List) -> Dict:
    result = dict()
    for index in candidateIndexes:
        table = index.split('.')[0]
        column = index.split('.')[1]
        if table not in result.keys():
            result[table] = []
        result[table].append(column)

    return result
    pass

def bestConf(conn, W :List, schema: Dict) -> Dict:
    '''
    Input : 
        W -> workload as a List
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: candidates}
    '''
    candidateIndexes = ['partsupp.ps_suppkey', 'supplier.s_nationkey', 'orders.o_custkey', 'lineitem.l_shipdate', 'supplier.s_suppkey', 'part.p_size', 'part.p_container', 'part.p_brand', 'lineitem.l_partkey', 'lineitem.l_orderkey', 'nation.n_nationkey', 'partsupp.ps_partkey', 'part.p_partkey', 'lineitem.l_suppkey', 'orders.o_orderkey', 'customer.c_acctbal', 'part.p_type']
    candidateIndexes = listToDict(candidateIndexes)
    return candidateIndexes
    candidateIndexes = set()
    I = set()
    # i=0
    for query in W:
        I = singleIndexableColumnsIn(query, schema)
        # print(I)
        bestIndexes = enumerateConfigs(conn, I, [query])
        print(bestIndexes)
        print()
        candidateIndexes = candidateIndexes.union(bestIndexes)
        # i+=1
        # if i==5:
        #     break
    candidateIndexes = listToDict(list(candidateIndexes))
    return candidateIndexes


def generateCandidateIndexes(conn_or_W, W_or_schema, schema=None):
    """Standard Candidate Generation interface wrapper."""
    if schema is not None:
        return bestConf(conn_or_W, W_or_schema, schema)
    # If called as (W, schema)
    return bestConf(None, conn_or_W, W_or_schema)

