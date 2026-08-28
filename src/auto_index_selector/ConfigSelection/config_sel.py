from itertools import combinations
import sqlglot
from ..CostEstimator.costEstimator import *


def getTablesUsed(query):
    """
    Extract tables from a SQL query.
    """
    parsed = sqlglot.parse_one(query)
    tables = []
    for table in parsed.find_all(sqlglot.exp.Table):
        tables.append(table.name)
    return list(dict.fromkeys(tables))


def sortedConfig(config):
    """
    Canonical form of a configuration.
    Converts any inner lists to tuples so the configuration is fully hashable and sortable.
    """
    canonical = tuple(tuple(x) if isinstance(x, list) else x for x in config)
    return tuple(sorted(canonical))


def getRelevantIndexes(tables, candidate_indexes):
    """
    Return the relevant index set I_q for one query.

    An index is relevant to a query only if it sits on a table that the
    query actually reads. Indexes on any other table cannot change the
    query's cost at all, so they are dropped.

    Note this returns ONE FLAT LIST of index names, not a list-of-lists.
    """
    relevant = []
    for table in tables:
        if table not in candidate_indexes:
            continue
        for index in candidate_indexes[table]:
            if index not in relevant:
                relevant.append(index)
    return relevant


def enumerateSubsets(relevant, mode="pairs"):
    """
    Enumerate the subsets of the relevant index set.

    mode = "pairs" : every single index, plus every pair of indexes.
                     This is the pruning the paper actually implements.
                     Cost is quadratic in the number of relevant indexes.

    mode = "all"   : every non-empty subset.
                     This is what the paper's quality guarantee needs.
                     Cost is 2^n, so only usable for small relevant sets.

    The empty set is never generated: a configuration with no indexes has
    benefit zero by definition, so there is nothing to measure.
    """
    subsets = []

    if mode == "pairs":
        for index in relevant:
            subsets.append((index,))
        for pair in combinations(relevant, 2):
            subsets.append(pair)
        return subsets

    if mode == "all":
        for size in range(1, len(relevant) + 1):
            for combo in combinations(relevant, size):
                subsets.append(combo)
        return subsets

    raise ValueError("mode must be 'pairs' or 'all', got " + repr(mode))


def generateConfigurations(candidate_indexes, workload, mode="pairs"):
    """
    Parameters
    ----------
    candidate_indexes : dict   table name -> list of index names
    workload          : list[str]
    mode              : "pairs" or "all"

    Returns
    -------
    list of configurations (each configuration is a tuple of index names)
    """
    configurations = list()
    seen = set()

    for query in workload:
        tables = getTablesUsed(query)
        relevant = getRelevantIndexes(tables, candidate_indexes)
        print(relevant)

        for config in enumerateSubsets(relevant, mode):
            # print(config)
            key = sortedConfig(config)
            # print(key)
            # key = list(key)
            if key not in configurations:
                # seen.add(key)
                configurations.append(key)

    return configurations


def generateConfigurationsPerQuery(candidate_indexes, workload, mode="pairs"):
    """
    Same enumeration, but keeps the link between a query and its own
    configurations instead of merging everything into one flat list.

    Returns
    -------
    dict (query string : list of configurations)
    """
    perQuery = dict()

    for query in workload:
        tables = getTablesUsed(query)
        relevant = getRelevantIndexes(tables, candidate_indexes)

        configs = []
        seen = set()
        for config in enumerateSubsets(relevant, mode):
            key = sortedConfig(config)
            if key not in seen:
                seen.add(key)
                configs.append(key)

        perQuery[query] = configs

    return perQuery


def numericConfig(configurations, indexSet):
    """
    Return : dict (ConfigNum : list(index))
    """
    configSet = dict()
    k = 1
    for config in configurations:
        configSet[k] = list()
        for index in config:
            temp = '['+','.join(index)+']'
            pos = indexSet[temp][0]
            configSet[k].append(pos)
        k += 1

    return configSet



####################################
# GREEDY(m, k) CONFIGURATION ENUM  #
####################################
#
# Candidate index representation used throughout this file:
#
#   (table: str, columns: tuple[str, ...])
#
# e.g. ('lineitem', ('l_returnflag', 'l_linestatus'))
#
# This matches your candidate dict format:
#   {'lineitem': [['l_shipdate'], ['l_returnflag', 'l_linestatus'], ...], ...}
# flattened via flattenCandidateIndexes() below.

from itertools import combinations

# Assumes these are importable from your cost_estimator module:
# clearHypotheticalIndexes, getQueryCost


