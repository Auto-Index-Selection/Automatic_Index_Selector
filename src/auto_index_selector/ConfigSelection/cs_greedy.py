import sqlglot
from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)
from .config_sel import flattenCandidateIndexes
from itertools import combinations
from typing import Dict, List, Optional


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


def _greedy_inner(conn, W, candidate_indexes, cost_cache, m, k, query_weights=None, write_penalties=None, verbose=True):
    """
    Greedy(m, k) enumeration algorithm.
    """
    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            cost_cache[key] = estimateWorkloadCostForConfig(
                conn, W, key,
                query_weights=query_weights,
                write_penalties=write_penalties
            )
        return cost_cache[key]

    def batch_cost_cached(configs):
        """Evaluate a list of frozenset configs, skipping already-cached ones."""
        results = {}
        for c in configs:
            results[c] = cost(c)
        return results

    eff_m = min(m, k, len(candidate_indexes))

    # Prevent combinatorial explosion if candidate pool is very large
    if len(candidate_indexes) > 50 and eff_m > 1:
        eff_m = 1

    # --- Step 1: exhaustively find the best seed of size <= m ---
    best_seed = frozenset()
    best_seed_cost = cost(best_seed)   # cost with no indexes at all

    for size in range(1, eff_m + 1):
        combos = [frozenset(c) for c in combinations(candidate_indexes, size)]
        results = batch_cost_cached(combos)
        for c, c_cost in results.items():
            if c_cost < best_seed_cost:
                best_seed_cost = c_cost
                best_seed = c

    S = best_seed

    if len(S) >= k:
        return S

    # --- Step 2: greedily extend S one index at a time ---
    remaining = [idx for idx in candidate_indexes if idx not in S]

    # current_cost for the first iteration is the seed cost already in cache.
    current_cost = cost(S)

    while len(S) < k and remaining:
        trials = {S | {I}: I for I in remaining}
        results = batch_cost_cached(list(trials.keys()))

        best_index = None
        best_extended_cost = float("inf")

        for trial_config, trial_cost in results.items():
            if trial_cost < best_extended_cost:
                best_extended_cost = trial_cost
                best_index = trials[trial_config]

        # Stop if adding the best available index doesn't reduce cost
        if best_index is None or best_extended_cost >= current_cost:
            break

        S = S | {best_index}
        remaining.remove(best_index)
        current_cost = best_extended_cost

    return S


def greedyMK(conn, W, candidate_dict, m=2, k=10, cost_cache=None, write_penalties=None, query_weights=None, verbose=False, **kwargs):
    """
    Greedy(m, k) enumeration algorithm (Chaudhuri & Narasayya, Section 5).
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)

    return _greedy_inner(conn, W, candidate_indexes, cost_cache, m=m, k=k,
                         query_weights=query_weights, write_penalties=write_penalties, verbose=verbose)


def selectConfiguration(conn, W, candidate_dict, k=10, m=2, cost_cache=None, write_penalties=None, query_weights=None, **kwargs):
    """Standard single-configuration entry point."""
    return greedyMK(conn, W, candidate_dict, m=m, k=k, cost_cache=cost_cache,
                    write_penalties=write_penalties, query_weights=query_weights)


def selectConfigurations(conn, W, candidate_dict, k_list: List[int], m: int = 2,
                         cost_cache=None, write_penalties=None, query_weights=None, verbose=True, **kwargs):
    """
    Run Greedy(m, k) for *multiple* k values in a single pass.
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)

    sorted_k = sorted(k_list)
    max_k = max(sorted_k)
    configs = {}

    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            cost_cache[key] = estimateWorkloadCostForConfig(
                conn, W, key,
                query_weights=query_weights,
                write_penalties=write_penalties
            )
        return cost_cache[key]

    def batch_cost_cached(configs_to_eval):
        results = {}
        for c in configs_to_eval:
            results[c] = cost(c)
        return results

    eff_m = min(m, max_k, len(candidate_indexes))
    if len(candidate_indexes) > 50 and eff_m > 1:
        eff_m = 1

    # Seed phase
    best_seed = frozenset()
    best_seed_cost = cost(best_seed)

    for size in range(1, eff_m + 1):
        combos = [frozenset(c) for c in combinations(candidate_indexes, size)]
        results = batch_cost_cached(combos)
        for c, c_cost in results.items():
            if c_cost < best_seed_cost:
                best_seed_cost = c_cost
                best_seed = c

    S = best_seed
    remaining = [idx for idx in candidate_indexes if idx not in S]

    # Record configs at or below seed size
    for k_val in sorted_k:
        if k_val <= len(S):
            configs[k_val] = S

    current_cost = cost(S)

    while len(S) < max_k and remaining:
        trials = {S | {I}: I for I in remaining}
        results = batch_cost_cached(list(trials.keys()))

        best_index = None
        best_extended_cost = float("inf")

        for trial_config, trial_cost in results.items():
            if trial_cost < best_extended_cost:
                best_extended_cost = trial_cost
                best_index = trials[trial_config]

        if best_index is None or best_extended_cost >= current_cost:
            if verbose:
                print(f"  [Greedy] No further cost improvement at |S|={len(S)} (cost={current_cost:,.2f})")
            break

        S = S | {best_index}
        remaining.remove(best_index)
        current_cost = best_extended_cost

        if len(S) in sorted_k:
            configs[len(S)] = S
            if verbose:
                print(f"  [Greedy] k={len(S)} config selected: {len(S)} indexes, cost={best_extended_cost:,.2f}")

    # For any k larger than stopping point, map to final S
    for k_val in sorted_k:
        if k_val not in configs:
            configs[k_val] = S

    return configs
