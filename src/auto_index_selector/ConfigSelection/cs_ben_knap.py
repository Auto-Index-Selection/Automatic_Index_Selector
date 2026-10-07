"""
cs_ben_knap.py — BEN_KNAP Configuration Selection.
"""

import sqlglot as sg
from itertools import combinations
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

import numpy as np
import gurobipy as gp
from gurobipy import GRB

from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig
)
from .config_sel import flattenCandidateIndexes
from .cs_drop import buildSizeMap, estimateIndexSize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb(x: float) -> float:
    return x / (1024.0 ** 2)


def _query_tables(query: str) -> set:
    """Tables referenced by a query (used to restrict Iq)."""
    tables = set()
    parsed = sg.parse_one(query)
    for t in parsed.find_all(sg.exp.Table):
        tables.add(t.name)
    return tables


def _relevant_indexes(query: str, candidate_indexes: List[Tuple]) -> List[Tuple]:
    tables = _query_tables(query)
    return [idx for idx in candidate_indexes if idx[0] in tables]


def _pairwise_subsets(indexes: List[Tuple]) -> List[Tuple[Tuple, ...]]:
    """Singletons + pairs — the Section 6.4 pruning heuristic."""
    subsets = [(idx,) for idx in indexes]
    subsets += list(combinations(indexes, 2))
    return subsets


# ---------------------------------------------------------------------------
# Phase 1 internals
# ---------------------------------------------------------------------------

def _query_fq_values(conn, query, subsets, cost_cache, query_weights=None):
    """Evaluate Fq(S) = cost(query, {}) − cost(query, S) for every subset S."""
    keys_needed = [frozenset(S) for S in subsets] + [frozenset()]
    uncached = [k for k in keys_needed if (query, k) not in cost_cache]

    if uncached:
        for k in uncached:
            cost_cache[(query, k)] = estimateWorkloadCostForConfig(
                conn, [query], k,
                query_weights=query_weights,
                write_penalties=None # Do not double count write penalties per query
            )

    base_cost = cost_cache[(query, frozenset())]
    fq_values = {}
    for S in subsets:
        key = frozenset(S)
        fq_values[tuple(S)] = base_cost - cost_cache[(query, key)]
    return fq_values


def _lp_feasible(subsets, fq_values, indexes, k):
    """Zero-objective LP feasibility check."""
    n = len(indexes)
    index_pos = {idx: p for p, idx in enumerate(indexes)}

    try:
        model = gp.Model()
        model.Params.OutputFlag = 0

        b_vars = model.addVars(n, lb=-1e12, ub=1e12, name="b")
        model.setObjective(0, GRB.MINIMIZE)

        for S in subsets:
            fq = fq_values[S]
            lhs = gp.LinExpr()
            for idx in S:
                lhs += b_vars[index_pos[idx]]

            model.addConstr(lhs <= k * fq)
            model.addConstr(lhs >= (fq / k if k != 0 else 0.0))

        model.optimize()

        if model.Status == GRB.OPTIMAL:
            solution = np.array([b_vars[i].X for i in range(n)])
            return True, solution
        return False, None
    except gp.GurobiError:
        return False, None


def _k_tolerant_benefit_vector(
    conn, query, indexes, cost_cache, query_weights=None,
    k_start=1.0, max_doublings=20, binary_search_iters=20,
):
    """Find the smallest K for which a K-tolerant benefit vector exists."""
    if not indexes:
        return {}, 1.0

    subsets = _pairwise_subsets(indexes)
    fq_values = _query_fq_values(conn, query, subsets, cost_cache, query_weights=query_weights)

    if all(v == 0.0 for v in fq_values.values()):
        return {idx: 0.0 for idx in indexes}, 1.0

    k = k_start
    feasible, solution = _lp_feasible(subsets, fq_values, indexes, k)
    doublings = 0
    while not feasible and doublings < max_doublings:
        k *= 2
        feasible, solution = _lp_feasible(subsets, fq_values, indexes, k)
        doublings += 1

    if not feasible:
        best_S = max(fq_values, key=lambda S: fq_values[S])
        share = fq_values[best_S] / len(best_S) if best_S else 0.0
        return {idx: (share if idx in best_S else 0.0) for idx in indexes}, float(k)

    lo = k / 2 if doublings > 0 else 0.0
    hi = k
    best_k, best_solution = k, solution

    for _ in range(binary_search_iters):
        mid = (lo + hi) / 2
        if mid <= 0:
            break
        feas, sol = _lp_feasible(subsets, fq_values, indexes, mid)
        if feas:
            hi = mid
            best_k, best_solution = mid, sol
        else:
            lo = mid

    return {idx: float(best_solution[p]) for p, idx in enumerate(indexes)}, best_k


def _assign_workload_benefits(conn, W, candidate_indexes, cost_cache, query_weights=None, verbose=False):
    """Phase 1: run benefit assignment across the whole workload."""
    total_benefit: Dict[Tuple, float] = {}
    k_max = 1.0

    for i, query in enumerate(W):
        relevant = _relevant_indexes(query, candidate_indexes)
        benefits, k = _k_tolerant_benefit_vector(
            conn, query, relevant, cost_cache, query_weights=query_weights
        )
        k_max = max(k_max, k)
        for idx, b in benefits.items():
            total_benefit[idx] = total_benefit.get(idx, 0.0) + b
        if verbose:
            print(
                f"  [BEN_KNAP] query {i+1}/{len(W)} done  "
                f"K={k:.3f}  |Iq|={len(relevant)}"
            )

    return total_benefit, k_max


