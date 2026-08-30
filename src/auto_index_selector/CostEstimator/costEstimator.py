"""
CostEstimator/costEstimator.py
------------------------------
Core cost estimation module using HypoPG and the PostgreSQL optimizer.
"""

import logging
import psycopg2

logger = logging.getLogger(__name__)


def clearHypotheticalIndexes(conn):
    """Remove all HypoPG hypothetical indexes."""
    with conn.cursor() as cur:
        cur.execute("SELECT hypopg_reset();")


def getQueryCost(conn, query: str, fallback_cost: float = 1e9) -> float:
    """
    Returns PostgreSQL optimizer cost via EXPLAIN (FORMAT JSON).
    If a query fails or times out, safely rolls back and returns fallback_cost.
    """
    explain_query = f"EXPLAIN (FORMAT JSON) {query}"
    try:
        with conn.cursor() as cur:
            cur.execute(explain_query)
            result = cur.fetchone()
            if result and result[0]:
                return float(result[0][0]["Plan"]["Total Cost"])
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("Query cost estimation failed or timed out: %s. Assigned fallback cost.", exc)
        return fallback_cost

    return fallback_cost


def createCompositeHypoIndexes(conn, configuration):
    """Create HypoPG hypothetical indexes for a list of (table, (columns...)) tuples."""
    with conn.cursor() as cur:
        for table, cols in configuration:
            col_list = ",".join(cols)
            stmt = f"CREATE INDEX ON {table}({col_list})"
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (stmt,))


def createHypoIndexesCS(conn, configuration):
    """Create HypoPG hypothetical indexes for a list of 'table.column' strings."""
    with conn.cursor() as cur:
        for index in configuration:
            table, column = index.split(".", 1)
            cur.execute("SELECT * FROM hypopg_create_index(%s);", (f"CREATE INDEX ON {table}({column})",))


def estimateConfigurationCost(conn, query: str, configuration):
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


def estimateWorkloadCostForConfig(conn, W, configuration, query_weights=None, write_penalties=None) -> float:
    """
    Computes total hypothetical workload execution cost (Read Cost + Write Penalty) for a configuration.

    Parameters
    ----------
    conn            : psycopg2 connection
    W               : list of SQL queries
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