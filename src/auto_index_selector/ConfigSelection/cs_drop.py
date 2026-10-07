import sqlglot
from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)
from .config_sel import flattenCandidateIndexes
from itertools import combinations

def estimateIndexSize(conn, table, columns):
    """
    Estimated on-disk size of a B-tree index on table(columns), in BYTES.
    """
    col_list = ", ".join(columns)
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT hypopg_relation_size(indexrelid) "
            "FROM hypopg_create_index(%s)",
            (f"CREATE INDEX ON {table} ({col_list})",),
        )
        row = cur.fetchone()
        cur.execute("SELECT hypopg_reset()")
        if row and row[0]:
            return float(row[0])
    except Exception:
        conn.rollback()

    cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
    row = cur.fetchone()
    n_rows = float(row[0]) if row and row[0] and row[0] > 0 else 1.0

    entry_bytes = 12.0
    for col in columns:
        cur.execute(
            "SELECT avg_width FROM pg_stats WHERE tablename = %s AND attname = %s",
            (table, col),
        )
        row = cur.fetchone()
        entry_bytes += float(row[0]) if row and row[0] else 8.0

    return n_rows * entry_bytes / 0.9


def buildSizeMap(conn, candidate_indexes, size_cache=None):
    """Return {(table, columns) -> size in bytes} for every candidate index."""
    if size_cache is None:
        size_cache = {}
    for idx in candidate_indexes:
        if idx not in size_cache:
            table, columns = idx
            size_cache[idx] = estimateIndexSize(conn, table, columns)
    return size_cache


def dropHeuristic(conn, W, candidate_dict, storage_budget,
                  max_group=2, cost_cache=None, size_cache=None,
                  write_penalties=None, query_weights=None, verbose=False):
    """
    DROP heuristic (Whang 1985, Algorithm 1), adapted to the
    table -> [[col,...], ...] candidate format and to a hard STORAGE budget.
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    S = _drop_inner(conn, W, candidate_indexes, size_cache, cost_cache,
                    storage_budget, max_group, write_penalties, query_weights, verbose)

    return S


def _drop_inner(conn, W, candidate_indexes, size_cache, cost_cache,
                storage_budget, max_group=2, write_penalties=None, query_weights=None, verbose=False):
    """
    Core DROP logic.
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
        results = {}
        for c in configs:
            results[c] = cost(c)
        return results

    def size(config):
        return sum(size_cache[idx] for idx in config)

    eff_max_group = max(1, min(max_group, len(candidate_indexes)))
    if len(candidate_indexes) > 30 and eff_max_group > 1:
        eff_max_group = 1

    def mb(x):
        return x / (1024.0 ** 2)

    # --- Step 1: start from the FULL candidate set ---------------------------
    S = frozenset(candidate_indexes)

    if verbose and S:
        print(f"  START |S|={len(S)}  size={mb(size(S)):,.1f} MB  "
              f"budget={mb(storage_budget):,.1f} MB  cost={cost(S):,.2f}")

    # --- Step 2: forced reduction until we fit in the storage budget ---------
    current_cost = cost(S)

    while size(S) > storage_budget:
        current_size = size(S)

        trials = {}
        for group_size in range(1, eff_max_group + 1):
            for group in combinations(sorted(S), group_size):
                trial = S - frozenset(group)
                bytes_freed = current_size - size(trial)
                if bytes_freed <= 0:
                    continue
                trials[trial] = bytes_freed

        if not trials:
            break

        results = batch_cost_cached(list(trials.keys()))

        best_config = None
        best_cost = float("inf")
        best_ratio = float("inf")
        for trial, trial_cost in results.items():
            bytes_freed = trials[trial]
            ratio = (trial_cost - current_cost) / bytes_freed
            if ratio < best_ratio:
                best_ratio = ratio
                best_cost = trial_cost
                best_config = trial

        if best_config is None:
            break

        removed = sorted(S - best_config)
        S = best_config
        current_cost = best_cost
        if verbose:
            print(f"    [budget] drop {removed} -> |S|={len(S)}  "
                  f"size={mb(size(S)):,.1f} MB  cost={current_cost:,.2f}")

    # --- Step 3: keep dropping while it genuinely reduces cost ---------------
    for group_size in range(1, eff_max_group + 1):
        while True:
            if len(S) < group_size:
                break

            trial_to_group = {
                S - frozenset(group): group
                for group in combinations(sorted(S), group_size)
            }
            results = batch_cost_cached(list(trial_to_group.keys()))

            best_config = None
            best_cost = current_cost

            for trial, trial_cost in results.items():
                if trial_cost < best_cost:
                    best_cost = trial_cost
                    best_config = trial

            if best_config is None:
                if verbose:
                    print(f"    no improvement dropping {group_size} at a time")
                break

            removed = sorted(S - best_config)
            S = best_config
            current_cost = best_cost
            if verbose:
                print(f"    drop {removed} -> |S|={len(S)}  "
                      f"size={mb(size(S)):,.1f} MB  cost={current_cost:,.2f}")

    if verbose and S:
        final_cost = cost_cache.get(frozenset(S), float('nan'))
        print(f"  RESULT |S|={len(S)}  size={mb(size(S)):,.1f} MB "
              f"of {mb(storage_budget):,.1f} MB  cost={final_cost:,.2f}")

    return S


def selectConfiguration(conn, W, candidate_dict, storage_budget,
                        max_group=2, cost_cache=None, size_cache=None,
                        write_penalties=None, query_weights=None, **kwargs):
    """Single-budget convenience wrapper around dropHeuristic."""
    return dropHeuristic(conn, W, candidate_dict, storage_budget,
                         verbose=True,
                         max_group=max_group,
                         cost_cache=cost_cache,
                         size_cache=size_cache,
                         write_penalties=write_penalties,
                         query_weights=query_weights)


def selectConfigurations(conn, W, candidate_dict, storage_budgets_mb,
                         max_group=2, cost_cache=None, size_cache=None,
                         write_penalties=None, query_weights=None, verbose=True, **kwargs):
    """
    Run DROP for *multiple* storage budgets in a single pass.
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    budgets_bytes = sorted(
        [int(b * 1024 * 1024) for b in storage_budgets_mb], reverse=True
    )

    configs = {}
    current_start = candidate_indexes
    for budget in budgets_bytes:
        if verbose:
            print(f"\n=== DROP  budget={budget / (1024**2):,.0f} MB ===")
        result = _drop_inner(
            conn, W, current_start, size_cache, cost_cache,
            budget, max_group, write_penalties, query_weights, verbose
        )
        configs[budget] = result
        current_start = list(result)

    return configs
