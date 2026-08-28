from itertools import combinations
import logging
import psycopg2
from ..CostEstimator.costEstimator import estimateWorkloadCostForConfig
from .config_sel import flattenCandidateIndexes, sortedConfig

logger = logging.getLogger(__name__)

#####################################
# DROP HEURISTIC CONFIGURATION ENUM  #
#####################################
#
# Whang (1985), "Index Selection in Relational Databases",
# Proc. Intl. Conf. on Foundations of Data Organization, Kyoto, pp. 369-378.
#
# Candidate index representation: (table: str, columns: tuple[str, ...])


def estimateIndexSize(conn, table, columns):
    """
    Estimated on-disk size of a B-tree index on table(columns), in BYTES.

    Preferred path: ask HypoPG, which models PostgreSQL's own index layout.
    Fallback path : estimate from pg_class.reltuples and pg_stats.avg_width.
    """
    col_list = ", ".join(columns)
    cur = conn.cursor()

    # --- preferred: HypoPG's own size model ---
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

    # --- fallback: catalog statistics ---
    cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
    row = cur.fetchone()
    n_rows = float(row[0]) if row and row[0] and row[0] > 0 else 1.0

    entry_bytes = 12.0  # tuple header (8) + item pointer (4)
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


def dropHeuristic(conn, W, candidate_dict, storage_budget=float('inf'),
                  budget_mb=None, max_group=2, cost_cache=None, size_cache=None,
                  write_penalties=None, query_weights=None, verbose=False, **kwargs):
    """
    DROP heuristic (Whang 1985, Algorithm 1), adapted to storage budget,
    write penalties, and execution frequency weights.

    Parameters
    ----------
    conn           : psycopg2 connection
    W              : list[str] SQL queries (the workload)
    candidate_dict : dict(table -> list[list[str]])
    storage_budget : maximum total index size in BYTES (default: infinity)
    budget_mb      : maximum total index size in MEGABYTES (optional convenience)
    max_group      : largest group size to try removing at once (default: 2)
    write_penalties: dict optional {(table, (col,...)): penalty_float}
    query_weights  : dict optional {query_str: call_count_float}
    """
    if cost_cache is None:
        cost_cache = {}

    if budget_mb is not None:
        storage_budget = float(budget_mb) * 1024.0 * 1024.0 if budget_mb != float("inf") else float("inf")
    elif storage_budget is not None:
        try:
            storage_budget = float(storage_budget)
        except (ValueError, TypeError):
            storage_budget = float("inf")
    else:
        storage_budget = float("inf")

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    def cost(config):
        key = frozenset(config)
        if key not in cost_cache:
            read_cost = estimateWorkloadCostForConfig(conn, W, key, query_weights=query_weights)
            write_cost = 0.0
            if write_penalties:
                for table, cols in key:
                    write_cost += write_penalties.get((table, tuple(cols)), 0.0)
            cost_cache[key] = read_cost + write_cost
        return cost_cache[key]

    def size(config):
        return sum(size_cache[idx] for idx in config)

    max_group = max(1, min(max_group, len(candidate_indexes))) if candidate_indexes else 1

    def mb(x):
        return x / (1024.0 ** 2)

    # --- Step 1: Start from the FULL candidate set ---
    S = frozenset(candidate_indexes)

    if verbose and S:
        print(f"  START |S|={len(S)}  size={mb(size(S)):,.1f} MB  "
              f"budget={mb(storage_budget):,.1f} MB  cost={cost(S):,.2f}")

    # --- Step 2: Forced reduction until within storage_budget ---
    while size(S) > storage_budget:
        current_cost = cost(S)
        current_size = size(S)

        best_config = None
        best_ratio = float("inf")

        eff_max_group = 1 if len(S) > 30 else max_group
        for group_size in range(1, eff_max_group + 1):
            for group in combinations(sorted(S), group_size):
                trial = S - frozenset(group)
                bytes_freed = current_size - size(trial)
                if bytes_freed <= 0:
                    continue
                ratio = (cost(trial) - current_cost) / bytes_freed
                if ratio < best_ratio:
                    best_ratio = ratio
                    best_config = trial

        if best_config is None:
            break

        removed = sorted(S - best_config)
        S = best_config
        if verbose:
            print(f"    [budget] drop {removed} -> |S|={len(S)}  "
                  f"size={mb(size(S)):,.1f} MB  cost={cost(S):,.2f}")

    # --- Step 3: Keep dropping while it strictly reduces total cost ---
    for group_size in range(1, max_group + 1):
        if len(S) > 30 and group_size > 1:
            break
        while True:
            if len(S) < group_size:
                break

            current_cost = cost(S)
            best_config = None
            best_cost = current_cost

            for group in combinations(sorted(S), group_size):
                trial = S - frozenset(group)
                trial_cost = cost(trial)
                if trial_cost < best_cost:
                    best_cost = trial_cost
                    best_config = trial

            if best_config is None:
                if verbose:
                    print(f"    no improvement dropping {group_size} at a time")
                break

            removed = sorted(S - best_config)
            S = best_config
            if verbose:
                print(f"    drop {removed} -> |S|={len(S)}  "
                      f"size={mb(size(S)):,.1f} MB  cost={best_cost:,.2f}")

    if verbose and S:
        print(f"  RESULT |S|={len(S)}  size={mb(size(S)):,.1f} MB "
              f"of {mb(storage_budget):,.1f} MB  cost={cost(S):,.2f}")

    return S


def selectConfiguration(conn, W, candidate_dict, storage_budget=float('inf'),
                        max_group=2, cost_cache=None, size_cache=None,
                        write_penalties=None, query_weights=None, **kwargs):
    return dropHeuristic(conn, W, candidate_dict, storage_budget=storage_budget,
                         max_group=max_group, cost_cache=cost_cache, size_cache=size_cache,
                         write_penalties=write_penalties, query_weights=query_weights)


# Standard drop-in alias matching greedyMK signature
def greedyMK(conn, W, candidate_dict, m=2, k=10, cost_cache=None,
             write_penalties=None, query_weights=None, **kwargs):
    return selectConfiguration(conn, W, candidate_dict,
                               storage_budget=float('inf'),
                               max_group=m,
                               cost_cache=cost_cache,
                               write_penalties=write_penalties,
                               query_weights=query_weights)
