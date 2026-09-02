import sqlglot
from auto_index_selector.CostEstimator.costEstimator import (
    flattenCandidateIndexes,
    make_db_params,
    parallel_cost_evaluator,
)
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


def _greedy_inner(pce, candidate_indexes, cost_cache, m, k, verbose=False):
    """
    Greedy(m, k) enumeration algorithm running against an open parallel_cost_evaluator.
    """
    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            results = pce.batch_cost([key])
            cost_cache.update(results)
        return cost_cache[key]

    def batch_cost_cached(configs):
        """Evaluate a list of frozenset configs, skipping already-cached ones."""
        uncached = [c for c in configs if c not in cost_cache]
        if uncached:
            results = pce.batch_cost(uncached)
            cost_cache.update(results)
        return {c: cost_cache[c] for c in configs}

    eff_m = min(m, k, len(candidate_indexes))

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
    # After each step the winner's cost becomes current_cost for the next step,
    # so we never need to re-evaluate cost(S) at the top of the loop.
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
        # The winning trial cost IS cost(new S) — carry it forward.
        current_cost = best_extended_cost

    return S


def greedyMK(conn, W, candidate_dict, m=2, k=10, cost_cache=None, n_workers=None, db_name=None, verbose=False):
    """
    Greedy(m, k) enumeration algorithm (Chaudhuri & Narasayya, Section 5).
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    db_params = make_db_params(db_name=db_name)

    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        return _greedy_inner(pce, candidate_indexes, cost_cache, m=m, k=k, verbose=verbose)


def selectConfiguration(conn, W, candidate_dict, k=10, m=2, cost_cache=None, n_workers=None, db_name=None, **kwargs):
    """Standard single-configuration entry point."""
    return greedyMK(conn, W, candidate_dict, m=m, k=k, cost_cache=cost_cache, n_workers=n_workers, db_name=db_name)


def selectConfigurations(conn, W, candidate_dict, k_list: List[int], m: int = 2,
                         cost_cache=None, n_workers=None, db_name=None, verbose=True, **kwargs):
    """
    Run Greedy(m, k) for *multiple* k values in a single pass.

    Opens ONE parallel_cost_evaluator and shares cost_cache across all steps.
    Since greedy adds indexes one by one, a single forward pass up to max(k)
    yields the configurations for all requested k values.

    Parameters
    ----------
    k_list : list[int]
        Target configuration sizes (e.g. [2, 3, 4, 5, 6, 7, 8, 9, 10]).
    m : int
        Size of seed configuration (default 2).

    Returns
    -------
    dict[int, frozenset]
        {k: chosen_config} for every requested k.
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    db_params = make_db_params(db_name=db_name)

    sorted_k = sorted(k_list)
    max_k = max(sorted_k)
    configs = {}

    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        def cost(config):
            key = frozenset(config)
            if key not in cost_cache:
                results = pce.batch_cost([key])
                cost_cache.update(results)
            return cost_cache[key]

        def batch_cost_cached(configs_to_eval):
            uncached = [c for c in configs_to_eval if c not in cost_cache]
            if uncached:
                results = pce.batch_cost(uncached)
                cost_cache.update(results)
            return {c: cost_cache[c] for c in configs_to_eval}

        eff_m = min(m, max_k, len(candidate_indexes))

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

        # Greedy expansion up to max_k.
        # current_cost for the first iteration is the seed cost already in cache.
        # After each step the winner's cost becomes current_cost for the next step,
        # so we never need to re-evaluate cost(S) at the top of the loop.
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
            # The winning trial cost IS cost(new S) — carry it forward.
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