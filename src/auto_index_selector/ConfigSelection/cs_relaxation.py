"""
cs_relaxation.py — Configuration Selection phase for the relaxation-based
physical design approach of Bruno & Chaudhuri (SIGMOD 2005). Implements
the generic search strategy of the paper's Figure 5: start from the
oversized "optimal" configuration produced by cg_relaxation.py and
repeatedly relax it — merge, prefix, or remove indexes — until it fits
the space budget, tracking the best-cost configuration seen along the
way.

Three of the paper's five index transformations (Section 3.1.1) are
implemented: Merging, Prefixing, and Removal. Splitting and promotion-
to-clustered are left out of scope: they only pay off once the
optimizer is actually choosing between index-intersection plans or a
native clustered-index feature, and this codebase's HypoPG-based cost
oracle doesn't model either — matching how the rest of this project
documents implementation-honesty limitations rather than silently
approximating them.

Cost and space are both computed directly (a real HypoPG/EXPLAIN call
per candidate configuration, and a page-count estimate from pg_stats/
pg_class) rather than via the paper's optimizer-internal upper bounds
(Section 3.3), since we don't have the plan introspection those bounds
are derived from.
"""

import time
from itertools import combinations
from typing import Dict, FrozenSet, List, Optional

from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)
from .cg_relaxation import Index


####################################
# COST ESTIMATION                  #
####################################

def _cost(conn, W, config: FrozenSet[Index], cost_cache: Dict,
          query_weights=None, write_penalties=None) -> float:
    if config not in cost_cache:
        candidate = frozenset(i.asCandidateTuple() for i in config)
        cost_cache[config] = estimateWorkloadCostForConfig(
            conn, W, candidate, query_weights=query_weights, write_penalties=write_penalties
        )
    return cost_cache[config]


####################################
# SPACE ESTIMATION (Sec 3.3.1)     #
####################################

PAGE_SIZE_BYTES = 8192.0


def _columnWidth(conn, table: str, column: str, width_cache: Dict) -> float:
    key = (table, column)
    if key in width_cache:
        return width_cache[key]
    width = 8.0  # fixed-width fallback
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT avg_width FROM pg_stats WHERE tablename = %s AND attname = %s",
                (table, column),
            )
            row = cur.fetchone()
            if row and row[0]:
                width = float(row[0])
    except Exception:
        pass
    width_cache[key] = width
    return width


def _rowCount(conn, table: str, rowcount_cache: Dict) -> float:
    if table in rowcount_cache:
        return rowcount_cache[table]
    rows = 1_000_000.0  # conservative fallback — unknown tables shouldn't look free to index
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
            row = cur.fetchone()
            if row and row[0] and row[0] > 0:
                rows = float(row[0])
    except Exception:
        pass
    rowcount_cache[table] = rows
    return rows


def _indexSizeMB(conn, index: Index, rowcount_cache: Dict, width_cache: Dict, size_cache: Dict) -> float:
    """
    Simplified version of Section 3.3.1's leaf-page estimate: a single
    B-Tree level sized from average column widths and estimated row
    count, with a flat +15% overhead standing in for internal-node
    pages rather than modeling them level by level.
    """
    if index in size_cache:
        return size_cache[index]

    row_width = sum(_columnWidth(conn, index.table, c, width_cache) for c in index.columns()) + 8.0
    rows = _rowCount(conn, index.table, rowcount_cache)
    entries_per_page = max(1.0, PAGE_SIZE_BYTES / row_width)
    leaf_pages = rows / entries_per_page
    size_mb = (leaf_pages * 1.15 * PAGE_SIZE_BYTES) / (1024.0 * 1024.0)

    size_cache[index] = size_mb
    return size_mb


def _configSizeMB(conn, config: FrozenSet[Index], rowcount_cache: Dict, width_cache: Dict, size_cache: Dict) -> float:
    return sum(_indexSizeMB(conn, i, rowcount_cache, width_cache, size_cache) for i in config)


####################################
# INDEX TRANSFORMATIONS (Sec 3.1.1)#
####################################

