from typing import *
import sqlglot
from auto_index_selector.CostEstimator.costEstimator import *
from itertools import combinations


# ---------------------------------------------------------------------------
# Query / schema parsing helpers
# ---------------------------------------------------------------------------

def getTablesIn(query: str) -> List:
    """Return the list of sqlglot Table expressions referenced by `query`."""
    parsedQuery = sqlglot.parse_one(query, read='postgres')
    return list(parsedQuery.find_all(sqlglot.exp.Table))


def singleIndexableColumnsIn(query: str, schema: Dict) -> List:
    """
    Return admissible single-column indexes ('table.column' strings) for
    `query`, restricted to columns that (a) actually appear in the query and
    (b) exist in `schema`.

    Note: for simplicity this treats *any* column referenced in the query
    (not just WHERE/GROUP BY/ORDER BY/UPDATE-SET columns, per the paper's
    strict definition of "indexable column" in Section 2.3) as a candidate.
    This is a superset of the paper's definition, which is a safe
    over-approximation -- it can only add extra candidates that Greedy will
    then discard for lack of benefit, never miss a genuinely indexable one.
    """
    indexable_columns = set()
    parsedQuery = sqlglot.parse_one(query, read='postgres')

    cols_in_query = {col.name for col in parsedQuery.find_all(sqlglot.exp.Column)}

    for table in parsedQuery.find_all(sqlglot.exp.Table):
        if table.name not in schema:
            continue
        for col in schema[table.name]:
            if col in cols_in_query:
                indexable_columns.add(f'{table.name}.{col}')

    return list(indexable_columns)


# ---------------------------------------------------------------------------
# Configuration enumeration (Greedy(m,k), see Figure 5 of the paper)
# ---------------------------------------------------------------------------

def _greedyExpand(conn, query: str, selected: List, remaining: List, k: int) -> List:
    """
    Shared greedy-expansion step used by both the pure-greedy phase and the
    seeded Greedy(m,k) phase: repeatedly add whichever remaining index gives
    the largest positive benefit, until `k` indexes are selected or no
    remaining index helps.
    """
    selected = list(selected)
    remaining = list(remaining)

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


def enumerateConfigsHelperGreedy(conn, I: List, total_columns: int, W: list) -> List:
    """
    Pure greedy phase: Greedy(0, k) with k unbounded, i.e. we keep adding
    the single most beneficial remaining index until no index yields a
    positive benefit. This is what `Enumerate(Ii, Wi)` reduces to when used
    for *candidate* selection (Section 4), as opposed to *final*
    configuration enumeration (Section 5), where a seed (m>0) and a bound
    (k) are used.
    """
    return _greedyExpand(conn, W[0], selected=[], remaining=I, k=total_columns)


def enumerateConfigsHelperGreedyWithSeed(conn, I: List, total_columns: int, W: list, m: int = 2) -> List:
    """
    Full Greedy(m, k): exhaustive search over an m-index seed, followed by
    greedy expansion. The paper found m=2 to work very well for *final*
    configuration enumeration (Section 7.4, step 2). Not used for candidate
    selection itself -- kept here for the later enumeration stage.
    """
    query = W[0]
    selected: List = []
    remaining = list(I)

    if m > 0 and len(I) >= m:
        best_seed_cost = float('inf')
        best_seed: List = []

        for combo in combinations(range(len(I)), m):
            seed = [I[i] for i in combo]
            _, cost_fin = estimateConfigurationCost(conn, query, seed)
            if cost_fin < best_seed_cost:
                best_seed_cost = cost_fin
                best_seed = seed

        selected = best_seed
        remaining = [idx for idx in I if idx not in selected]

    return _greedyExpand(conn, query, selected, remaining, k=total_columns)


def enumerateConfigs(conn, I: List, W: List) -> List:
    """Enumerate(I, W) with no bound on configuration size -- used to find
    the best configuration for a single query in the BEST-CONF algorithm."""
    return enumerateConfigsHelperGreedy(conn, I, total_columns=len(I), W=W)


def listToDict(candidateIndexes: List) -> Dict:
    """Turn a flat list of 'table.column' strings into {table: [columns]}."""
    result: Dict[str, List[str]] = {}
    for index in candidateIndexes:
        table, column = index.split('.', 1)
        result.setdefault(table, []).append(column)
    return result


# ---------------------------------------------------------------------------
# Candidate index selection (BEST-CONF, Section 4 / Figure 4)
# ---------------------------------------------------------------------------

def bestConf(conn, W: List, schema: Dict, max_per_query: Optional[int] = None) -> Dict:
    """
    Query-specific-best-configuration candidate index selection algorithm
    (Figure 4).

    Input:
        W          -> workload, a list of SQL query strings
        schema     -> {table: [columns]}
        max_per_query -> optional cap on how many candidate indexes to keep
                         per query. None reproduces BEST-CONF exactly
                         (unbounded, as in the paper). Passing 1 or 2
                         reproduces the BEST-CONF-1 / BEST-CONF-2 variants
                         discussed in Section 7.3.1, which the paper found
                         to noticeably hurt quality relative to unbounded
                         BEST-CONF -- kept here mainly for experimentation.

    Returns:
        candidateIndexes -> {table: [candidate columns]}
    """
    candidateIndexes: Set[str] = set()

    for query in W:
        I = singleIndexableColumnsIn(query, schema)
        bestIndexesForQuery = enumerateConfigs(conn, I, [query])

        if max_per_query is not None:
            bestIndexesForQuery = bestIndexesForQuery[:max_per_query]

        candidateIndexes |= set(bestIndexesForQuery)

    return listToDict(list(candidateIndexes))


def admissibleIndexes(W: List, schema: Dict) -> Dict:
    """
    The *un*-pruned baseline: every admissible single-column index for the
    workload (Section 2.3), with no BEST-CONF filtering. Useful as the
    "MJ"/"baseline" comparison point from Section 7.3.1 / Table 4 -- i.e.
    to measure how much BEST-CONF actually prunes for a given workload.
    """
    admissible: Set[str] = set()
    for query in W:
        admissible |= set(singleIndexableColumnsIn(query, schema))
    return listToDict(list(admissible))


def generateCandidateIndexes(conn, W: List, schema: Dict) -> Dict:
    '''
    Input : 
        W -> workload as a List
        schema -> dict
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: candidates}
    '''
    return bestConf(conn, W, schema)