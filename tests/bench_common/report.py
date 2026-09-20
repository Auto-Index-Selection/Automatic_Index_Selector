"""
tests/bench_common/report.py
----------------------------
Turn a finished run directory into a human-readable summary.

The CSVs and JSON files hold everything, but nothing in them says which number
is the one to quote, or which caveats apply. This writes a `summary.md` next to
them that states the baseline, ranks the strategies against it, and carries the
warnings forward (failed queries, baseline drift, database growth) so a run
cannot be read out of context later.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _read_csv(path: Path) -> Dict[str, Dict[str, str]]:
    with open(path, newline="") as f:
        return {row["query"]: row for row in csv.DictReader(f)}


def _f(rows: Dict[str, Dict[str, str]], key: str, col: str = "avg_time_ms") -> float:
    try:
        return float(rows[key][col])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def _fmt(v: float, unit: str = "") -> str:
    return "n/a" if v != v else f"{v:,.2f}{unit}"


def _one_line(sql: str, width: int = 110) -> str:
    """
    Collapse a query to a single readable line for a table cell.

    Strips leading `--` comments (the query files are documented, and
    pg_stat_statements keeps that text verbatim) and squeezes whitespace, so a
    multi-line SELECT does not break the markdown table.
    """
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    flat = " ".join(body.split()).replace("|", "\\|")
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def collect(arm_dir: Path) -> Dict:
    """Gather every artefact a run produced into one structure."""
    csv_dir, configs_dir = arm_dir / "csv", arm_dir / "configs"
    data: Dict = {"arm": arm_dir.name, "strategies": [], "baseline": None,
                  "baseline_closing": None, "observation": None, "row_counts": None}

    for name, key in [("baseline", "baseline"), ("baseline_closing", "baseline_closing")]:
        p = csv_dir / f"{name}.csv"
        if p.exists():
            rows = _read_csv(p)
            data[key] = {
                "total_ms": _f(rows, "TOTAL"),
                "p95_ms": _f(rows, "TOTAL_P95"),
                "min_ms": _f(rows, "TOTAL_MIN"),
                "failures": int(float(rows.get("FAILED_QUERIES", {}).get("failures") or 0)),
                "per_query": {q: _f(rows, q) for q in rows
                              if not q.startswith("TOTAL") and q not in ("STORAGE_MB", "FAILED_QUERIES")},
            }

    for p in sorted(csv_dir.glob("*.csv")):
        if p.stem.startswith("baseline"):
            continue
        rows = _read_csv(p)
        cfg_path = configs_dir / f"{p.stem}.json"
        indexes = []
        if cfg_path.exists():
            indexes = json.loads(cfg_path.read_text()).get("indexes", [])
        data["strategies"].append({
            "label": p.stem,
            "total_ms": _f(rows, "TOTAL"),
            "p95_ms": _f(rows, "TOTAL_P95"),
            "storage_mb": _f(rows, "STORAGE_MB", "avg_time_seconds"),
            "failures": int(float(rows.get("FAILED_QUERIES", {}).get("failures") or 0)),
            "indexes": indexes,
        })

    for name in ("observation", "row_counts"):
        p = configs_dir / f"{name}.json"
        if p.exists():
            data[name] = json.loads(p.read_text())
    return data


def _observation_section(obs: Dict) -> List[str]:
    out = ["## Observation window", ""]
    exec_stats = obs.get("workload_execution") or {}
    txns = exec_stats.get("transactions") or {}

    failed_rounds = exec_stats.get("read_failures") or 0
    reads_line = f"- Reads executed: `{exec_stats.get('reads', 0)}`"
    if failed_rounds:
        reads_line += f" ⚠ ({failed_rounds} read round(s) failed)"
    txn_line = ", ".join(f"`{k}`×{v}" for k, v in sorted(txns.items())) or "none"

    out += [
        f"Captured in **{obs.get('seconds', 0):.1f}s** and shared by every strategy below, "
        f"so all of them were evaluated against the *identical* workload.",
        "",
        reads_line,
        f"- Transactions: {txn_line}",
        f"- Distinct queries observed: **{obs.get('queries_observed', 0)}**",
        "",
    ]

    weights = obs.get("query_weights") or {}
    if weights:
        out += ["### Observed workload (call-count weighted)", "",
                "| calls | query |", "|---:|---|"]
        for q, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            out.append(f"| {int(w)} | `{_one_line(q)}` |")
        out.append("")

    delta = obs.get("write_delta") or {}
    written = {t: d for t, d in delta.items()
               if d.get("inserts") or d.get("updates") or d.get("deletes") or d.get("column_sets")}
    out += ["### Write activity driving the penalty model", ""]
    if not written:
        out += ["No write activity recorded — every write penalty will be 0.", ""]
        return out

    # The extension reports some relations only by OID. They cannot be matched
    # to a candidate index, so they are summarised separately rather than
    # padding the table with unusable rows -- but they are NOT hidden, since
    # unattributed writes may point at a gap in the extension's coverage.
    named = {t: d for t, d in written.items() if not t.startswith("unknown_")}
    unnamed = {t: d for t, d in written.items() if t.startswith("unknown_")}

    out += ["| table | inserts | updates | deletes | updated column sets |",
            "|---|---:|---:|---:|---|"]
    for t, d in sorted(named.items()):
        sets = ", ".join(f"`{k}`×{v}" for k, v in sorted((d.get("column_sets") or {}).items())) or "—"
        out.append(f"| `{t}` | {d.get('inserts', 0):,} | {d.get('updates', 0):,} | "
                   f"{d.get('deletes', 0):,} | {sets} |")
    out.append("")

    if unnamed:
        rows = sum(sum((d.get("column_sets") or {}).values()) for d in unnamed.values())
        out += [f"> {len(unnamed)} relation(s) were reported by OID only "
                f"(`{'`, `'.join(sorted(unnamed))}`), covering {rows} updated row(s). "
                f"They cannot be matched to a candidate index, so they contribute no "
                f"write penalty.", ""]
    return out


def render(data: Dict) -> str:
    arm = data["arm"]
    base = data.get("baseline")
    closing = data.get("baseline_closing")

    # The closing baseline is the trustworthy reference when the two disagree:
    # a contaminated opening baseline is the failure mode this guards against.
    ref = base
    ref_name = "opening baseline"
    drift = None
    if base and closing and base["total_ms"] == base["total_ms"] and base["total_ms"]:
        drift = ((closing["total_ms"] - base["total_ms"]) / base["total_ms"]) * 100.0
        if abs(drift) > 5.0:
            ref, ref_name = closing, "closing baseline"

    out = [f"# Benchmark summary — arm `{arm}`", ""]

    if base:
        out += ["## Baseline (C0 only)", "",
                f"- Opening: **{_fmt(base['total_ms'], ' ms')}** "
                f"(min {_fmt(base['min_ms'])}, p95 {_fmt(base['p95_ms'])})"]
        if closing:
            out.append(f"- Closing: **{_fmt(closing['total_ms'], ' ms')}**")
        if drift is not None:
            out.append(f"- Drift over the arm: **{drift:+.1f}%**")
            if abs(drift) > 5.0:
                out += ["",
                        f"> ⚠ The baseline moved more than 5% during this arm, so the two "
                        f"measurements disagree. Improvements below are computed against the "
                        f"**{ref_name}**, and any strategy difference smaller than {abs(drift):.0f}% "
                        f"is not attributable to the strategy."]
        out.append("")

    if data.get("observation"):
        out += _observation_section(data["observation"])

    ok = [s for s in data["strategies"] if not s["failures"] and s["total_ms"] == s["total_ms"]]
    bad = [s for s in data["strategies"] if s["failures"] or s["total_ms"] != s["total_ms"]]
    ok.sort(key=lambda s: s["total_ms"])

    out += [f"## Strategies ({len(ok)} valid"
            + (f", {len(bad)} excluded" if bad else "") + ")", ""]
    if ref and ref["total_ms"] == ref["total_ms"]:
        out.append(f"Improvement is measured against the {ref_name} "
                   f"({_fmt(ref['total_ms'], ' ms')}).")
        out.append("")
    out += ["| rank | strategy | total | vs baseline | storage | indexes |",
            "|---:|---|---:|---:|---:|---|"]
    for i, s in enumerate(ok, 1):
        imp = "—"
        if ref and ref["total_ms"] == ref["total_ms"] and ref["total_ms"]:
            imp = f"{((ref['total_ms'] - s['total_ms']) / ref['total_ms']) * 100.0:+.1f}%"
        idx = "; ".join(f"`{e['table']}({', '.join(e['columns'])})`" for e in s["indexes"]) or "_none selected_"
        out.append(f"| {i} | `{s['label']}` | {_fmt(s['total_ms'], ' ms')} | {imp} | "
                   f"{_fmt(s['storage_mb'], ' MB')} | {idx} |")
    out.append("")

    if bad:
        out += ["### Excluded from comparison", "",
                "These runs had failed queries, so their totals cover an incomplete "
                "query set and are not comparable to the baseline.", ""]
        for s in bad:
            out.append(f"- `{s['label']}` — {s['failures']} failed query execution(s)")
        out.append("")

    penalised = [(s["label"], e) for s in data["strategies"] for e in s["indexes"]
                 if e.get("write_penalty")]
    if penalised:
        out += ["## Write penalties", "",
                "| strategy | index | write penalty |", "|---|---|---:|"]
        seen = set()
        for label, e in penalised:
            key = (e["table"], tuple(e["columns"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(f"| `{label}` | `{e['table']}({', '.join(e['columns'])})` | "
                       f"{e['write_penalty']:,.4f} |")
        out.append("")

    redundant = {(e["table"], tuple(e["columns"]))
                 for s in data["strategies"] for e in s["indexes"]
                 if e.get("redundant_with_existing")}
    if redundant:
        out += ["> ⚠ Some recommendations duplicate an index that already exists in C0:",
                ""]
        out += [f"> - `{t}({', '.join(c)})`" for t, c in sorted(redundant)]
        out.append("")

    rc = data.get("row_counts")
    if rc and rc.get("growth"):
        grew = {t: n for t, n in rc["growth"].items() if n}
        if grew:
            out += ["## Database growth during this arm", "",
                    "The observation window commits real write transactions, so the data "
                    "changes as the arm runs. **Do not compare absolute timings across arms.**",
                    "", "| table | before | after | growth |", "|---|---:|---:|---:|"]
            for t, n in sorted(grew.items()):
                out.append(f"| `{t}` | {rc['before'].get(t, 0):,} | {rc['after'].get(t, 0):,} | {n:+,} |")
            out.append("")

    out += ["## Files", "",
            "- `csv/` — per-query timings, one file per strategy "
            "(`TOTAL`, `TOTAL_MEDIAN`, `TOTAL_MIN`, `TOTAL_P95`, `STORAGE_MB`, `FAILED_QUERIES`)",
            "- `configs/` — the index configuration each strategy chose, the shared "
            "observation, C0, and row counts",
            "- `plots/` — comparison charts", ""]
    return "\n".join(out)


def write_summary(arm_dir: Path) -> Optional[Path]:
    """Write summary.md into a finished arm directory."""
    if not (arm_dir / "csv").is_dir():
        return None
    data = collect(arm_dir)
    if not data["strategies"] and not data["baseline"]:
        return None
    path = arm_dir / "summary.md"
    path.write_text(render(data))
    return path
