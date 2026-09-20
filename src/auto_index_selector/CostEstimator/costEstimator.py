"""
CostEstimator/costEstimator.py
------------------------------
Core cost estimation module using HypoPG and the PostgreSQL optimizer.
Supports parameterized queries via psycopg2 argument passing.
"""

import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def clearHypotheticalIndexes(conn) -> None:
    """Remove all HypoPG hypothetical indexes."""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT hypopg_reset();")
    except Exception:
        if conn:
            conn.rollback()


def prepare_explain_query(query: str, params: Optional[Sequence[Any]] = None) -> Tuple[str, Tuple[Any, ...]]:
    """
    Format a query for EXPLAIN (FORMAT JSON) execution.

    If params are provided (e.g. from query_logger), replaces $1, $2, ...
    with %s and escapes existing % characters so psycopg2 safely binds
    the runtime values into the query plan.
    """
    if not params:
        return f"EXPLAIN (FORMAT JSON) {query}", ()

    param_map: Dict[int, Any] = {}
    for idx, p in enumerate(params, start=1):
        if hasattr(p, "position") and hasattr(p, "value"):
            param_map[p.position] = p.value
        elif isinstance(p, dict):
            pos = p.get("position", p.get("param", idx))
            val = p.get("value", p.get("val"))
            param_map[pos] = val
        else:
            param_map[idx] = p

    # Escape existing % to %% so psycopg2 parameter interpolation does not fail
    escaped_query = query.replace("%", "%%")

    # Replace $N with %s and collect matching values in order
    positions: List[int] = []
    converted_query = re.sub(
        r"\$(\d+)",
        lambda m: (positions.append(int(m.group(1))), "%s")[1],
        escaped_query,
    )

    values = tuple(param_map.get(pos, None) for pos in positions)
    return f"EXPLAIN (FORMAT JSON) {converted_query}", values


def getQueryCost(conn, query: str, fallback_cost: float = 1e9) -> float:
    """
    Returns PostgreSQL optimizer cost via EXPLAIN (FORMAT JSON).
    Supports parameterized queries (with attached .params or $N placeholders).
    If a query fails or times out, safely rolls back and returns fallback_cost.
    """
    params = getattr(query, "params", None)
    explain_sql, param_values = prepare_explain_query(query, params)

    try:
        with conn.cursor() as cur:
            if param_values:
                cur.execute(explain_sql, param_values)
            else:
                cur.execute(explain_sql)
            result = cur.fetchone()
            if result and result[0]:
                return float(result[0][0]["Plan"]["Total Cost"])
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("Query cost estimation failed or timed out: %s. Assigned fallback cost.", exc)
        return fallback_cost

    return fallback_cost


def createCompositeHypoIndexes(conn, configuration: Iterable[Tuple[str, Iterable[str]]]) -> None:
    """Create HypoPG hypothetical indexes for a list of (table, (columns...)) tuples."""
    with conn.cursor() as cur:
        for table, cols in configuration:
            col_list = ",".join(cols)
            stmt = f"CREATE INDEX ON {table}({col_list})"
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (stmt,))


def createHypoIndexesCS(conn, configuration: Iterable[str]) -> None:
    """Create HypoPG hypothetical indexes for a list of 'table.column' strings."""
    with conn.cursor() as cur:
        for index in configuration:
            table, column = index.split(".", 1)
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (f"CREATE INDEX ON {table}({column})",))


def estimateConfigurationCost(conn, query: str, configuration: Iterable[str]) -> Tuple[float, float]:
    """
    Estimates initial vs hypothetical cost for a single query under a 'table.column' configuration.
    Used by candidate generation (cg_auto_admin).
    """
    clearHypotheticalIndexes(conn)
    cost_init = getQueryCost(conn, query)
    createHypoIndexesCS(conn, configuration)
    cost_fin = getQueryCost(conn, query)
    clearHypotheticalIndexes(conn)
    return cost_init, cost_fin


def estimateWorkloadCostForConfig(
    conn,
    W: List[Any],
    configuration: Iterable[Tuple[str, Tuple[str, ...]]],
    query_weights: Optional[Dict[str, float]] = None,
    write_penalties: Optional[Callable[[str, Tuple[str, ...]], float]] = None,
) -> float:
    """
    Computes total hypothetical workload execution cost (Read Cost + Write Penalty) for a configuration.

    Parameters
    ----------
    conn            : psycopg2 connection
    W               : list of SQL queries (or CapturedQuery objects with .params)
    configuration   : iterable of (table, (col1, col2, ...)) indexes
    query_weights   : dict, optional ({query: call_frequency})
    write_penalties : callable, optional ((table, columns) -> penalty float)

    Returns
    -------
    float : Total Workload Cost = ReadCost + WriteCost
    """
    clearHypotheticalIndexes(conn)
    if configuration:
        createCompositeHypoIndexes(conn, configuration)

    read_cost = 0.0
    for query in W:
        weight = float(query_weights.get(query, 1.0)) if query_weights else 1.0
        read_cost += weight * getQueryCost(conn, query)
    clearHypotheticalIndexes(conn)

    write_cost = 0.0
    if write_penalties and configuration and callable(write_penalties):
        for table, cols in configuration:
            write_cost += write_penalties(table, tuple(cols))

    total_cost = read_cost + write_cost
    config_desc = ", ".join(f"{t}({','.join(c)})" for t, c in configuration) if configuration else "None (Baseline)"
    print(f"  [Cost Evaluation] Config: [{config_desc}] | Read Cost: {read_cost:.2f} | Write Penalty: {write_cost:.2f} | Total Cost: {total_cost:.2f}")

    return total_cost