def flattenCandidateIndexes(candidate_dict):
    """
    candidate_dict : dict(table -> list[list[str]])
                     (your generated candidate structure)

    Returns
    -------
    list[(table, tuple(columns))]  -- flat candidate index list,
    one entry per table/column-combo, ready for Greedy(m, k).
    """
    flat = []
    for table, col_lists in candidate_dict.items():
        for cols in col_lists:
            flat.append((table, tuple(cols)))
    return flat


def createCompositeHypoIndexes(conn, configuration):
    """
    Create HypoPG indexes for a configuration of (possibly multi-column)
    candidate indexes.

    configuration : iterable[(table, tuple(columns))]
    """
    with conn.cursor() as cur:
        for table, cols in configuration:
            col_list = ",".join(cols)
            stmt = f"CREATE INDEX ON {table}({col_list})"
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (stmt,))
    conn.commit()


def estimateWorkloadCostForConfig(conn, W, configuration, query_weights=None):
    """
    Total workload cost for ONE configuration (sum over all queries).

    configuration : iterable[(table, tuple(columns))]
    W             : list[str] SQL queries
    query_weights : dict, optional
                    per-query execution count weights: {query_str: call_count}

    Returns
    -------
    float -- sum of optimizer cost estimates across the workload
    """
    clearHypotheticalIndexes(conn)

    if configuration:
        createCompositeHypoIndexes(conn, configuration)

    total = 0.0
    for query in W:
        weight = float(query_weights.get(query, 1.0)) if query_weights else 1.0
        total += weight * getQueryCost(conn, query)

    clearHypotheticalIndexes(conn)
    return total


def greedyMK(conn, W, candidate_dict, m, k, cost_cache=None, write_penalties=None, query_weights=None):
    """
    Greedy(m, k) algorithm (Chaudhuri & Narasayya, 1997) with write penalty integration.

    Parameters
    ----------
    conn            : psycopg2 connection
    W               : list of query strings (the workload)
    candidate_dict  : dict {table: [[col1], [col1, col2], ...]}
    m               : size of exhaustively searched seed configuration
    k               : maximum number of indexes to select
    cost_cache      : dict, optional
                      shared across runs to avoid re-evaluating configurations
                      already scored.
    write_penalties : dict, optional
                      pre-computed write penalties: {(table, (col1, ...)): penalty_float}
    query_weights   : dict, optional
                      per-query execution frequency counts: {query_str: call_count}

    Returns
    -------
    frozenset[(table, tuple(columns))] -- the chosen configuration (<= k indexes)
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)

    # Obtain write penalty map via standard CostEstimator function
    penalties = estimateWorkloadCostUpdate(conn, W, candidate_dict, write_penalties=write_penalties)

    def config_write_penalty(config):
        if not penalties:
            return 0.0
        return sum(penalties.get(idx, 0.0) for idx in config)

    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            read_cost = estimateWorkloadCostForConfig(conn, W, key, query_weights=query_weights)
            write_cost = config_write_penalty(key)
            cost_cache[key] = read_cost + write_cost
        return cost_cache[key]

    m_eff = min(m, k, len(candidate_indexes))
    # Prevent combinatorial explosion if candidate pool is very large (e.g. cg_naive with >50 candidates)
    if len(candidate_indexes) > 50 and m_eff > 1:
        m_eff = 1

    # --- Step 1: find the best seed of size <= m_eff ---
    best_seed = frozenset()
    best_seed_cost = cost(best_seed)  # cost with no indexes at all

    for size in range(1, m_eff + 1):
        for combo in combinations(candidate_indexes, size):
            c = frozenset(combo)
            c_cost = cost(c)
            if c_cost < best_seed_cost:
                best_seed_cost = c_cost
                best_seed = c

    S = best_seed

    if len(S) >= k:
        return S

    # --- Step 2: greedily extend S one index at a time ---
    remaining = [idx for idx in candidate_indexes if idx not in S]

    while len(S) < k and remaining:
        current_cost = cost(S)

        best_index = None
        best_extended_cost = float("inf")

        for I in remaining:
            trial = S | {I}
            trial_cost = cost(trial)
            if trial_cost < best_extended_cost:
                best_extended_cost = trial_cost
                best_index = I

        # Stop if adding the best available index doesn't reduce cost
        if best_index is None or best_extended_cost >= current_cost:
            break

        S = S | {best_index}
        remaining.remove(best_index)

    return S


def selectConfiguration(conn, W, candidate_dict, m=2, k=10, cost_cache=None,
                        write_penalties=None, query_weights=None, **kwargs):
    """Standard interface for configuration selection."""
    return greedyMK(conn, W, candidate_dict, m=m, k=k,
                    cost_cache=cost_cache,
                    write_penalties=write_penalties,
                    query_weights=query_weights)