"""
collapse_avg_csvs.py
====================
One-time migration: collapse per-k / per-storage-budget *_avg.csv files that
were produced by the OLD test_strategy.py into a single consolidated file per
(workload, cg, cs) — matching the NEW format written by write_total_timings_csv.

Naming conventions handled
--------------------------
  Greedy  : {w}_{cg}_cs_greedy_{k}_{m}_avg.csv   → key_col = "k"
  Drop    : {w}_{cg}_cs_drop_{storage}_avg.csv    → key_col = "storage_mb"
  Extend  : {w}_{cg}_cs_extend_{storage}_avg.csv  → key_col = "storage_mb"

Output
------
  {w}_{cg}_{cs}_avg.csv   (one file per group, written in the same directory)

The originals are deleted after a successful merge.  Pass --dry-run to preview
without writing or deleting anything.
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Regex patterns — most specific first
# ---------------------------------------------------------------------------
# greedy:  {w}_{cg}_cs_greedy_{k}_{m}_avg.csv
GREEDY_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_greedy)_(?P<k>\d+)_(?P<m>\d+)_avg\.csv$"
)
# drop / extend / any other cs with a single numeric suffix:
#   {w}_{cg}_{cs}_{storage}_avg.csv
SIZE_RE = re.compile(
    r"^(?P<w>.+?)_(?P<cg>cg_\w+)_(?P<cs>cs_\w+)_(?P<storage>\d+)_avg\.csv$"
)


def parse_filename(name: str):
    """
    Return (group_key, key_col, key_value, sort_key) or None if not matched.

    group_key  : (w, cg, cs)  — the consolidated file prefix
    key_col    : column name for the varying dimension
    key_value  : raw string value to write in that column
    sort_key   : numeric value used to sort rows
    """
    m = GREEDY_RE.match(name)
    if m:
        w, cg, cs = m["w"], m["cg"], m["cs"]
        k = int(m["k"])
        return (w, cg, cs), "k", str(k), k

    m = SIZE_RE.match(name)
    if m:
        w, cg, cs = m["w"], m["cg"], m["cs"]
        storage = int(m["storage"])
        return (w, cg, cs), "storage_mb", str(storage), storage

    return None


def collect_files(results_dir: Path):
    """
    Return a dict:
        { (w, cg, cs) -> [ (sort_key, key_col, key_value, path, rows, existing_cols) ] }
    """
    groups = defaultdict(list)
    for p in sorted(results_dir.glob("*_avg.csv")):
        # skip already-consolidated files (no numeric suffix before _avg)
        parsed = parse_filename(p.name)
        if parsed is None:
            print(f"  [skip] {p.name}  (no match)")
            continue
        group_key, key_col, key_value, sort_key = parsed

        # read existing columns from the file
        try:
            with open(p, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                existing_cols = list(reader.fieldnames or [])
        except Exception as e:
            print(f"  [skip] {p.name}  ({e})")
            continue

        groups[group_key].append((sort_key, key_col, key_value, p, rows, existing_cols))

    return groups


def merge_group(group_key, entries, results_dir: Path, dry_run: bool):
    w, cg, cs = group_key
    out_name = f"{w}_{cg}_{cs}_avg.csv"
    out_path = results_dir / out_name

    # sort by numeric key
    entries.sort(key=lambda e: e[0])

    # determine key_col (should be consistent within a group)
    key_cols = {e[1] for e in entries}
    if len(key_cols) > 1:
        print(f"  [warn] {group_key} has mixed key_col types {key_cols} — skipping")
        return
    key_col = key_cols.pop()

    # build unified fieldnames: key_col first, then everything else from old cols
    all_extra = []
    for _, _, _, _, _, existing_cols in entries:
        for c in existing_cols:
            if c not in all_extra and c != key_col:
                all_extra.append(c)
    fieldnames = [key_col] + all_extra

    print(f"\n  {'[DRY RUN] ' if dry_run else ''}Merging {len(entries)} files → {out_name}")
    for e in entries:
        print(f"    {e[3].name}  (key={e[2]})")

    if dry_run:
        return

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for sort_key, key_col_name, key_value, src_path, rows, _ in entries:
            for row in rows:
                # inject the key column if the old file didn't have it
                row.setdefault(key_col_name, key_value)
                # filename is ground truth for key value
                row[key_col_name] = key_value
                writer.writerow(row)

    # delete originals (only files that were actually merged, not out_path itself)
    for _, _, _, src_path, _, _ in entries:
        if src_path.resolve() != out_path.resolve():
            src_path.unlink()
            print(f"    deleted {src_path.name}")

    print(f"  wrote {out_path}")


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN — nothing will be written or deleted ===\n")

    groups = collect_files(RESULTS_DIR)

    if not groups:
        print("No matching files found.")
        return

    for group_key, entries in sorted(groups.items()):
        merge_group(group_key, entries, RESULTS_DIR, dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
