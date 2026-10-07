"""
cg_relaxation.py — Candidate Generation phase for the relaxation-based
physical design approach of Bruno & Chaudhuri (SIGMOD 2005),
"Automatic Physical Database Tuning: A Relaxation-based Approach".

The paper obtains an "optimal" (usually oversized) configuration by
instrumenting the query optimizer itself: every index/view request the
optimizer issues while planning a query is intercepted, and the
provably-best index for that single request is synthesized and unioned
across the workload (Section 2). We don't have access to an
instrumentable optimizer, so this module approximates that step using
HypoPG-based what-if costing on top of PostgreSQL's real optimizer:

  1. For each query, extract the sargable (equality + range), ORDER/
     GROUP BY, and other-referenced columns per table — the same
     signal an intercepted index-request (S, N, O, A) would carry
     (Section 3.1, non-sargable predicates N aren't modeled and are
     folded into the referenced/suffix set).
  2. Following the paper's Lemma 1 & 2 case analysis, build a single
     covering index per (query, table): a seek over a selectivity-
     ordered prefix of the sargable columns, optionally re-keyed on
     the ORDER/GROUP BY columns when that's cheaper (the O != ∅ case
     in Section 2.1), with any remaining referenced columns appended
     as suffix/include columns. Prefix length and the seek-vs-sort
     variant are both chosen by real cost calls rather than derived
     analytically, since we don't have the plan introspection the
     paper's instrumented optimizer relies on.
  3. Union the per-query winners into C_best, the initial (typically
     too-large) configuration cs_relaxation.py then shrinks.

Candidate indexes are represented as `Index` namedtuples so that key
columns (ordered, used for seeking) and suffix columns (unordered,
included for covering) stay distinguishable — the relaxation
transformations in cs_relaxation.py (merging, prefixing) need that
distinction, which the flat `(table, columns)` representation used by
config_sel.py's Greedy(m, k) doesn't carry.
"""

from collections import OrderedDict
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

import sqlglot as sg
from ordered_set import OrderedSet

from auto_index_selector.CostEstimator.costEstimator import (
    estimateWorkloadCostForConfig,
)


####################################
# INDEX REPRESENTATION             #
####################################

class Index(NamedTuple):
    """
    A candidate index as an ordered seek key plus an unordered suffix
    of extra (covering) columns, e.g.
    Index('lineitem', ('l_shipdate',), ('l_returnflag',)).
    """
    table: str
    key: Tuple[str, ...]
    suffix: Tuple[str, ...] = ()

    def columns(self) -> Tuple[str, ...]:
        """Flat physical column list (key, then suffix) — the
        representation the existing cost estimator / Greedy(m, k) code
        expects."""
        return self.key + tuple(c for c in self.suffix if c not in self.key)

    def asCandidateTuple(self) -> Tuple[str, Tuple[str, ...]]:
        return (self.table, self.columns())


####################################
# INDEX REQUEST EXTRACTION         #
####################################

def _normalizeColumn(column: str, schema: Dict) -> Optional[Tuple[str, str]]:
    for table, attrs in schema.items():
        if column in attrs.keys():
            return table, column
    return None


def _extractIndexRequest(query: str, schema: Dict) -> Dict[str, Dict[str, "OrderedSet"]]:
    """
    Per-table approximation of what an intercepted index request would
    carry: sargable columns (equality + range predicates, plus join
    predicates — together the S in the paper's (S, N, O, A) request),
    ORDER/GROUP BY columns (O), and any other referenced columns (A).
    Non-sargable (N) predicates aren't classified separately; anything
    we can't place ends up in the referenced/suffix bucket, matching
    the covering role the paper gives A.
    """
    request: Dict[str, Dict[str, OrderedSet]] = OrderedDict()

    def bucket(table: str) -> Dict[str, OrderedSet]:
        return request.setdefault(
            table,
            {"sargable": OrderedSet(), "order": OrderedSet(), "referenced": OrderedSet()},
        )

    parsed = sg.parse_one(query)

    for where in parsed.find_all(sg.exp.Where):
        for pred in where.find_all((sg.exp.EQ, sg.exp.GT, sg.exp.GTE, sg.exp.LT, sg.exp.LTE)):
            for column in pred.find_all(sg.exp.Column):
                hit = _normalizeColumn(column.name, schema)
                if hit:
                    table, col = hit
                    bucket(table)["sargable"].add(col)

    for join in parsed.find_all(sg.exp.Join):
        on_clause = join.args.get("on")
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                hit = _normalizeColumn(column.name, schema)
                if hit:
                    table, col = hit
                    bucket(table)["sargable"].add(col)

    for grouporder in parsed.find_all((sg.exp.Group, sg.exp.Order)):
        for column in grouporder.find_all(sg.exp.Column):
            hit = _normalizeColumn(column.name, schema)
            if hit:
                table, col = hit
                bucket(table)["order"].add(col)

    for column in parsed.find_all(sg.exp.Column):
        hit = _normalizeColumn(column.name, schema)
        if hit:
            table, col = hit
            b = bucket(table)
            if col not in b["sargable"] and col not in b["order"]:
                b["referenced"].add(col)

    return request


####################################
# SELECTIVITY ESTIMATION           #
####################################

