import sqlglot
from auto_index_selector.CostEstimator.costEstimator import *
from itertools import combinations





####################################
# GREEDY(m, k) CONFIGURATION ENUM  #
####################################
#
# Candidate index representation used throughout this file:
#
#   (table: str, columns: tuple[str, ...])
#
# e.g. ('lineitem', ('l_returnflag', 'l_linestatus'))
#   {'lineitem': [['l_shipdate'], ['l_returnflag', 'l_linestatus'], ...], ...}
# flattened via flattenCandidateIndexes() below.

def greedyMK(conn, W, candidate_dict, m, k, cost_cache=None):
    """
    Greedy(m, k) enumeration algorithm (Chaudhuri & Narasayya, Section 5),
    adapted to the table -> [[col,...], ...] candidate format.

    Parameters
    ----------
    conn           : psycopg2 connection
    W              : list[str] SQL queries (the workload)
    candidate_dict : dict(table -> list[list[str]])
                     your existing candidate-index structure
    m              : size of the exhaustively-searched seed configuration.
                      m=2 is the paper's recommended default.
    k              : final number of indexes to pick
    cost_cache     : optional dict for memoizing frozenset(config) -> cost.
                      Pass {} in if sweeping parameters repeatedly so you
                      don't re-hit HypoPG+optimizer for configs you've
                      already scored.

    Returns
    -------
    frozenset[(table, tuple(columns))] -- the chosen configuration (<= k indexes)
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)

    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            cost_cache[key] = estimateWorkloadCostForConfig(conn, W, key)
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

def selectConfiguration(conn, W, candidate_dict, m, k, cost_cache=None):
    return greedyMK(conn, W, candidate_dict, m, k)