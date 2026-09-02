"""
measure_index_sizes.py
======================
Standalone script that measures the ACTUAL on-disk size of every index
configuration stored in indexes/*.sql, without running a full workload.

For each (workload, cg, cs) combination it:
  1. Discovers all matching *_create.sql files in indexes/.
  2. Sorts them by k / storage_mb (numeric).
  3. For each configuration:
       a. Executes the CREATE INDEX statements.
       b. Queries pg_relation_size() for total index size.
       c. Executes the DELETE INDEX statements.
  4. Writes one CSV per (workload, cg, cs) to results/:
       {w}_{cg}_{cs}_index_sizes.csv
     Columns: k | storage_mb, index_size_bytes, index_size_mb

Usage
-----
  # from repo root with venv active:
  PYTHONPATH=src python tests/measure_index_sizes.py

  # only a specific workload:
  PYTHONPATH=src python tests/measure_index_sizes.py --workload tpchWorkload

  # use a different DB / env file:
  PYTHONPATH=src python tests/measure_index_sizes.py --env .env.prod
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from pyprojroot import here

INDEXES_DIR = Path(str(here() / "indexes"))
RESULTS_DIR = Path(str(here() / "results"))

# ── filename patterns ──────────────────────────────────────────────────────────
# greedy:  {w}_{cg}_cs_greedy_{k}_{m}_create.sql
GREEDY_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_greedy)_(?P<k>\d+)_(?P<m>\d+)_create\.sql$"
)
# drop / extend: {w}_{cg}_{cs}_{storage}_create.sql
SIZE_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_(?P<storage>\d+)_create\.sql$"
)


def parse_create_filename(name: str):
    """
    Returns (group_key, key_col, key_value_str, sort_key) or None.
    group_key = (w, cg, cs)
    """
    m = GREEDY_RE.match(name)
    if m:
        return (m["w"], m["cg"], m["cs"]), "k", m["k"], int(m["k"])

    m = SIZE_RE.match(name)
    if m:
        return (m["w"], m["cg"], m["cs"]), "storage_mb", m["storage"], int(m["storage"])

    return None


def execute_sql_file(conn, path: Path) -> None:
    """Execute every statement in a .sql file, committing after each."""
    sql_text = path.read_text()
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    for stmt in statements:
        try:
            with conn.cursor() as cur:
                cur.execute(stmt + ";")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"    Warning: failed to execute '{stmt[:60]}…': {e}")


def measure_index_size(conn) -> int:
    """Total actual on-disk size (bytes) of all public non-system indexes."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(pg_relation_size(indexrelid)), 0)
            FROM pg_indexes
            JOIN pg_class idx ON idx.relname = pg_indexes.indexname
            JOIN pg_index  pi ON pi.indexrelid = idx.oid
            WHERE schemaname = 'public'
        """)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def collect_groups(indexes_dir: Path, workload_filter: str):
    """
    Returns {(w, cg, cs): [(sort_key, key_col, key_value, create_path, delete_path)]},
    sorted by sort_key within each group.
    """
    groups = defaultdict(list)
    for p in sorted(indexes_dir.glob("*_create.sql")):
        parsed = parse_create_filename(p.name)
        if parsed is None:
            continue
        group_key, key_col, key_value, sort_key = parsed
        w, cg, cs = group_key

        if workload_filter and workload_filter.lower() not in w.lower():
            continue

        delete_name = p.name.replace("_create.sql", "_delete.sql")
        delete_path = indexes_dir / delete_name
        if not delete_path.exists():
            print(f"  [skip] no matching delete file for {p.name}")
            continue

        groups[group_key].append((sort_key, key_col, key_value, p, delete_path))

    for gk in groups:
        groups[gk].sort(key=lambda e: e[0])

    return groups


def measure_group(conn, group_key, entries, results_dir: Path, dry_run: bool):
    w, cg, cs = group_key

    # All entries in a group share the same key_col
    key_col = entries[0][1]
    out_path = results_dir / f"{w}_{cg}_{cs}_index_sizes.csv"

    print(f"\n  {'[DRY RUN] ' if dry_run else ''}({w}, {cg}, {cs})  "
          f"key={key_col}  {len(entries)} configs  → {out_path.name}")

    rows = []
    for sort_key, key_col_name, key_value, create_path, delete_path in entries:
        print(f"    {key_col_name}={key_value} … ", end="", flush=True)

        if dry_run:
            print("(skipped)")
            continue

        # 1. Create indexes
        execute_sql_file(conn, create_path)

        # 2. Flush plan cache so pg_relation_size sees the new indexes
        with conn.cursor() as cur:
            cur.execute("DISCARD PLANS;")
        conn.commit()

        # 3. Measure
        size_bytes = measure_index_size(conn)
        size_mb    = size_bytes / (1024 ** 2)
        print(f"{size_mb:.2f} MB")

        rows.append({
            key_col_name:        key_value,
            "index_size_bytes":  size_bytes,
            "index_size_mb":     f"{size_mb:.4f}",
        })

        # 4. Drop indexes
        execute_sql_file(conn, delete_path)

    if not dry_run and rows:
        results_dir.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=[key_col, "index_size_bytes", "index_size_mb"]
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"    wrote {out_path}")


def main():
    ap = argparse.ArgumentParser(
        description="Measure actual on-disk index sizes for all configurations in indexes/."
    )
    ap.add_argument("--workload", default="",
                    help="Filter by workload name substring (e.g. 'tpch').")
    ap.add_argument("--env", default=".env",
                    help="Path to .env file (default: .env).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Discover files and print plan without touching the DB.")
    args = ap.parse_args()

    load_dotenv(args.env)

    groups = collect_groups(INDEXES_DIR, args.workload)
    if not groups:
        print("No matching create/delete SQL pairs found.")
        return

    print(f"Found {len(groups)} (w, cg, cs) groups across "
          f"{sum(len(v) for v in groups.values())} configurations.\n")

    if args.dry_run:
        for gk, entries in sorted(groups.items()):
            w, cg, cs = gk
            key_col = entries[0][1]
            print(f"  ({w}, {cg}, {cs})  key={key_col}  {len(entries)} configs")
            for _, kcn, kv, cp, dp in entries:
                print(f"    {kcn}={kv}  create={cp.name}  delete={dp.name}")
        return

    # Connect once per workload (same DB) — group by workload name
    by_workload = defaultdict(list)
    for gk, entries in groups.items():
        by_workload[gk[0]].append((gk, entries))

    for w_name, group_list in sorted(by_workload.items()):
        # Determine DB name from the workload module
        try:
            import importlib
            wl_module = importlib.import_module(f"auto_index_selector.Workload.{w_name}")
            _, DB_NAME, _ = wl_module.getWorkload()
        except Exception as e:
            print(f"  [warn] could not load workload module for {w_name}: {e}")
            DB_NAME = os.getenv("DB_NAME", w_name)

        print(f"\n=== Workload: {w_name}  DB: {DB_NAME} ===")
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        try:
            for gk, entries in sorted(group_list):
                measure_group(conn, gk, entries, RESULTS_DIR, dry_run=False)
        finally:
            conn.close()

    print("\nAll done.")


if __name__ == "__main__":
    main()
