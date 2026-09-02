from typing import *
import multiprocessing as mp
import psycopg2
import sqlglot as sg
from ordered_set import OrderedSet
from auto_index_selector.CostEstimator.costEstimator import (
    make_db_params,
    clearHypotheticalIndexes,
    createHypoIndexesCS,
    getQueryCost,
)


# ---------------------------------------------------------------------------
# Query / schema parsing helpers (consistent with cg_dta & cg_rule_based)
# ---------------------------------------------------------------------------

def normalizeColumn(column: str, schema: Dict) -> str:
    """
    Input:
        column : string
        schema : Dict
    Output:
        result : table.column (empty string if not found)
    """
    result = ""
    col_lower = column.lower()
    for table, attrs in schema.items():
        attr_map = {k.lower(): k for k in attrs.keys()}
        if col_lower in attr_map:
            result = f"{table}.{attr_map[col_lower]}"
            break
    return result


def getJoinCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    try:
        parsed_query = sg.parse_one(q, read="postgres")
    except Exception:
        try:
            parsed_query = sg.parse_one(q)
        except Exception:
            return result

    for joins in parsed_query.find_all(sg.exp.Join):
        on_clause = joins.args.get("on")
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                normalized_col = normalizeColumn(column.name, schema)
                if normalized_col:
                    result.add(normalized_col)
    return result


def getEqCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    try:
        parsed_query = sg.parse_one(q, read="postgres")
    except Exception:
        try:
            parsed_query = sg.parse_one(q)
        except Exception:
            return result

    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for eq in where.find_all(sg.exp.EQ):
                for column in eq.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column.name, schema)
                    if normalized_col:
                        result.add(normalized_col)
    return result


def getRangeCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    try:
        parsed_query = sg.parse_one(q, read="postgres")
    except Exception:
        try:
            parsed_query = sg.parse_one(q)
        except Exception:
            return result

    range_operators = (sg.exp.GT, sg.exp.GTE, sg.exp.LT, sg.exp.LTE)
    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for r in where.find_all(range_operators):
                for column in r.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column.name, schema)
                    if normalized_col:
                        result.add(normalized_col)
    return result


def getOCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    try:
        parsed_query = sg.parse_one(q, read="postgres")
    except Exception:
        try:
            parsed_query = sg.parse_one(q)
        except Exception:
            return result

    group_order = (sg.exp.Group, sg.exp.Order)
    for clause in parsed_query.find_all(group_order):
        for column in clause.find_all(sg.exp.Column):
            normalized_col = normalizeColumn(column.name, schema)
            if normalized_col:
                result.add(normalized_col)
    return result


def singleIndexableColumnsIn(query: str, schema: Dict) -> List[str]:
    """
    Return admissible single-column indexes ('table.column' strings) for
    `query`, restricted to columns that appear in index-relevant clauses (WHERE,
    JOIN ON, GROUP BY, ORDER BY) and exist in `schema`.
    """
    cols: OrderedSet = OrderedSet()
    cols |= getJoinCols(query, schema)
    cols |= getEqCols(query, schema)
    cols |= getRangeCols(query, schema)
    cols |= getOCols(query, schema)
    return list(cols)


# ---------------------------------------------------------------------------
# Configuration enumeration (Greedy(m,k), see Figure 5 of AutoAdmin paper)
# ---------------------------------------------------------------------------

def _greedyExpand(conn, query: str, selected: List[str], remaining: List[str], k: int) -> List[str]:
    """
    Shared greedy-expansion step: repeatedly add whichever remaining index gives
    the largest positive cost reduction, until `k` indexes are selected or no
    remaining index helps.
    """
    selected = list(selected)
    remaining = list(remaining)

    clearHypotheticalIndexes(conn)
    current_best_cost = getQueryCost(conn, query)

    while len(selected) < k and remaining:
        best_idx = None
        best_cost = current_best_cost

        for idx in remaining:
            test_config = selected + [idx]
            clearHypotheticalIndexes(conn)
            createHypoIndexesCS(conn, test_config)
            cost_fin = getQueryCost(conn, query)

            if cost_fin < best_cost:
                best_cost = cost_fin
                best_idx = idx

        clearHypotheticalIndexes(conn)

        if best_idx is None or best_cost >= current_best_cost:
            break

        selected.append(best_idx)
        remaining.remove(best_idx)
        current_best_cost = best_cost

    return selected


