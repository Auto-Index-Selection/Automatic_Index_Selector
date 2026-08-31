##################
# COST ESTIMATOR #
##################

import psycopg2

import threading
import time
import multiprocessing as mp
from tqdm import tqdm
from dotenv import load_dotenv

import os

# ------------------------------------------------------------------ #
# Reusable threaded progress bar helper (single-connection loops)
# ------------------------------------------------------------------ #
def run_with_progress(target_func, total, desc="Processing"):
    """
    Runs target_func(update_progress) while a separate thread renders a
    single tqdm progress bar driven by a shared counter.
    """
    progress = {"count": 0}
    lock = threading.Lock()
    done = threading.Event()

    def update_progress():
        with lock:
            progress["count"] += 1

    def monitor():
        with tqdm(total=total, desc=desc) as pbar:
            last = 0
            while not done.is_set():
                with lock:
                    current = progress["count"]
                if current > last:
                    pbar.update(current - last)
                    last = current
                if current >= total:
                    break
                time.sleep(0.05)
            with lock:
                current = progress["count"]
            if current > last:
                pbar.update(current - last)

    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()

    try:
        result = target_func(update_progress)
    finally:
        done.set()
        monitor_thread.join()

    return result


def make_db_params(db_name: str = None) -> dict:
    """
    Read DB connection parameters from the .env file and return them as a
    dict suitable for passing to psycopg2.connect(**db_params).

    Parameters
    ----------
    db_name : str, optional
        Override the database name.  Pass the DB_NAME returned by
        getWorkload() so worker processes connect to the correct database
        regardless of what DB_NAME is set to in .env.
    """
    load_dotenv()
    return dict(
        dbname=db_name if db_name is not None else os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST", "/var/run/postgresql"),
        port=os.getenv("DB_PORT"),
    )



