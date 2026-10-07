"""
cs_extend.py — Extend Algorithm for Configuration Selection.

Reference: Schlosser, Kossmann, Boissier.
"Efficient Scalable Multi-Attribute Index Selection Using Recursive Strategies."
ICDE 2019 (Algorithm 1, Heuristic H6).

Candidate generation is handled by cg_extend.generateCandidateIndexes(), which
extracts index-relevant columns from the workload SQL.  This module receives
the resulting candidate_dict and runs the Extend algorithm on it.

Algorithm steps
---------------
1. Seed: from the candidate set, pick the single index with best ΔCost / ΔSize.
2. Expand: each round, try
       Option A — Add a new candidate index not yet in the configuration.
       Option B — Morph an existing index by appending a column
                  (replace (table, (c1,)) with (table, (c1, c2)) if
                  (table, (c1, c2)) exists in the candidate set).
   Accept the move with the highest ΔCost / ΔSize that also passes the
   minimum-improvement threshold.  Stop when no move qualifies.

Cost evaluation uses estimateWorkloadCostForConfig sequentially, matching cs_drop / cs_greedy.

selectConfigurations() sweeps all budgets with a shared
cost_cache — same pattern as cs_drop.selectConfigurations().
"""

from collections import defaultdict
from typing import Dict, Tuple

from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)
from .cs_drop import buildSizeMap, estimateIndexSize
from .config_sel import flattenCandidateIndexes

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _mb(x: float) -> float:
    return x / (1024.0 ** 2)


# ---------------------------------------------------------------------------
# Inner algorithm
# ---------------------------------------------------------------------------

def _extend_inner(conn, W, candidate_indexes, size_cache, cost_cache,
                  storage_budget, max_index_width=3,
                  min_cost_improvement=1.003, verbose=False,
                  write_penalties=None, query_weights=None):
    """
    Extend algorithm core.
    """
    def cost(cfg):
        if cfg not in cost_cache:
            cost_cache[cfg] = estimateWorkloadCostForConfig(
                conn, W, cfg,
                query_weights=query_weights,
                write_penalties=write_penalties
            )
        return cost_cache[cfg]

    def batch_cost_cached(cfgs):
        results = {}
        for c in cfgs:
            results[c] = cost(c)
        return results

    def size_bytes(cfg):
        return sum(size_cache[idx] for idx in cfg)

    def passes(new_cost, cur_cost):
        return new_cost * min_cost_improvement < cur_cost

    # -----------------------------------------------------------------------
    # Step 1: Baseline — empty configuration
    # -----------------------------------------------------------------------
    S = frozenset()
    F_current = cost(S)

    if verbose:
        print(f"  [Extend] baseline cost={F_current:,.2f}  "
              f"budget={_mb(storage_budget):,.1f} MB")

    # -----------------------------------------------------------------------
    # Step 2: Seed — pick the best single-column candidate index
    # -----------------------------------------------------------------------
    seed_trials = [
        frozenset([idx]) for idx in candidate_indexes
        if size_cache.get(idx, float("inf")) <= storage_budget
    ]

    if not seed_trials:
        if verbose:
            print("  [Extend] no candidate fits in budget — returning empty config")
        return S

    seed_costs = batch_cost_cached(seed_trials)
    best_seed, best_seed_cost, best_seed_ratio = None, F_current, -float("inf")

    for trial, tcost in seed_costs.items():
        delta = F_current - tcost
        if delta <= 0 or not passes(tcost, F_current):
            continue
        sz = size_bytes(trial)
        ratio = delta / max(sz, 1.0)
        if ratio > best_seed_ratio:
            best_seed_ratio, best_seed, best_seed_cost = ratio, trial, tcost

    if best_seed is None:
        if verbose:
            print("  [Extend] no beneficial seed — returning empty config")
        return S

    S, F_current = best_seed, best_seed_cost
    if verbose:
        print(f"  [Extend] seed={sorted(S)}  cost={F_current:,.2f}  "
              f"size={_mb(size_bytes(S)):,.1f} MB")

    # -----------------------------------------------------------------------
    # Step 3: Expansion loop — Add (Option A) or Morph (Option B)
    # -----------------------------------------------------------------------
    iteration = 0
    while True:
        iteration += 1
        trials: Dict[frozenset, Tuple[str, float]] = {}
        current_size = size_bytes(S)

        for idx in candidate_indexes:
            table, cols = idx

            # --- Option A: Add this candidate index if not already in S -------
            if idx not in S:
                new_S = S | frozenset([idx])
                new_sz = size_bytes(new_S)
                if new_sz <= storage_budget:
                    trials[new_S] = (
                        f"ADD {table}({','.join(cols)})",
                        new_sz - current_size,
                    )

            # --- Option B: Morph — replace an existing index on the same table
            # with this candidate index by appending its column to the existing index.
            # Only append if the column is not already in the index.
            for existing in list(S):
                e_table, e_cols = existing
                if e_table != table:
                    continue
                if len(cols) != 1:
                    continue
                
                c_new = cols[0]
                if c_new in e_cols:
                    continue
                    
                new_cols = tuple(list(e_cols) + [c_new])
                if len(new_cols) > max_index_width:
                    continue
                    
                new_idx = (table, new_cols)
                
                # Check size_cache or calculate it
                if new_idx not in size_cache:
                    try:
                        from .cs_drop import estimateIndexSize
                        size_cache[new_idx] = estimateIndexSize(conn, table, new_cols)
                    except Exception:
                        continue
                        
                new_S = (S - frozenset([existing])) | frozenset([new_idx])
                new_sz = (current_size
                          - size_cache[existing]
                          + size_cache[new_idx])
                if new_sz <= storage_budget:
                    trials[new_S] = (
                        f"MORPH {e_table}({','.join(e_cols)})"
                        f"->({','.join(new_cols)})",
                        new_sz - current_size,
                    )

        if not trials:
            if verbose:
                print(f"  [Extend] iter {iteration}: no valid moves — done")
            break

        trial_costs = batch_cost_cached(list(trials.keys()))

        best_cfg, best_ratio = None, -float("inf")
        best_cost_val, best_label = F_current, ""

        for trial_cfg, (label, delta_size) in trials.items():
            tcost = trial_costs[trial_cfg]
            delta_cost = F_current - tcost
            if delta_cost <= 0 or not passes(tcost, F_current):
                continue
            ratio = delta_cost / max(delta_size, 1.0)
            if ratio > best_ratio:
                best_ratio, best_cfg, best_cost_val, best_label = (
                    ratio, trial_cfg, tcost, label
                )

        if best_cfg is None:
            if verbose:
                print(f"  [Extend] iter {iteration}: no improving move — done")
            break

        S, F_current = best_cfg, best_cost_val
        if verbose:
            print(f"  [Extend] iter {iteration}: {best_label}  "
                  f"cost={F_current:,.2f}  size={_mb(size_bytes(S)):,.1f} MB")

    if verbose:
        print(f"  [Extend] RESULT |S|={len(S)}  "
              f"size={_mb(size_bytes(S)):,.1f} MB  "
              f"of {_mb(storage_budget):,.1f} MB  cost={F_current:,.2f}")

    return S


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