def _mergeIndexes(i1: Index, i2: Index) -> Optional[Index]:
    """
    I1=(K1;S1), I2=(K2;S2). Default: I1,2 = (K1; (S1 ∪ K2 ∪ S2) − K1).
    If K1 is a prefix of K2 (the "minor improvement" case), instead
    I1,2 = (K2; (S1 ∪ S2) − K2).
    """
    if i1.table != i2.table or i1 == i2:
        return None
    k1, s1 = i1.key, set(i1.suffix)
    k2, s2 = i2.key, set(i2.suffix)

    if len(k1) <= len(k2) and k2[: len(k1)] == k1:
        merged_key, merged_suffix = k2, (s1 | s2) - set(k2)
    else:
        merged_key, merged_suffix = k1, (s1 | set(k2) | s2) - set(k1)

    return Index(i1.table, merged_key, tuple(sorted(merged_suffix)))


def _prefixIndex(index: Index, prefix_len: int) -> Optional[Index]:
    """IP = (K′, ∅) for a proper prefix K′ of K — suffix columns become
    reachable only via rid lookup rather than staying inline."""
    if prefix_len <= 0 or prefix_len >= len(index.key):
        return None
    return Index(index.table, index.key[:prefix_len], ())


def _candidateTransformations(config: FrozenSet[Index]) -> List[Dict]:
    """
    Enumerates the relaxations available from `config`: remove any
    index, prefix any multi-column index to a shorter one, or merge any
    same-table pair (tried in both orderings, since merging is
    asymmetric). Each entry carries an `apply` closure returning the
    relaxed configuration.
    """
    transforms: List[Dict] = []

    for index in config:
        transforms.append({
            "type": "remove", "targets": (index,),
            "apply": lambda c, idx=index: c - {idx},
        })
        for prefix_len in range(1, len(index.key)):
            prefixed = _prefixIndex(index, prefix_len)
            if prefixed and prefixed not in config:
                transforms.append({
                    "type": "prefix", "targets": (index,),
                    "apply": lambda c, idx=index, p=prefixed: (c - {idx}) | {p},
                })

    by_table: Dict[str, List[Index]] = {}
    for index in config:
        by_table.setdefault(index.table, []).append(index)

    for indexes in by_table.values():
        for i1, i2 in combinations(indexes, 2):
            for a, b in ((i1, i2), (i2, i1)):
                merged = _mergeIndexes(a, b)
                if merged and merged not in config:
                    transforms.append({
                        "type": "merge", "targets": (a, b),
                        "apply": lambda c, x=a, y=b, m=merged: (c - {x, y}) | {m},
                    })

    return transforms


####################################
# RELAXATION SEARCH (Sec 3.2 / 3.4)#
####################################

