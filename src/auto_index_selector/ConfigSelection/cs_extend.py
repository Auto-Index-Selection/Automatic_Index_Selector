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

Cost evaluation uses parallel_cost_evaluator (HypoPG + multiprocessing),
matching cs_drop / cs_greedy.

selectConfigurations() sweeps all budgets in ONE pce session with a shared
cost_cache — same pattern as cs_drop.selectConfigurations().
"""

from collections import defaultdict
from typing import Dict, Tuple

from auto_index_selector.CostEstimator.costEstimator import (
    flattenCandidateIndexes,
    make_db_params,
    parallel_cost_evaluator,
)
from .cs_drop import buildSizeMap, estimateIndexSize


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _mb(x: float) -> float:
    return x / (1024.0 ** 2)


# ---------------------------------------------------------------------------
# Inner algorithm (runs against an already-open pce)
# ---------------------------------------------------------------------------

def _extend_inner(pce, candidate_indexes, size_cache, cost_cache,
                  storage_budget, max_index_width=3,
                  min_cost_improvement=1.003, verbose=False):
    """
    Extend algorithm core.  Runs inside an open parallel_cost_evaluator.

    Parameters
    ----------
    pce              : open parallel_cost_evaluator context
    candidate_indexes: list[(table, tuple[str,...])]  from flattenCandidateIndexes
    size_cache       : {(table, cols) -> size_bytes}
    cost_cache       : {frozenset -> workload_cost}  (shared across budget sweeps)
    storage_budget   : hard limit in BYTES
    max_index_width  : maximum columns per index (Morph stops here)
    min_cost_improvement : cost must satisfy new_cost * factor < cur_cost

    Returns
    -------
    frozenset[(table, tuple[str,...])]
    """

    def cost(cfg):
        if cfg not in cost_cache:
            cost_cache.update(pce.batch_cost([cfg]))
        return cost_cache[cfg]

    def batch_cost_cached(cfgs):
        uncached = [c for c in cfgs if c not in cost_cache]
        if uncached:
            cost_cache.update(pce.batch_cost(uncached))
        return {c: cost_cache[c] for c in cfgs}

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
            # with this candidate index, provided the candidate strictly extends
            # the existing index (existing_cols is a prefix of candidate cols).
            for existing in list(S):
                e_table, e_cols = existing
                if e_table != table:
                    continue
                if len(cols) <= len(e_cols):
                    continue
                if cols[:len(e_cols)] != e_cols:   # candidate must extend existing
                    continue
                if len(cols) > max_index_width:
                    continue
                if idx not in size_cache:
                    continue

                new_S = (S - frozenset([existing])) | frozenset([idx])
                new_sz = (current_size
                          - size_cache[existing]
                          + size_cache[idx])
                if new_sz <= storage_budget:
                    trials[new_S] = (
                        f"MORPH {e_table}({','.join(e_cols)})"
                        f"->({','.join(cols)})",
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

def _setup(conn, W, candidate_dict, size_cache, cost_cache, n_workers, db_name=None):
    """
    Flatten candidates, build size map (single-col + likely morphed combos),
    and return (candidate_indexes, size_cache, cost_cache, db_params).
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    # Pre-populate sizes for 2-column morphed indexes (c1,c2) and (c2,c1)
    # so the Morph branch in _extend_inner never hits a missing key.
    tables_cols: Dict[str, list] = defaultdict(list)
    for table, cols in candidate_indexes:
        if len(cols) == 1:
            tables_cols[table].append(cols[0])

    for table, col_list in tables_cols.items():
        for i, c1 in enumerate(col_list):
            for c2 in col_list[i + 1:]:
                for morph_cols in [(c1, c2), (c2, c1)]:
                    key = (table, morph_cols)
                    if key not in size_cache:
                        try:
                            size_cache[key] = estimateIndexSize(
                                conn, table, morph_cols
                            )
                        except Exception:
                            pass

    db_params = make_db_params(db_name=db_name)
    return candidate_indexes, size_cache, cost_cache, db_params


# ---------------------------------------------------------------------------
# Public API — single budget
# ---------------------------------------------------------------------------

def selectConfiguration(conn, W, candidate_dict, storage_budget,
                        max_index_width=3, min_cost_improvement=1.003,
                        cost_cache=None, size_cache=None,
                        n_workers=None, db_name=None, **kwargs):
    """
    Single-budget entry point.

    candidate_dict must come from cg_extend.generateCandidateIndexes().
    storage_budget is in BYTES (e.g. 500 * 1024**2 for 500 MB).
    """
    candidate_indexes, size_cache, cost_cache, db_params = _setup(
        conn, W, candidate_dict, size_cache, cost_cache, n_workers, db_name=db_name
    )

    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        return _extend_inner(
            pce, candidate_indexes, size_cache, cost_cache,
            storage_budget, max_index_width, min_cost_improvement,
            verbose=True,
        )


# ---------------------------------------------------------------------------
# Public API — multi-budget sweep (one pce session for all budgets)
# ---------------------------------------------------------------------------

def selectConfigurations(conn, W, candidate_dict, storage_budgets_mb,
                         max_index_width=3, min_cost_improvement=1.003,
                         cost_cache=None, size_cache=None,
                         n_workers=None, verbose=True, db_name=None, **kwargs):
    """
    Run Extend for *multiple* storage budgets in a single pass.

    Opens ONE parallel_cost_evaluator and shares cost_cache across all budget
    levels — same pattern as cs_drop.selectConfigurations().

    candidate_dict must come from cg_extend.generateCandidateIndexes().

    Parameters
    ----------
    storage_budgets_mb : list[int | float]
        Budgets in MEGABYTES.  Order does not matter; sorted descending.

    Returns
    -------
    dict[int, frozenset]   {budget_bytes: chosen_config}
    """
    candidate_indexes, size_cache, cost_cache, db_params = _setup(
        conn, W, candidate_dict, size_cache, cost_cache, n_workers, db_name=db_name
    )

    if verbose:
        print(f"  [Extend] {len(candidate_indexes)} candidate indexes "
              f"(from cg_extend)")

    budgets_bytes = sorted(
        [int(b * 1024 * 1024) for b in storage_budgets_mb], reverse=True
    )

    configs = {}
    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        for budget in budgets_bytes:
            if verbose:
                print(f"\n=== Extend  budget={_mb(budget):,.0f} MB ===")
            configs[budget] = _extend_inner(
                pce, candidate_indexes, size_cache, cost_cache,
                budget, max_index_width, min_cost_improvement, verbose,
            )

    return configs