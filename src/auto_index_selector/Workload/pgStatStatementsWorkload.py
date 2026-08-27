"""
Workload/pgStatStatementsWorkload.py
------------------------------------
Live Workload Loader using PostgreSQL pg_stat_statements.

Extracts the top analytical SELECT queries executed on the live database,
and uses an automated PlaceholderResolver to substitute normalized query
placeholders ($1, $2, ...) with typed dummy literals so that HypoPG
and EXPLAIN (FORMAT JSON) plan the queries without errors.

Usage in config.toml::

    [workload]
    module = "pgStatStatementsWorkload"
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fallback default schema for standard TPC-H tables
_DEFAULT_TPCH_SCHEMA = {
    "region": {"r_regionkey": "INT", "r_name": "VARCHAR", "r_comment": "VARCHAR"},
    "nation": {"n_nationkey": "INT", "n_name": "VARCHAR", "n_regionkey": "INT", "n_comment": "VARCHAR"},
    "part": {"p_partkey": "INT", "p_name": "VARCHAR", "p_mfgr": "VARCHAR", "p_brand": "VARCHAR", "p_type": "VARCHAR", "p_size": "INT", "p_container": "VARCHAR", "p_retailprice": "DOUBLE", "p_comment": "VARCHAR"},
    "supplier": {"s_suppkey": "INT", "s_name": "VARCHAR", "s_address": "VARCHAR", "s_nationkey": "INT", "s_phone": "VARCHAR", "s_acctbal": "DOUBLE", "s_comment": "VARCHAR"},
    "partsupp": {"ps_partkey": "INT", "ps_suppkey": "INT", "ps_availqty": "INT", "ps_supplycost": "DOUBLE", "ps_comment": "VARCHAR"},
    "customer": {"c_custkey": "INT", "c_name": "VARCHAR", "c_address": "VARCHAR", "c_nationkey": "INT", "c_phone": "VARCHAR", "c_acctbal": "DOUBLE", "c_mktsegment": "VARCHAR", "c_comment": "VARCHAR"},
    "orders": {"o_orderkey": "INT", "o_custkey": "INT", "o_orderstatus": "VARCHAR", "o_totalprice": "DOUBLE", "o_orderdate": "DATE", "o_orderpriority": "VARCHAR", "o_clerk": "VARCHAR", "o_shippriority": "INT", "o_comment": "VARCHAR"},
    "lineitem": {"l_orderkey": "INT", "l_partkey": "INT", "l_suppkey": "INT", "l_linenumber": "INT", "l_quantity": "DOUBLE", "l_extendedprice": "DOUBLE", "l_discount": "DOUBLE", "l_tax": "DOUBLE", "l_returnflag": "VARCHAR", "l_linestatus": "VARCHAR", "l_shipdate": "DATE", "l_commitdate": "DATE", "l_receiptdate": "DATE", "l_shipinstruct": "VARCHAR", "l_shipmode": "VARCHAR", "l_comment": "VARCHAR"},
}


class PlaceholderResolver:
    """Substitutes $1, $2, ... placeholders in pg_stat_statements queries with typed dummy literals."""

    def __init__(self, schema: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._schema = schema or _DEFAULT_TPCH_SCHEMA

    def resolve(self, query: str) -> str:
        """Replace all $N parameters with valid PostgreSQL literals."""
        if "$" not in query:
            return query

        resolved = query

        # 1. Resolve date expressions e.g. col <= $1 or col >= $1 or col = $1 where col contains 'date'
        resolved = re.sub(
            r'(\w*(?:date|time)\w*)\s*([=><!]+|BETWEEN)\s*\$(\d+)',
            r"\1 \2 '1998-01-01'",
            resolved,
            flags=re.IGNORECASE
        )

        # 2. Resolve string/varchar expressions e.g. col IN ($1, $2) or col = $1 where col contains name/char/flag/status/mode
        resolved = re.sub(
            r'(\w*(?:name|mode|flag|status|comment|segment|priority|type|instruct|address|phone|brand|container|clerk)\w*)\s*([=><!]+|LIKE|ILIKE)\s*\$(\d+)',
            r"\1 \2 'A'",
            resolved,
            flags=re.IGNORECASE
        )

        # 3. Resolve float/price/discount/tax/quantity/balance expressions
        resolved = re.sub(
            r'(\w*(?:price|discount|tax|qty|quantity|balance|acctbal|cost)\w*)\s*([=><!]+)\s*\$(\d+)',
            r"\1 \2 1.0",
            resolved,
            flags=re.IGNORECASE
        )

        # 4. Resolve integer/key/id expressions
        resolved = re.sub(
            r'(\w*(?:key|id|num|number|size|count|days)\w*)\s*([=><!]+)\s*\$(\d+)',
            r"\1 \2 1",
            resolved,
            flags=re.IGNORECASE
        )

        # 5. Resolve LIMIT / OFFSET $N
        resolved = re.sub(
            r'(LIMIT|OFFSET)\s*\$(\d+)',
            r"\1 10",
            resolved,
            flags=re.IGNORECASE
        )

        # 6. Fallback: replace any remaining $N with integer 1
        resolved = re.sub(r'\$\d+', '1', resolved)

        return resolved


def fetch_live_schema(conn) -> Dict[str, Dict[str, str]]:
    """Fetch table and column schema mapping from PostgreSQL information_schema."""
    schema: Dict[str, Dict[str, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position;
            """)
            for table_name, column_name, data_type in cur.fetchall():
                schema.setdefault(table_name, {})[column_name] = data_type.upper()
    except Exception as e:
        logger.warning("Could not fetch information_schema: %s. Using default schema.", e)
        if conn:
            conn.rollback()
        return _DEFAULT_TPCH_SCHEMA

    return schema if schema else _DEFAULT_TPCH_SCHEMA


def getWorkload(conn=None, limit: int = 50) -> Tuple[List[str], Dict[str, Dict[str, str]]]:
    """Retrieve top SELECT queries from pg_stat_statements with resolved placeholders.

    Returns:
        (queries, schema) tuple matching the standard Workload interface.
    """
    if conn is None:
        logger.warning("No live connection passed to pgStatStatementsWorkload. Returning default TPC-H schema.")
        return [], _DEFAULT_TPCH_SCHEMA

    schema = fetch_live_schema(conn)
    resolver = PlaceholderResolver(schema=schema)
    queries: List[str] = []

    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT query
                FROM pg_stat_statements
                WHERE query NOT ILIKE '%pg_stat%'
                  AND query NOT ILIKE '%hypopg%'
                  AND query NOT ILIKE '%advisor%'
                  AND query NOT ILIKE '%information_schema%'
                  AND query ILIKE 'SELECT%'
                ORDER BY total_exec_time DESC
                LIMIT {int(limit)};
            """)
            for row in cur.fetchall():
                raw_query = row[0].strip()
                if raw_query:
                    resolved_query = resolver.resolve(raw_query)
                    queries.append(resolved_query)
        logger.info("Loaded %d live queries from pg_stat_statements", len(queries))
    except Exception as e:
        logger.error("Failed to load queries from pg_stat_statements: %s", e)
        conn.rollback()

    return queries, schema
