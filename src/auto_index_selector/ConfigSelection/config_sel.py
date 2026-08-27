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

    A configuration is a SET of indexes, so (A, B) and (B, A) are the same
    thing. Sorting puts them into one fixed order so they can be compared.
    We return a tuple (not a list) because tuples can be put inside a set,
    which is what makes the duplicate check fast.
    """
    return tuple(sorted(config))


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


def estimateWorkloadCostForConfig(conn, W, configuration):
    """
    Total workload cost for ONE configuration (sum over all queries).

    configuration : iterable[(table, tuple(columns))]
    W             : list[str] SQL queries

    Returns
    -------
    float -- sum of optimizer cost estimates across the workload
    """
    clearHypotheticalIndexes(conn)

    if configuration:
        createCompositeHypoIndexes(conn, configuration)

    total = 0.0
    for query in W:
        total += getQueryCost(conn, query)

    clearHypotheticalIndexes(conn)
    return total


def greedyMK(conn, W, candidate_dict, m, k, cost_cache=None, write_penalties=None):
    """
    Greedy(m, k) enumeration algorithm (Chaudhuri & Narasayya, Section 5),
    adapted to the table -> [[col,...], ...] candidate format with write penalty support.

    Parameters
    ----------
    conn            : psycopg2 connection
    W               : list[str] SQL queries (the workload)
    candidate_dict  : dict(table -> list[list[str]])
                      your existing candidate-index structure
    m               : size of the exhaustively-searched seed configuration.
                      m=2 is the paper's recommended default.
    k               : final number of indexes to pick
    cost_cache      : optional dict for memoizing frozenset(config) -> cost.
                      Pass {} in if sweeping parameters repeatedly so you
                      don't re-hit HypoPG+optimizer for configs you've
                      already scored.
    write_penalties : dict, optional
                      pre-computed write penalties: {(table, (col1, ...)): penalty_float}

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
            read_cost = estimateWorkloadCostForConfig(conn, W, key)
            write_cost = config_write_penalty(key)
            cost_cache[key] = read_cost + write_cost
        return cost_cache[key]

    m = min(m, k, len(candidate_indexes))

    # --- Step 1: exhaustively find the best seed of size <= m ---
    # Exhaustive over small subsets so it stays cheap, but it's what lets
    # Greedy(m,k) capture index *interactions* a pure greedy walk (m=0)
    # would miss -- e.g. two columns that are only valuable together.
    best_seed = frozenset()
    best_seed_cost = cost(best_seed)  # cost with no indexes at all

    for size in range(1, m + 1):
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