def relaxationSearch(
    conn,
    W: List[str],
    initial_config,
    space_budget_mb: float,
    cost_cache: Optional[Dict] = None,
    query_weights=None,
    write_penalties=None,
    max_iterations: int = 500,
    time_budget_seconds: Optional[float] = None,
    verbose: bool = False,
) -> FrozenSet[Index]:
    """
    Implements the Figure 5 template: relax C_best step by step,
    tracking the best-cost configuration that fits `space_budget_mb`,
    using the three-part heuristic from Section 3.4 to choose which
    configuration to relax next:
      1. keep relaxing the last-relaxed configuration while it's still
         over budget;
      2. otherwise walk back up the relaxation chain to whichever
         ancestor incurred the largest penalty and try correcting it;
      3. otherwise fall back to the cheapest configuration in the pool
         that still has an untried transformation.
    Each step picks, among the available transformations for the
    chosen configuration, the one with minimal penalty = ΔT / ΔS
    (Section 3.4's refined denominator: ΔS capped at the remaining
    distance to the budget once the configuration is already close).
    """
    if cost_cache is None:
        cost_cache = {}
    rowcount_cache: Dict = {}
    width_cache: Dict = {}
    size_cache: Dict = {}

    def cost(c):
        return _cost(conn, W, c, cost_cache, query_weights, write_penalties)

    def size(c):
        return _configSizeMB(conn, c, rowcount_cache, width_cache, size_cache)

    c_best = frozenset(initial_config)

    # chain[config] = {"parent": config | None, "penalty": float, "remaining": [transform, ...]}
    chain: Dict[FrozenSet[Index], Dict] = {
        c_best: {"parent": None, "penalty": float("-inf"), "remaining": _candidateTransformations(c_best)}
    }

    best_fit: Optional[FrozenSet[Index]] = None
    best_fit_cost = float("inf")
    if size(c_best) <= space_budget_mb:
        best_fit = c_best
        best_fit_cost = cost(c_best)

    last_relaxed = c_best
    start_time = time.time()

    for iteration in range(max_iterations):
        if time_budget_seconds is not None and (time.time() - start_time) > time_budget_seconds:
            if verbose:
                print(f"[Relax] time budget exceeded after {iteration} iterations")
            break

        # --- choose which configuration to relax next (Sec 3.4) ---
        candidate = None
        if size(last_relaxed) > space_budget_mb and chain[last_relaxed]["remaining"]:
            candidate = last_relaxed
        else:
            node = last_relaxed
            best_penalty = float("-inf")
            while node is not None:
                info = chain[node]
                if info["remaining"] and info["penalty"] > best_penalty:
                    best_penalty = info["penalty"]
                    candidate = node
                node = info["parent"]

        if candidate is None:
            open_configs = [c for c, info in chain.items() if info["remaining"]]
            if not open_configs:
                if verbose:
                    print(f"[Relax] no configurations left to relax after {iteration} iterations")
                break
            candidate = min(open_configs, key=cost)

        # --- pick the relaxing transformation with minimal penalty ---
        remaining = chain[candidate]["remaining"]
        base_cost = cost(candidate)
        base_size = size(candidate)

        best_transform = None
        best_penalty = float("inf")
        best_new_config = None
        best_new_cost = None
        best_new_size = None

        for transform in remaining:
            new_config = frozenset(transform["apply"](candidate))
            new_cost = cost(new_config)
            new_size = size(new_config)
            delta_t = new_cost - base_cost
            delta_s = base_size - new_size
            if delta_s <= 0:
                continue  # doesn't actually free space — not a useful relaxation here
            denom = min(base_size - space_budget_mb, delta_s)
            penalty = delta_t if denom <= 0 else delta_t / denom
            if penalty < best_penalty:
                best_penalty = penalty
                best_transform = transform
                best_new_config = new_config
                best_new_cost = new_cost
                best_new_size = new_size

        chain[candidate]["remaining"] = [t for t in remaining if t is not best_transform]

        if best_transform is None:
            continue  # nothing useful from this configuration right now — try another next round

        if best_new_config not in chain:
            chain[best_new_config] = {
                "parent": candidate,
                "penalty": best_penalty,
                "remaining": _candidateTransformations(best_new_config),
            }

        last_relaxed = best_new_config

        if verbose:
            print(f"  [Relax] iter {iteration}: {best_transform['type']} -> "
                  f"size={best_new_size:.1f}MB cost={best_new_cost:,.2f} penalty={best_penalty:,.3f}")

        if best_new_size <= space_budget_mb and best_new_cost < best_fit_cost:
            best_fit = best_new_config
            best_fit_cost = best_new_cost
            if verbose:
                print(f"  [Relax] new best-fit configuration: {len(best_fit)} indexes, "
                      f"{best_new_size:.1f}MB, cost={best_new_cost:,.2f}")

    if best_fit is None:
        # nothing ever fit the budget (e.g. a single required index
        # already exceeds it) — fall back to the smallest configuration seen
        best_fit = min(chain.keys(), key=size)
        if verbose:
            print("[Relax] no configuration fit the space budget; returning the smallest one found")

    return best_fit


def selectConfiguration(conn, W, candidate_indexes, space_budget_mb, cost_cache=None,
                         query_weights=None, write_penalties=None, **kwargs):
    """
    Entry point mirroring config_sel.py's `selectConfiguration`, so a
    benchmark driver can swap Greedy(m, k) for the relaxation-based
    search without branching. `candidate_indexes` is the C_best set
    (or any starting configuration) produced by
    cg_relaxation.generateOptimalConfiguration().
    """
    return relaxationSearch(
        conn, W, candidate_indexes, space_budget_mb,
        cost_cache=cost_cache, query_weights=query_weights, write_penalties=write_penalties,
        **kwargs
    )