def _columnSelectivity(conn, table: str, column: str, selectivity_cache: Dict) -> float:
    """
    Rough selectivity proxy in (0, 1] (lower = more selective), read
    from pg_stats.n_distinct. This drives the "sorted by selectivity"
    prefix-growing step of Section 2.1. Falls back to 1.0 (assume
    non-selective, i.e. ordered last) when stats aren't available so
    ordering degrades gracefully instead of failing.
    """
    key = (table, column)
    if key in selectivity_cache:
        return selectivity_cache[key]

    selectivity = 1.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n_distinct FROM pg_stats WHERE tablename = %s AND attname = %s",
                (table, column),
            )
            row = cur.fetchone()
            if row and row[0]:
                n_distinct = row[0]
                if n_distinct > 0:
                    selectivity = 1.0 / n_distinct
                elif n_distinct < 0:
                    # pg_stats encodes this as -(ndistinct/rowcount) when
                    # distinct-count scales with table size
                    selectivity = -n_distinct
    except Exception:
        pass

    selectivity_cache[key] = selectivity
    return selectivity


####################################
# PER-REQUEST BEST INDEX (Sec 2.1) #
####################################

def _bestIndexForRequest(
    conn,
    query: str,
    table: str,
    sargable: "OrderedSet",
    order_cols: "OrderedSet",
    referenced: "OrderedSet",
    cost_cache: Dict,
    selectivity_cache: Dict,
    query_weights=None,
    write_penalties=None,
) -> Optional[Index]:
    """
    Synthesizes the single best covering index for one (query, table)
    index request, following the paper's Section 2.1 case analysis:
      - order sargable columns by estimated selectivity and grow the
        key one column at a time while it keeps reducing cost — the
        seek-over-a-selectivity-ordered-prefix optimum of Lemmas 1-2;
      - if the query also carries an order/group-by request, also try
        an index keyed on those columns (extended by the sargable
        columns, when compatible) and keep whichever plan is cheaper —
        the O != ∅ with/without a sort comparison;
      - append any still-uncovered referenced columns as suffix
        (covering) columns on the winner.
    Real cost calls (via the existing HypoPG-backed cost estimator)
    stand in for the paper's optimizer-internal reasoning, since we
    don't have plan introspection to derive this analytically.
    """

    def cost(index: Index) -> float:
        candidate = frozenset({index.asCandidateTuple()})
        if candidate not in cost_cache:
            cost_cache[candidate] = estimateWorkloadCostForConfig(
                conn, [query], candidate,
                query_weights=query_weights, write_penalties=write_penalties,
            )
        return cost_cache[candidate]

    baseline_cost = cost(Index(table, ()))

    ordered_sargable = sorted(
        sargable, key=lambda c: _columnSelectivity(conn, table, c, selectivity_cache)
    )

    best: Optional[Index] = None
    best_cost = baseline_cost
    key_so_far: Tuple[str, ...] = ()
    for col in ordered_sargable:
        trial_key = key_so_far + (col,)
        trial = Index(table, trial_key)
        trial_cost = cost(trial)
        if trial_cost < best_cost:
            best_cost = trial_cost
            key_so_far = trial_key
            best = trial
        else:
            break  # no further benefit from extending the prefix — Sec 2.1's stopping rule

    if order_cols:
        order_key = tuple(order_cols)
        already_covered = best is not None and best.key[: len(order_key)] == order_key
        if not already_covered:
            extra = tuple(c for c in ordered_sargable if c not in order_key)
            order_candidate = Index(table, order_key + extra)
            order_cost = cost(order_candidate)
            if order_cost < best_cost:
                best_cost = order_cost
                best = order_candidate

    if best is None:
        return None

    suffix = tuple(c for c in referenced if c not in best.key)
    return Index(best.table, best.key, suffix)


####################################
# WORKLOAD-LEVEL ENTRY POINT       #
####################################

def generateOptimalConfiguration(
    conn,
    W: List[str],
    schema: Dict,
    query_weights=None,
    write_penalties=None,
    cost_cache: Optional[Dict] = None,
    verbose: bool = False,
) -> Tuple[Set[Index], Dict[str, List[Index]]]:
    """
    Section 2's "get the optimal configuration": the union, across the
    workload, of the best per-(query, table) index. Returns C_best (the
    initial — usually oversized — configuration cs_relaxation.py will
    progressively shrink) plus a query -> [Index, ...] map so callers
    can inspect which request produced which index.
    """
    if cost_cache is None:
        cost_cache = {}
    selectivity_cache: Dict = {}

    c_best: Set[Index] = set()
    query_index_map: Dict[str, List[Index]] = OrderedDict()

    for query in W:
        request = _extractIndexRequest(query, schema)
        query_index_map[query] = []
        for table, cols in request.items():
            if not cols["sargable"] and not cols["order"]:
                continue
            index = _bestIndexForRequest(
                conn, query, table,
                cols["sargable"], cols["order"], cols["referenced"],
                cost_cache, selectivity_cache,
                query_weights=query_weights, write_penalties=write_penalties,
            )
            if index is not None:
                c_best.add(index)
                query_index_map[query].append(index)
                if verbose:
                    print(f"  [CandGen] {table}: key={index.key} suffix={index.suffix}")

    if verbose:
        print(f"[CandGen] C_best has {len(c_best)} candidate indexes across {len(W)} queries")

    return c_best, query_index_map


def toGreedyCandidateDict(indexes) -> Dict[str, List[List[str]]]:
    """
    Interop helper: converts a set of Index objects into the
    {table: [[col, ...], ...]} shape flattenCandidateIndexes() /
    generateCandidateIndexes() already produce, for feeding C_best into
    the existing Greedy(m, k) selector instead of (or alongside)
    cs_relaxation.py.
    """
    result: Dict[str, List[List[str]]] = {}
    for index in indexes:
        result.setdefault(index.table, []).append(list(index.columns()))
    return result