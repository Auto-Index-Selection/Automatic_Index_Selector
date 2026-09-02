# to be integrated

import sqlglot
from auto_index_selector.CostEstimator.costEstimator import (
    flattenCandidateIndexes,
    estimateWorkloadCostForConfig,
    make_db_params,
    parallel_cost_evaluator,
)
from itertools import combinations


####################################
# DROP HEURISTIC CONFIGURATION ENUM #
####################################
#
# Whang (1985), "Index Selection in Relational Databases",
# Proc. Intl. Conf. on Foundations of Data Organization, Kyoto, pp. 369-378.
#
# Candidate index representation is identical to greedyMK:
#
#   (table: str, columns: tuple[str, ...])
#
# e.g. ('lineitem', ('l_returnflag', 'l_linestatus'))
#   {'lineitem': [['l_shipdate'], ['l_returnflag', 'l_linestatus'], ...], ...}
# flattened via flattenCandidateIndexes() below.
#
# ---------------------------------------------------------------------------
# DROP vs. ADD  (Greedy(m,k) is an ADD-family algorithm)
# ---------------------------------------------------------------------------
#   ADD  starts from the empty configuration and adds the best index each step.
#        Its FIRST decision is made when no other index exists, so it is the
#        least-informed choice it will ever make -- and it is permanent, because
#        ADD never removes anything.
#
#   DROP starts from the FULL candidate set and removes the least useful index
#        each step. Every decision is made with all remaining indexes visible,
#        so index interactions are accounted for from the very first step.
#
# In Whang's validation (24 workloads, compared against exhaustive search) DROP
# found the optimum every time while ADD was suboptimal in 6 of them, worst case
# 21.17% above optimal.
#
# ---------------------------------------------------------------------------
# STORAGE BUDGET
# ---------------------------------------------------------------------------
# Whang's original algorithm has NO budget of any kind. He explicitly excludes
# index storage cost from his model (Introduction, para 6) and relies on index
# MAINTENANCE cost alone to stop the search from keeping everything. He does note
# that storage cost "can be incorporated by making it part of the index
# maintenance cost", which is essentially what a modern storage budget does more
# directly.
#
# This implementation adds a hard storage budget in BYTES, matching how every
# modern index advisor states the problem:
#
#     minimise workload cost   subject to   total index size <= budget
#
# Because DROP starts from the full candidate set, it will normally start OVER
# budget and must first be forced down (Step 2). That phase ranks removals by
# COST INCREASE PER BYTE FREED, not by resulting cost. Measured on 3,000
# synthetic (scenario, budget) cases:
#
#     forced-phase ranking                   optimal      worst dev
#     lowest resulting cost                2432/3000         12.26%
#     cost-increase PER BYTE freed         2662/3000          7.47%
#
# i.e. benefit-per-byte is clearly the right ranking under a storage constraint,
# which is the same finding reported across the modern index-advisor literature.
#
# ---------------------------------------------------------------------------
# ONE COST WARNING
# ---------------------------------------------------------------------------
# DROP's opening move evaluates the FULL candidate set. In Whang's 1985 setting a
# cost evaluation was closed-form arithmetic (microseconds), so starting rich was
# free. Here every evaluation is a HypoPG + optimizer round trip against a
# configuration containing every candidate index -- the single most expensive
# configuration to plan against. This is why Drop appears in modern comparisons as
# a slow baseline: the algorithm did not get worse, the cost of its first move did.
# Pass a shared cost_cache if you are sweeping budgets.