def clearHypotheticalIndexes(conn):
    """
    Remove all HypoPG indexes.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT hypopg_reset();")

def createHypoIndex(conn, indexes, table):
    idxs = list()
    for index in indexes:
        idxs.append(f"CREATE INDEX ON {table}({index})")
    with conn.cursor() as cur:
        for index in idxs:
            cur.execute(
                f"SELECT * FROM hypopg_create_index('{index}');",
            )
            result = cur.fetchone()
    return result

def getSizeOfHypoIndex(conn, indexes, table, virtId):
    idxs = list()
    for index in indexes:
        idxs.append(f"CREATE INDEX ON {table}({index})")
    with conn.cursor() as cur:
        for index in idxs:
            cur.execute(
                f"SELECT (hypopg_relation_size('{virtId}')/(1024*1024)) AS size_in_mb;",
            )
            result = cur.fetchone()
    return result

def createHypoIndexes(conn, configuration, indexSet: dict):
    """
    configuration: list[str]
    """
    __config = list()
    for index in configuration:
        __index =  '['+','.join(index)+']'
        table = indexSet[__index][1]
        __config.append(f"CREATE INDEX ON {table}({__index[1:-1]})")

    # print(__config)

    with conn.cursor() as cur:
        for index in __config:
            cur.execute(
                f"SELECT * FROM hypopg_create_index('{index}');",
            )

def createHypoIndexesCS(conn, configuration):
    """
    configuration: list[str] table.index
    """
    __config = list()
    for index in configuration:
        table = index.split('.')[0]
        column = index.split('.')[1]
        __config.append(f"CREATE INDEX ON {table}({column})")
    with conn.cursor() as cur:
        for index in __config:
            cur.execute(
                f"SELECT * FROM hypopg_create_index('{index}');",
            )

def getQueryCost(conn, query):
    """
    Returns PostgreSQL optimizer cost.
    """
    explain_query = f"EXPLAIN (FORMAT JSON) {query}"
    with conn.cursor() as cur:
        cur.execute(explain_query)
        result = cur.fetchone()
    return float(result[0][0]["Plan"]["Total Cost"])


def estimateConfigurationCost(conn, query, configuration):
    clearHypotheticalIndexes(conn)
    cost_init = getQueryCost(conn, query)
    createHypoIndexesCS(conn, configuration)
    cost_fin = getQueryCost(conn, query)
    clearHypotheticalIndexes(conn)
    return cost_init, cost_fin


# ------------------------------------------------------------------ #
# Parallel cost_final computation (multiprocessing, one conn/process)
# ------------------------------------------------------------------ #
# NOTE: EXPLAIN (without ANALYZE) never executes the query, so this is
# safe to run massively concurrently. Each worker opens its OWN
# connection; HypoPG hypothetical indexes are session-scoped, so
# workers never see each other's hypothetical index state.
# Correctness is unaffected - this is still full brute force, just
# distributed across processes.

_worker_conn = None
_worker_indexSet = None


def _init_worker(db_params, indexSet):
    """
    Runs once per worker process. Opens a dedicated connection so that
    workers never share a psycopg2 connection object (unsafe).
    """
    global _worker_conn, _worker_indexSet
    _worker_conn = psycopg2.connect(**db_params)
    _worker_conn.autocommit = True  # skip commit() round trips; hypopg
                                     # indexes are never actually persisted
    _worker_indexSet = indexSet


def _eval_init_query(args):
    """
    Evaluate ONE query's cost with no hypothetical indexes at all
    (cost_init). Worker connection starts clean (cleared in
    _init_worker), so no per-query clear is needed here.
    """
    i, query = args
    conn = _worker_conn
    cost = getQueryCost(conn, query)
    return i, cost


def _eval_config(args):
    """
    Evaluate ONE configuration against the full query workload.
    Index creation happens once per config (not once per config*query).
    """
    k, config, W = args
    conn = _worker_conn
    clearHypotheticalIndexes(conn)
    createHypoIndexes(conn, config, _worker_indexSet)
    results = {}
    for i, query in enumerate(W, start=1):
        results[(i, k)] = getQueryCost(conn, query)
    clearHypotheticalIndexes(conn)
    return k, results


# ------------------------------------------------------------------ #
# Lightweight per-config worker used by cs_greedy / cs_drop
# ------------------------------------------------------------------ #
# Each task is (config_key, W) where:
#   config_key : frozenset[(table, tuple[str, ...])]
#   W          : list[str]  -- the SQL workload
#
# Returns (config_key, total_workload_cost).
# HypoPG indexes are session-scoped so each worker process has its own
# isolated hypothetical-index state; no locking needed.

def _init_worker_simple(db_params):
    """Worker initializer for parallel_cost_evaluator (no indexSet needed)."""
    global _worker_conn
    _worker_conn = psycopg2.connect(**db_params)
    _worker_conn.autocommit = True


def _eval_config_for_greedy(args):
    """
    Evaluate the total workload cost for ONE frozenset configuration.
    Returns (config_key, total_cost).
    """
    config_key, W = args
    conn = _worker_conn
    clearHypotheticalIndexes(conn)
    if config_key:
        createCompositeHypoIndexes(conn, config_key)
    total = sum(getQueryCost(conn, q) for q in W)
    clearHypotheticalIndexes(conn)
    return config_key, total


class parallel_cost_evaluator:
    """
    Context manager that opens a multiprocessing.Pool once and exposes a
    ``batch_cost`` method to evaluate a list of frozenset configs in parallel.

    Usage::

        with parallel_cost_evaluator(db_params, W, n_workers=8) as pce:
            results = pce.batch_cost([frozenset(...), ...])
            # results: dict[frozenset -> float]

    Falls back to serial evaluation when n_workers <= 1.
    """

    def __init__(self, db_params: dict, W: list, n_workers: int = None):
        self._db_params = db_params
        self._W = W
        if n_workers is None:
            n_workers = max(1, mp.cpu_count() - 1)
        self._n_workers = n_workers
        self._pool = None

    def __enter__(self):
        if self._n_workers > 1:
            self._pool = mp.Pool(
                processes=self._n_workers,
                initializer=_init_worker_simple,
                initargs=(self._db_params,),
            )
        return self

    def __exit__(self, *_):
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    def batch_cost(self, configs: list) -> dict:
        """
        Evaluate every frozenset config in ``configs`` and return a dict
        mapping each config to its total workload cost.

        Already-cached configs should be filtered out by the caller before
        passing them here.
        """
        if not configs:
            return {}

        results = {}
        if self._pool is not None:
            tasks = [(cfg, self._W) for cfg in configs]
            for cfg_key, cost in self._pool.imap_unordered(_eval_config_for_greedy, tasks):
                results[cfg_key] = cost
        else:
            # serial fallback (n_workers=1 or pool failed to open)
            for cfg in configs:
                _, cost = _eval_config_for_greedy((cfg, self._W))
                results[cfg] = cost

        return results


def estimateCostInitParallel(pool, W):
    """
    Parallel version of cost_init - splits the query list across the
    already-open worker pool. One task per query.

    Returns cost_init : dict[query_i] -> cost
    """
    tasks = [(i, query) for i, query in enumerate(W, start=1)]
    cost_init = {}
    with tqdm(total=len(W), desc="Computing initial costs (parallel)") as pbar:
        for i, cost in pool.imap_unordered(_eval_init_query, tasks):
            cost_init[i] = cost
            pbar.update(1)
    return cost_init


def estimateCostFinalParallel(pool, W, configurations):
    """
    Parallel, brute-force cost_final. Evaluates EVERY configuration in
    `configurations` against every query in W on the already-open
    worker pool - nothing is pruned or approximated, just distributed
    across worker processes.

    Returns cost_final : dict[(query_i, config_k)] -> cost
    """
    total_units = len(W) * len(configurations)
    tasks = [(k, config, W) for k, config in enumerate(configurations, start=1)]

    cost_final = {}
    with tqdm(total=total_units, desc="Computing final costs (parallel)") as pbar:
        for k, results in pool.imap_unordered(_eval_config, tasks):
            cost_final.update(results)
            pbar.update(len(results))

    return cost_final


def estimateWorkloadCost(conn, W, configurations, indexSet, db_params=None, n_workers=16):
    """
    Return : cost_init  = dict(query_i -> cost with no indexes)
             cost_final = dict((query_i, config_k) -> cost)

    If db_params is given, BOTH cost_init and cost_final run in
    parallel on a single shared worker pool (opened once, reused for
    both phases - avoids paying pool-startup cost twice). Each worker
    process gets its own dedicated connection built from db_params.

    Falls back to fully serial (using the single `conn` passed in) if
    db_params is None.
    """
    db_params = make_db_params()
    if db_params is not None:
        if n_workers is None:
            n_workers = max(1, mp.cpu_count() - 1)
        with mp.Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(db_params, indexSet),
        ) as pool:
            cost_init = estimateCostInitParallel(pool, W)
            cost_final = estimateCostFinalParallel(pool, W, configurations)
        return cost_init, cost_final

    # ---------------------- fully serial fallback ---------------------- #
    clearHypotheticalIndexes(conn)

    cost_init = dict()
    total_queries = len(W)

    def compute_cost_init(update_progress):
        i = 1
        for query in W:
            cost_init[i] = getQueryCost(conn, query)
            i += 1
            update_progress()
        return cost_init

    run_with_progress(compute_cost_init, total_queries, desc="Computing initial costs")

    # serial fallback, index created once per config (not per query)
    cost_final = dict()
    total_final = len(W) * len(configurations)
    # print(indexSet)
    def compute_cost_final(update_progress):
        for k, config in enumerate(configurations, start=1):
            clearHypotheticalIndexes(conn)
            createHypoIndexes(conn, config, indexSet)
            for i, query in enumerate(W, start=1):
                cost_final[i, k] = getQueryCost(conn, query)
                update_progress()
        return cost_final

    run_with_progress(compute_cost_final, total_final, desc="Computing final costs (serial)")

    clearHypotheticalIndexes(conn)
    return cost_init, cost_final


## This needs to be implemented
def estimateWorkloadCostUpdate(conn, W, configurations):
    return dict()

def createHypoIndexStorage(conn, indexes, table):
    idxs = list()
    for index in indexes:
        idxs.append(f"CREATE INDEX ON {table}({index})")
    with conn.cursor() as cur:
        for index in idxs:
            cur.execute(
                f"SELECT * FROM hypopg_create_index('{index}');",
            )
            result = cur.fetchone()
    return result

def storageEstimate(conn, indexSet):
    """
    Returns : dict(index_num: storage)
    """
    storage = dict()
    clearHypotheticalIndexes(conn)
    total_indexes = len(indexSet)

    def compute_storage(update_progress):
        for index, val in indexSet.items():
            clearHypotheticalIndexes(conn)
            temp = index[1:-1].split(',')
            result = createHypoIndexStorage(conn, temp, val[1])
            result_size = getSizeOfHypoIndex(conn, index, val[1], result[0])
            storage[val[0]] = result_size[0]
            update_progress()
        return storage

    run_with_progress(compute_storage, total_indexes, desc="Estimating index storage")

    clearHypotheticalIndexes(conn)
    return storage

def flattenCandidateIndexes(candidate_dict):
    flat = []
    for table, col_lists in candidate_dict.items():
        for cols in col_lists:
            flat.append((table, tuple(cols)))
    return flat


def createCompositeHypoIndexes(conn, configuration):
    with conn.cursor() as cur:
        for table, cols in configuration:
            col_list = ",".join(cols)
            stmt = f"CREATE INDEX ON {table}({col_list})"
            # print(stmt)
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (stmt,))


def estimateWorkloadCostForConfig(conn, W, configuration):
    clearHypotheticalIndexes(conn)
    if configuration:
        createCompositeHypoIndexes(conn, configuration)
    total = 0.0
    for query in W:
        total += getQueryCost(conn, query)
    clearHypotheticalIndexes(conn)
    return total


if __name__ == "__main__":
    # Example usage:
    #
    # db_params = make_db_params()
    # conn = psycopg2.connect(**db_params)
    # conn.autocommit = True
    #
    # # Parallel batch evaluation (cs_greedy / cs_drop style):
    # with parallel_cost_evaluator(db_params, W, n_workers=8) as pce:
    #     results = pce.batch_cost([frozenset(...), frozenset(...)])
    #
    # # Full ConfigEnumeration-style parallel cost:
    # cost_init, cost_final = estimateWorkloadCost(
    #     conn, W, configurations, indexSet,
    #     n_workers=8,
    # )
    pass