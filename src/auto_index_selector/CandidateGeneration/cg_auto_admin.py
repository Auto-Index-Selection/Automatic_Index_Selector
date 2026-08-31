from typing import *
import sqlglot as sg
from ordered_set import OrderedSet
from auto_index_selector.CostEstimator.costEstimator import estimateConfigurationCost
from itertools import combinations


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

    while len(selected) < k and remaining:
        best_idx = None
        best_benefit = 0.0

        for idx in remaining:
            test_config = selected + [idx]
            cost_init, cost_fin = estimateConfigurationCost(conn, query, test_config)
            benefit = cost_init - cost_fin

            if benefit > best_benefit:
                best_benefit = benefit
                best_idx = idx

        if best_idx is None or best_benefit <= 0:
            break

        selected.append(best_idx)
        remaining.remove(best_idx)

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
# Candidate index selection (BEST-CONF, Section 4 / Figure 4)
# ---------------------------------------------------------------------------

def bestConf(conn, W: List, schema: Dict, max_per_query: Optional[int] = None) -> Dict[str, List[List[str]]]:
    """
    Query-specific-best-configuration candidate index selection algorithm
    (Chaudhuri & Narasayya 1997, Figure 4).

    Input:
        W             -> workload, a list of SQL query strings (or query objects)
        schema        -> {table: {col: type, ...}}
        max_per_query -> optional cap on how many candidate indexes to keep per query.

    Returns:
        candidateIndexes -> {table: [[column], ...]}
    """
    candidateIndexes: OrderedSet = OrderedSet()

    for q in W:
        query_sql = getattr(q, "query", q)
        I = singleIndexableColumnsIn(query_sql, schema)
        if not I:
            continue
        bestIndexesForQuery = enumerateConfigs(conn, I, query_sql)

        if max_per_query is not None:
            bestIndexesForQuery = bestIndexesForQuery[:max_per_query]

        for idx in bestIndexesForQuery:
            candidateIndexes.add(idx)

    return listToDict(list(candidateIndexes))


def generateCandidateIndexes(conn, W: List, schema: Dict) -> Dict[str, List[List[str]]]:
    """
    Input : 
        conn   -> database connection
        W      -> workload as a List of queries
        schema -> dict {table: {col: type}}
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: [[col], ...]}
    """
    print("AutoAdmin")
    return bestConf(conn, W, schema)