def _setup(conn, W, candidate_dict, size_cache, cost_cache):
    """
    Flatten candidates, build size map (single-col + likely morphed combos),
    and return (candidate_indexes, size_cache, cost_cache).
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    # Removed O(N^2) size_cache pre-population for 2-column combinations.
    # Sizes are now evaluated dynamically during the Morph step.

    return candidate_indexes, size_cache, cost_cache


# ---------------------------------------------------------------------------
# Public API — single budget
# ---------------------------------------------------------------------------

def selectConfiguration(conn, W, candidate_dict, storage_budget,
                        max_index_width=3, min_cost_improvement=1.003,
                        cost_cache=None, size_cache=None,
                        n_workers=None, db_name=None,
                        write_penalties=None, query_weights=None, **kwargs):
    """
    Single-budget entry point.

    candidate_dict must come from cg_extend.generateCandidateIndexes().
    storage_budget is in BYTES (e.g. 500 * 1024**2 for 500 MB).
    """
    candidate_indexes, size_cache, cost_cache = _setup(
        conn, W, candidate_dict, size_cache, cost_cache
    )

    return _extend_inner(
        conn, W, candidate_indexes, size_cache, cost_cache,
        storage_budget, max_index_width, min_cost_improvement,
        verbose=True, write_penalties=write_penalties, query_weights=query_weights
    )


# ---------------------------------------------------------------------------
# Public API — multi-budget sweep
# ---------------------------------------------------------------------------

def selectConfigurations(conn, W, candidate_dict, storage_budgets_mb,
                         max_index_width=3, min_cost_improvement=1.003,
                         cost_cache=None, size_cache=None,
                         n_workers=None, verbose=True, db_name=None,
                         write_penalties=None, query_weights=None, **kwargs):
    """
    Run Extend for *multiple* storage budgets in a single pass.
    """
    candidate_indexes, size_cache, cost_cache = _setup(
        conn, W, candidate_dict, size_cache, cost_cache
    )

    if verbose:
        print(f"  [Extend] {len(candidate_indexes)} candidate indexes "
              f"(from cg_extend)")

    budgets_bytes = sorted(
        [int(b * 1024 * 1024) for b in storage_budgets_mb], reverse=True
    )

    configs = {}
    for budget in budgets_bytes:
        if verbose:
            print(f"\n=== Extend  budget={_mb(budget):,.0f} MB ===")
        configs[budget] = _extend_inner(
            conn, W, candidate_indexes, size_cache, cost_cache,
            budget, max_index_width, min_cost_improvement, verbose,
            write_penalties=write_penalties, query_weights=query_weights
        )

    return configs