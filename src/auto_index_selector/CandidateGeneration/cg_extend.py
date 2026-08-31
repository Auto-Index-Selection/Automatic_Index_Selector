"""
cg_extend.py — Candidate Generation for the Extend Algorithm.

Reference: Schlosser, Kossmann, Boissier.
"Efficient Scalable Multi-Attribute Index Selection Using Recursive Strategies."
ICDE 2019.

Extracts index-relevant (table, column) pairs directly from the workload SQL
by parsing each query with sqlglot and collecting columns referenced in
WHERE / JOIN ON / GROUP BY / ORDER BY clauses.

Unlike other CG modules that may enumerate multi-column composite candidates,
this module produces only SINGLE-COLUMN candidates — the Extend CS algorithm
then builds multi-column indexes iteratively via its "Morph" step (appending
one column at a time to an existing index).

Output format: {table -> [[col], ...]}  (same as all other CG modules)
"""

import sqlglot
import sqlglot.expressions as exp
from collections import defaultdict
from typing import Dict, List, Set, Tuple


# Clause types considered index-relevant
_INDEX_RELEVANT = (
    exp.Where,
    exp.Join,
    exp.Group,
    exp.Order,
)


def generateCandidateIndexes(conn, W: list, schema: dict) -> dict:
    """
    Parse every SQL query in W and collect index-relevant (table, column) pairs.

    Parameters
    ----------
    conn   : psycopg2 connection (not used; present for interface compatibility)
    W      : list of SQL strings (or objects with a .query attribute)
    schema : {table_name: {col_name: type_str, ...}, ...}
             Used to resolve unqualified column names to their tables and to
             validate that extracted columns actually exist.

    Returns
    -------
    dict  {table -> [[col], ...]}
        Single-column candidates per table, ready for flattenCandidateIndexes().
    """
    # Reverse map: col_name (lower) -> set of tables that own it
    col_to_tables: Dict[str, Set[str]] = defaultdict(set)
    for table, cols in schema.items():
        for col in cols:
            col_to_tables[col.lower()].add(table.lower())

    schema_tables = {t.lower() for t in schema}

    seen: Set[Tuple[str, str]] = set()
    candidates: Dict[str, List[List[str]]] = defaultdict(list)

    for query_obj in W:
        sql = getattr(query_obj, "query", query_obj)

        # Try postgres dialect first, fall back to generic
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
        except Exception:
            try:
                tree = sqlglot.parse_one(sql)
            except Exception:
                continue

        # Build alias -> real table name map for this query
        alias_map: Dict[str, str] = {}
        for table_expr in tree.find_all(exp.Table):
            tname = (table_expr.name or "").lower()
            alias = (table_expr.alias or "").lower()
            if tname in schema_tables:
                alias_map[tname] = tname
                if alias:
                    alias_map[alias] = tname

        # Walk index-relevant clauses and collect column references
        for clause_type in _INDEX_RELEVANT:
            for clause in tree.find_all(clause_type):
                for col_expr in clause.find_all(exp.Column):
                    col_name = (col_expr.name or "").lower()
                    if not col_name:
                        continue

                    raw_table = (col_expr.table or "").lower()
                    resolved = alias_map.get(raw_table, "")

                    if resolved and resolved in schema_tables:
                        candidates_for = [(resolved, col_name)]
                    else:
                        # Unqualified column: map to every table that owns it
                        candidates_for = [
                            (t, col_name)
                            for t in col_to_tables.get(col_name, set())
                            if t in schema_tables
                        ]

                    for table, col in candidates_for:
                        # Validate column exists in schema
                        schema_cols = {c.lower() for c in schema.get(table, {})}
                        if col not in schema_cols:
                            continue
                        pair = (table, col)
                        if pair not in seen:
                            seen.add(pair)
                            candidates[table].append([col])

    return dict(candidates)