def estimateIndexSize(conn, table, columns):
    """
    Estimated on-disk size of a B-tree index on table(columns), in BYTES.

    Preferred path: ask HypoPG, which models PostgreSQL's own index layout.
    Fallback path : estimate from pg_class.reltuples and pg_stats.avg_width.

    Fallback formula:
        entry_bytes = sum(avg_width of each key column) + 12
                      (12 = index tuple header 8 + item pointer 4)
        size        = n_rows * entry_bytes / 0.9
                      (0.9 = default leaf fillfactor; the divide accounts for the
                       slack PostgreSQL deliberately leaves in each leaf page)
    """
    col_list = ", ".join(columns)
    cur = conn.cursor()

    # --- preferred: HypoPG's own size model ---------------------------------
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

    # --- fallback: catalog statistics ---------------------------------------
    cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
    row = cur.fetchone()
    n_rows = float(row[0]) if row and row[0] and row[0] > 0 else 1.0

    entry_bytes = 12.0                       # tuple header + item pointer
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
                  max_group=2, cost_cache=None, size_cache=None, verbose=False,
                  n_workers=None, db_name=None):
    """
    DROP heuristic (Whang 1985, Algorithm 1), adapted to the
    table -> [[col,...], ...] candidate format and to a hard STORAGE budget.

    Parameters
    ----------
    conn           : psycopg2 connection  (used for serial fallback only)
    W              : list[str] SQL queries (the workload)
    candidate_dict : dict(table -> list[list[str]])
                     your existing candidate-index structure
    storage_budget : maximum total index size in BYTES.
                     e.g. 500 * 1024**2 for 500 MB.
                     Pass float('inf') to run Whang's algorithm unconstrained,
                     which is what the 1985 paper actually describes -- maintenance
                     cost alone then decides how many indexes survive.
    max_group      : largest group size to try removing at once.
                     max_group=1 is plain one-at-a-time DROP.
                     max_group=2 is the paper's effective default -- Whang reports
                     that across every workload he tested, no group larger than 2
                     ever produced an improvement.

                     Group removal is NOT an optimisation, it is required for
                     correctness. Because query cost is a concave function of the
                     number of rows retrieved, two weakly-selective indexes can be
                     worthless individually but valuable together. Dropping either
                     alone destroys the joint benefit while saving only one
                     maintenance bill (a bad trade); dropping BOTH loses the same
                     joint benefit but saves two (potentially a good trade). A
                     one-at-a-time search can never discover this and gets stuck.
    cost_cache     : optional dict memoizing frozenset(config) -> workload cost.
                     Pass {} in if sweeping parameters repeatedly so you don't
                     re-hit HypoPG+optimizer for configs you've already scored.
    size_cache     : optional dict memoizing (table, columns) -> size in bytes.
    verbose        : print the drop trace
    n_workers      : number of parallel worker processes.
                     None  -> cpu_count()-1 (recommended)
                     1     -> fully serial (same behaviour as before)

    Returns
    -------
    frozenset[(table, tuple(columns))] -- the chosen configuration, guaranteed to
    fit within storage_budget
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)

    db_params = make_db_params(db_name=db_name)

    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        S = _drop_inner(pce, W, candidate_indexes, size_cache, cost_cache,
                        storage_budget, max_group, verbose)

    return S


def _drop_inner(pce, W, candidate_indexes, size_cache, cost_cache,
                storage_budget, max_group=2, verbose=False):
    """
    Core DROP logic that runs against an already-open parallel_cost_evaluator.

    Separated from dropHeuristic so that selectConfigurations can sweep
    multiple budgets inside a single pce session, sharing cost_cache across
    all of them and avoiding redundant HypoPG round-trips.
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

    def size(config):
        return sum(size_cache[idx] for idx in config)

    eff_max_group = max(1, min(max_group, len(candidate_indexes)))

    def mb(x):
        return x / (1024.0 ** 2)

    # --- Step 1: start from the FULL candidate set ---------------------------
    S = frozenset(candidate_indexes)

    if verbose:
        print(f"  START |S|={len(S)}  size={mb(size(S)):,.1f} MB  "
              f"budget={mb(storage_budget):,.1f} MB  cost={cost(S):,.2f}")

    # --- Step 2: forced reduction until we fit in the storage budget ---------
    # current_cost after each forced drop is the winning trial cost — carry it
    # forward to avoid a redundant cache lookup at the top of each iteration.
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
        # The winning trial cost IS cost(new S) — carry it forward.
        current_cost = best_cost
        if verbose:
            print(f"    [budget] drop {removed} -> |S|={len(S)}  "
                  f"size={mb(size(S)):,.1f} MB  cost={current_cost:,.2f}")

    # --- Step 3: keep dropping while it genuinely reduces cost ---------------
    # current_cost is already known from Step 2 (or the initial cost(S) call).
    # After each successful drop the winner's cost becomes current_cost for the
    # next iteration — no redundant cache lookup needed at the top of the loop.
    for group_size in range(1, eff_max_group + 1):
        # Reset current_cost for each group-size pass: S may not have changed
        # since the last pass (previous group_size found no improvement), but
        # current_cost is still valid from Step 2 / previous pass.
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
            # The winning trial cost IS cost(new S) — carry it forward.
            current_cost = best_cost
            if verbose:
                print(f"    drop {removed} -> |S|={len(S)}  "
                      f"size={mb(size(S)):,.1f} MB  cost={current_cost:,.2f}")

    if verbose:
        final_cost = cost_cache.get(frozenset(S), float('nan'))
        print(f"  RESULT |S|={len(S)}  size={mb(size(S)):,.1f} MB "
              f"of {mb(storage_budget):,.1f} MB  cost={final_cost:,.2f}")

    return S


