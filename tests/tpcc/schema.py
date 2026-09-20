"""
tests/tpcc/schema.py
--------------------
Manage the starting index configuration (C0) for tpcc_standard_db.

The database ships with no indexes and no constraints, which lets the suite
measure two arms:

  bare   -- nothing at all, so the advisor's recommendations are the *only*
            indexes present. Maximum headroom, but every write seq-scans.
  pkeys  -- the primary keys TPC-C actually specifies. This is how AutoAdmin,
            DTA and Extend are evaluated: C0 holds the constraint indexes and
            C* is what the advisor adds on top.

Only these named constraints are ever touched. Nothing here can drop an index
the benchmark harness created (those carry the idx_rec_ prefix).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# The primary keys defined by the TPC-C specification.
PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
    "warehouse":  ("w_id",),
    "district":   ("d_w_id", "d_id"),
    "customer":   ("c_w_id", "c_d_id", "c_id"),
    "orders":     ("o_w_id", "o_d_id", "o_id"),
    "new_orders": ("no_w_id", "no_d_id", "no_o_id"),
    "order_line": ("ol_w_id", "ol_d_id", "ol_o_id", "ol_number"),
    "stock":      ("s_w_id", "s_i_id"),
    "item":       ("i_id",),
}


def _constraint_name(table: str) -> str:
    return f"{table}_pkey"


def _end_transaction(conn) -> None:
    """
    Close any open transaction before switching autocommit.

    psycopg2 raises "set_session cannot be used inside a transaction" otherwise,
    and any earlier SELECT on this connection (a schema check, a row count) is
    enough to have opened one.
    """
    try:
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def existing_primary_keys(conn) -> List[str]:
    """Return the TPC-C primary key constraints currently present."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE connamespace = 'public'::regnamespace AND contype = 'p';"
        )
        return sorted(r[0] for r in cur.fetchall())


def create_primary_keys(conn, verbose: bool = True) -> List[str]:
    """
    Add the standard TPC-C primary keys.

    Runs each ALTER in its own autocommit statement so one failure (e.g. data
    that violates uniqueness) cannot abort the rest and leave a half-built C0
    that would silently skew the arm's results.
    """
    created, failed = [], []
    _end_transaction(conn)
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for table, cols in PRIMARY_KEYS.items():
                name = _constraint_name(table)
                try:
                    cur.execute(
                        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                        f"PRIMARY KEY ({', '.join(cols)});"
                    )
                    created.append(name)
                except Exception as e:
                    msg = str(e).strip().splitlines()[0]
                    if "already exists" in msg:
                        created.append(name)
                    else:
                        failed.append((name, msg))
    finally:
        conn.autocommit = previous

    if verbose:
        print(f"  [C0] Created {len(created)} TPC-C primary key(s).")
    for name, msg in failed:
        print(f"  ⚠ Could not create {name}: {msg}")
    if failed:
        raise RuntimeError(
            f"C0 setup incomplete: {len(failed)} primary key(s) could not be "
            f"created, so this arm would measure a partial configuration."
        )
    return created


def drop_primary_keys(conn, verbose: bool = True) -> List[str]:
    """Remove the TPC-C primary keys, returning the schema to the bare arm."""
    dropped = []
    _end_transaction(conn)
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for table in PRIMARY_KEYS:
                name = _constraint_name(table)
                try:
                    cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};")
                    dropped.append(name)
                except Exception as e:
                    logger.warning("Could not drop %s: %s", name, e)
    finally:
        conn.autocommit = previous

    if verbose:
        print(f"  [C0] Dropped TPC-C primary keys ({len(dropped)} constraint(s)).")
    return dropped


def setup_arm(conn, arm: str, verbose: bool = True) -> None:
    """Put the database into the starting configuration for the named arm."""
    if arm == "bare":
        drop_primary_keys(conn, verbose)
    elif arm == "pkeys":
        create_primary_keys(conn, verbose)
    else:
        raise ValueError(f"Unknown arm {arm!r}; expected 'bare' or 'pkeys'")


def table_row_counts(conn) -> Dict[str, int]:
    """
    Exact row counts per table.

    The observation window commits real TPC-C writes, so the database grows as
    the suite runs. Recording counts at the start and end of each arm makes that
    growth auditable, and is why absolute timings must not be compared across
    arms.
    """
    counts = {}
    with conn.cursor() as cur:
        for table in PRIMARY_KEYS:
            try:
                cur.execute(f"SELECT count(*) FROM {table};")
                counts[table] = int(cur.fetchone()[0])
            except Exception:
                conn.rollback()
        try:
            cur.execute("SELECT count(*) FROM history;")
            counts["history"] = int(cur.fetchone()[0])
        except Exception:
            conn.rollback()
    conn.commit()
    return counts