def enumerateConfigs(conn, I: List[str], query: str) -> List[str]:
    """
    Enumerate(I, Q) with no bound on configuration size -- used to find
    the best configuration for a single query in the BEST-CONF algorithm.
    """
    return _greedyExpand(conn, query, selected=[], remaining=I, k=len(I))


def listToDict(candidateIndexes: List[str]) -> Dict[str, List[List[str]]]:
    """
    Turn a list of 'table.column' strings into {table: [[column], ...]}.
    Matches the candidate_dict structure used across all CG modules.
    """
    result: Dict[str, List[List[str]]] = {}
    for index in candidateIndexes:
        table, column = index.split('.', 1)
        if column.startswith('[') and column.endswith(']'):
            column = column[1:-1]
        cols = [c.strip() for c in column.split(',')]
        if table not in result:
            result[table] = []
        if cols not in result[table]:
            result[table].append(cols)
    return result


# ---------------------------------------------------------------------------
# Multiprocessing Worker for Parallel Best-Conf Evaluation
# ---------------------------------------------------------------------------

_auto_admin_worker_conn = None


def _init_auto_admin_worker(db_params: dict):
    global _auto_admin_worker_conn
    _auto_admin_worker_conn = psycopg2.connect(**db_params)
    _auto_admin_worker_conn.autocommit = True


def _eval_query_best_conf_task(args: Tuple[str, List[str], Optional[int]]) -> List[str]:
    query_sql, I, max_per_query = args
    conn = _auto_admin_worker_conn
    best_indexes = enumerateConfigs(conn, I, query_sql)
    if max_per_query is not None:
        best_indexes = best_indexes[:max_per_query]
    return best_indexes


# ---------------------------------------------------------------------------
# Candidate index selection (BEST-CONF, Section 4 / Figure 4)
# ---------------------------------------------------------------------------

def bestConf(conn, W: List, schema: Dict, max_per_query: Optional[int] = None,
             n_workers: Optional[int] = None, db_name: Optional[str] = None) -> Dict[str, List[List[str]]]:
    """
    Query-specific-best-configuration candidate index selection algorithm
    (Chaudhuri & Narasayya 1997, Figure 4) parallelized across workload queries.

    Input:
        W             -> workload, a list of SQL query strings (or query objects)
        schema        -> {table: {col: type, ...}}
        max_per_query -> optional cap on candidate indexes per query.
        n_workers     -> number of parallel worker processes.
        db_name       -> database name to connect to.

    Returns:
        candidateIndexes -> {table: [[column], ...]}
    """
    # Extract query tasks
    tasks: List[Tuple[str, List[str], Optional[int]]] = []
    for q in W:
        query_sql = getattr(q, "query", q)
        I = singleIndexableColumnsIn(query_sql, schema)
        if I:
            tasks.append((query_sql, I, max_per_query))

    if not tasks:
        return {}

    # Extract db_name from active conn if not provided
    if db_name is None:
        try:
            db_name = conn.info.dbname
        except Exception:
            try:
                db_name = conn.get_dsn_parameters().get("dbname")
            except Exception:
                db_name = None

    db_params = make_db_params(db_name=db_name)

    if n_workers is None:
        n_workers = max(1, mp.cpu_count() - 1)

    candidateIndexes: OrderedSet = OrderedSet()

    if n_workers > 1 and len(tasks) > 1:
        with mp.Pool(
            processes=min(n_workers, len(tasks)),
            initializer=_init_auto_admin_worker,
            initargs=(db_params,),
        ) as pool:
            results = pool.map(_eval_query_best_conf_task, tasks)
            for best_indexes in results:
                for idx in best_indexes:
                    candidateIndexes.add(idx)
    else:
        # Serial fallback
        for query_sql, I, max_pq in tasks:
            best_indexes = enumerateConfigs(conn, I, query_sql)
            if max_pq is not None:
                best_indexes = best_indexes[:max_pq]
            for idx in best_indexes:
                candidateIndexes.add(idx)

    return listToDict(list(candidateIndexes))


def generateCandidateIndexes(conn, W: List, schema: Dict, n_workers: Optional[int] = None,
                             db_name: Optional[str] = None, **kwargs) -> Dict[str, List[List[str]]]:
    """
    Input : 
        conn   -> database connection
        W      -> workload as a List of queries
        schema -> dict {table: {col: type}}
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: [[col], ...]}
    """
    print("AutoAdmin (Parallelized)")
    return bestConf(conn, W, schema, n_workers=n_workers, db_name=db_name)