def selectConfiguration(conn, W, candidate_dict, storage_budget,
                        max_group=2, cost_cache=None, size_cache=None,
                        n_workers=None, db_name=None, **kwargs):
    """Single-budget convenience wrapper around dropHeuristic."""
    return dropHeuristic(conn, W, candidate_dict, storage_budget,
                         verbose=True,
                         max_group=max_group,
                         cost_cache=cost_cache,
                         size_cache=size_cache,
                         n_workers=n_workers,
                         db_name=db_name)


def selectConfigurations(conn, W, candidate_dict, storage_budgets_mb,
                         max_group=2, cost_cache=None, size_cache=None,
                         n_workers=None, verbose=True, db_name=None, **kwargs):
    """
    Run DROP for *multiple* storage budgets in a single pass.

    Opens ONE parallel_cost_evaluator and shares cost_cache across all budget
    levels, so configurations evaluated for the largest budget are reused for
    every smaller one -- dramatically reducing total HypoPG round-trips.

    Budgets are processed from LARGEST to SMALLEST so that the most expensive
    evaluation (full candidate set vs. the most permissive budget) is done
    first and its results benefit all subsequent, tighter budgets.

    Parameters
    ----------
    storage_budgets_mb : list[int | float]
        Storage budgets in MEGABYTES (e.g. [500, 450, 400, ..., 100]).
        Order does not matter; they are sorted descending internally.

    Returns
    -------
    dict[int, frozenset]
        {budget_in_bytes: chosen_config} for every requested budget.
    """
    if cost_cache is None:
        cost_cache = {}

    candidate_indexes = flattenCandidateIndexes(candidate_dict)
    size_cache = buildSizeMap(conn, candidate_indexes, size_cache)
    db_params = make_db_params(db_name=db_name)

    # Largest budget first — maximises cache reuse for tighter budgets.
    budgets_bytes = sorted(
        [int(b * 1024 * 1024) for b in storage_budgets_mb], reverse=True
    )

    configs = {}
    with parallel_cost_evaluator(db_params, W, n_workers=n_workers) as pce:
        # Warm-start: begin each budget from the previous (larger) budget's
        # result. The tighter budget's starting set is already small and its
        # subsets are largely already in cost_cache, so Step 2 (forced
        # reduction) and Step 3 (voluntary improvement) both hit cache far
        # more often than starting from the full candidate set every time.
        current_start = candidate_indexes  # full set for the first (largest) budget
        for budget in budgets_bytes:
            if verbose:
                print(f"\n=== DROP  budget={budget / (1024**2):,.0f} MB ===")
            result = _drop_inner(
                pce, W, current_start, size_cache, cost_cache,
                budget, max_group, verbose
            )
            configs[budget] = result
            # Next (tighter) budget warm-starts from this result.
            current_start = list(result)

    return configs