# ---------------------------------------------------------------------------
# Phase 2 — greedy 0-1 knapsack
# ---------------------------------------------------------------------------

def _greedy_knapsack(benefits, size_cache, storage_budget,
                     partition_of=None, verbose=False):
    """Greedy 0-1 knapsack in decreasing benefit/size order."""
    partition_of = partition_of or {idx: idx for idx in benefits}
    selected = []
    used_partitions = set()
    used_storage = 0.0

    ranked = sorted(
        (idx for idx in benefits if benefits[idx] > 0),
        key=lambda idx: benefits[idx] / max(size_cache.get(idx, 1e-9), 1e-9),
        reverse=True,
    )

    for idx in ranked:
        size = size_cache.get(idx, 0.0)
        partition = partition_of.get(idx, idx)
        if partition in used_partitions:
            continue
        if used_storage + size > storage_budget:
            continue
        selected.append(idx)
        used_partitions.add(partition)
        used_storage += size

    if verbose:
        print(
            f"  [BEN_KNAP] knapsack selected {len(selected)} indexes  "
            f"size={_mb(used_storage):,.1f} MB  "
            f"of {_mb(storage_budget):,.1f} MB"
        )

    return frozenset(selected)


def approximationGuarantee(k_max: float) -> float:
    return 2 * (k_max ** 2)


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------

def _ben_knap_inner(conn, W, candidate_indexes, size_cache, cost_cache,
                    storage_budgets_bytes, partition_of=None, query_weights=None, verbose=False):
    if verbose:
        print(
            f"  [BEN_KNAP] Phase 1 — benefit assignment  "
            f"|W|={len(W)}  |candidates|={len(candidate_indexes)}"
        )
    total_benefit, k_max = _assign_workload_benefits(
        conn, W, candidate_indexes, cost_cache, query_weights=query_weights, verbose=verbose
    )
    if verbose:
        print(
            f"  [BEN_KNAP] Phase 1 done  K_max={k_max:.3f}  "
            f"approx. guarantee={approximationGuarantee(k_max):.3f}"
        )

    configs = {}
    for budget in storage_budgets_bytes:
        if verbose:
            print(f"\n=== BEN_KNAP  budget={_mb(budget):,.0f} MB ===")
        configs[budget] = _greedy_knapsack(
            total_benefit, size_cache, budget,
            partition_of=partition_of, verbose=verbose,
        )

    return configs, k_max


def benKnap(
    conn, W, candidate_dict, storage_budget,
    size_fn: Optional[Callable] = None,
    cost_cache=None, query_weights=None, write_penalties=None,
    partition_of=None, verbose=False, **kwargs
):
    """BEN_KNAP configuration selection."""
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)

    if size_fn is not None:
        size_cache = {idx: size_fn(idx) for idx in candidate_indexes}
    else:
        size_cache = buildSizeMap(conn, candidate_indexes)

    configs, k_max = _ben_knap_inner(
        conn, W, candidate_indexes, size_cache, cost_cache,
        storage_budgets_bytes=[int(storage_budget)],
        partition_of=partition_of, query_weights=query_weights, verbose=verbose,
    )

    return configs[int(storage_budget)], k_max


def selectConfiguration(
    conn, W, candidate_dict, storage_budget,
    cost_cache=None, size_cache=None,
    query_weights=None, write_penalties=None,
    partition_of=None, verbose=True, **kwargs,
):
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    configs, _k_max = _ben_knap_inner(
        conn, W, candidate_indexes, size_cache, cost_cache,
        storage_budgets_bytes=[int(storage_budget)],
        partition_of=partition_of, query_weights=query_weights, verbose=verbose,
    )

    return configs[int(storage_budget)]


def selectConfigurations(
    conn, W, candidate_dict, storage_budgets_mb,
    cost_cache=None, size_cache=None,
    query_weights=None, write_penalties=None,
    verbose=True, partition_of=None, **kwargs,
):
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    budgets_bytes = sorted(
        [int(b * 1024 * 1024) for b in storage_budgets_mb], reverse=True
    )

    if verbose:
        print(
            f"  [BEN_KNAP] {len(candidate_indexes)} candidate indexes  "
            f"{len(budgets_bytes)} budgets  "
            f"({_mb(min(budgets_bytes)):,.0f}–{_mb(max(budgets_bytes)):,.0f} MB)"
        )

    configs, k_max = _ben_knap_inner(
        conn, W, candidate_indexes, size_cache, cost_cache,
        storage_budgets_bytes=budgets_bytes,
        partition_of=partition_of, query_weights=query_weights, verbose=verbose,
    )

    if verbose:
        print(
            f"\n  [BEN_KNAP] done  K_max={k_max:.3f}  "
            f"approx. guarantee={approximationGuarantee(k_max):.3f}"
        )

    return configs
