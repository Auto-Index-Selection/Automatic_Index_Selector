"""
CostEstimator/write_penalty_estimator.py
-----------------------------------------
Self-contained B-tree write penalty estimator for the Automatic Index
Selector pipeline.

Integrates:
  - B-tree maintenance cost theory (from /home/pratik/MTP and /home/pratik/WritePenalty)
  - Column-set UPDATE tracking from the advisor_write_stats extension (/home/pratik/extension)
  - PostgreSQL catalog metadata (pg_settings, pg_stats, pg_stat_user_tables)

Adapted to use psycopg2 (the project's existing driver) with zero extra dependencies.

Usage::

    estimator = WritePenaltyEstimator(conn, write_scale=1.0)
    estimator.ensure_extension()

    snap_before = estimator.snapshot()
    # ... execute workload / candidate generation ...
    snap_after = estimator.snapshot()

    delta = estimator.compute_delta(snap_before, snap_after)
    penalties = estimator.estimate_penalties(candidate_indexes, delta)
    # penalties = {(table, (col1, col2)): penalty_cost_float, ...}

All costs are expressed in PostgreSQL planner cost units (the same
units as EXPLAIN output), ensuring direct comparability with read-side
HypoPG benefit estimates.

B-tree cost formulas:

    InsertCost(index) = btree_height × random_page_cost
                      + cpu_index_tuple_cost

    DeleteCost(index) = btree_height × random_page_cost

    UpdateCost(index, updated_col_set):
        IF (updated_col_set ∩ index_columns) ≠ ∅:
            DeleteCost + InsertCost          # non-HOT: index entry replacement
        ELSE:
            0                                # HOT update — no index maintenance

    WritePenalty(index) = Σ [
        delta_ins  × InsertCost
      + Σ_{col_set} delta_upd(col_set) × UpdateCost(index, col_set)
      + delta_del  × DeleteCost
    ]
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, FrozenSet, Set

logger = logging.getLogger(__name__)

# B-tree physical constants
_INDEX_TUPLE_OVERHEAD: int = 8        # bytes per index tuple (item pointer + header)
_BTREE_FILL_FACTOR: float = 0.9      # default B-tree fill factor


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PlannerCosts:
    """PostgreSQL planner cost constants from pg_settings."""
    random_page_cost: float = 4.0
    cpu_index_tuple_cost: float = 0.005
    seq_page_cost: float = 1.0


@dataclass
class ColumnUpdateStats:
    """Per-column update statistics from advisor_get_update_stats()."""
    relation_name: str
    column_name: str
    update_query_count: int
    rows_updated: int


@dataclass
class ColumnSetUpdateStats:
    """Per-column-SET update statistics from advisor_get_column_set_stats().

    Captures multi-column updates (e.g. UPDATE SET A=1, B=2) as a single
    atomic column set tuple ('A', 'B'), avoiding both overcounting
    and undercounting across single-column and composite indexes.
    """
    relation_name: str
    column_set: Tuple[str, ...]
    update_query_count: int
    rows_updated: int


@dataclass
class TableDMLDelta:
    """Aggregated DML delta for a single table across the workload window."""
    table_name: str
    delta_inserts: int = 0
    delta_updates: int = 0
    delta_deletes: int = 0
    # Per-column-set update rows: {frozenset({'colA', 'colB'}): rows_updated_delta}
    # Provides exact set-level granularity for HOT-aware penalty estimation.
    column_set_update_rows: Dict[FrozenSet[str], int] = field(default_factory=dict)
    # Rolled-up / single-column update rows: {column_name: rows_updated_delta}
    # Fallback when column set stats are unavailable.
    column_update_rows: Dict[str, int] = field(default_factory=dict)


@dataclass
class Snapshot:
    """Combined point-in-time snapshot of extension stats + pg_stat_user_tables."""
    # Per-column UPDATE stats from advisor_get_update_stats()
    column_stats: List[ColumnUpdateStats]
    # Per-column-SET UPDATE stats from advisor_get_column_set_stats()
    column_set_stats: List[ColumnSetUpdateStats]
    # Per-table DML counters from pg_stat_user_tables:
    #   {table_name: (n_tup_ins, n_tup_upd, n_tup_del)}
    table_stats: Dict[str, Tuple[int, int, int]]


# ---------------------------------------------------------------------------
# Main estimator class
# ---------------------------------------------------------------------------


class WritePenaltyEstimator:
    """B-tree write penalty estimator using advisor_write_stats + pg catalogs.

    All costs are expressed in PostgreSQL planner cost units (same as
    EXPLAIN output), ensuring direct comparability with read-side
    HypoPG benefit estimates.

    Example::

        estimator = WritePenaltyEstimator(conn, write_scale=1.0)
        estimator.ensure_extension()
        snap_before = estimator.snapshot()
        # ... run workload ...
        snap_after = estimator.snapshot()
        delta = estimator.compute_delta(snap_before, snap_after)
        penalties = estimator.estimate_penalties(candidate_indexes, delta)
    """

    def __init__(self, conn, write_scale: float = 1.0):
        """Initialise the estimator.

        Args:
            conn:        psycopg2 connection object.
            write_scale: Multiplier for observed write counts
                         (analytical scaling, e.g. 100.0 to project
                         100× OLTP write volume).
        """
        self._conn = conn
        self._write_scale = write_scale
        self._planner_costs: Optional[PlannerCosts] = None
        self._block_size: Optional[int] = None
        self._height_cache: Dict[Tuple[str, Tuple[str, ...]], int] = {}
        self._table_cardinality_cache: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Extension management
    # ------------------------------------------------------------------

    def ensure_extension(self):
        """Verify that the advisor_write_stats extension and functions exist.

        1. Creates extension if not yet created.
        2. Registers advisor_get_column_set_stats() if missing.
        Raises RuntimeError if the underlying shared library is not loaded
        via shared_preload_libraries.
        """
        with self._conn.cursor() as cur:
            try:
                # 1. Check if extension or function is available
                cur.execute(
                    "SELECT 1 FROM pg_proc WHERE proname = 'advisor_get_update_stats'"
                )
                if cur.fetchone() is None:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats")
                    self._conn.commit()
                    logger.info("Created advisor_write_stats extension")

                # 2. Ensure advisor_get_column_set_stats is registered
                cur.execute(
                    "SELECT 1 FROM pg_proc WHERE proname = 'advisor_get_column_set_stats'"
                )
                if cur.fetchone() is None:
                    try:
                        cur.execute("""
                            CREATE OR REPLACE FUNCTION advisor_get_column_set_stats(
                                OUT relation_name text,
                                OUT column_set text[],
                                OUT update_query_count bigint,
                                OUT rows_updated bigint
                            )
                            RETURNS SETOF record
                            AS '$libdir/advisor_write_stats', 'advisor_get_column_set_stats'
                            LANGUAGE C STRICT VOLATILE;
                        """)
                        self._conn.commit()
                        logger.info("Registered advisor_get_column_set_stats function")
                    except Exception as func_err:
                        logger.warning(
                            "Could not register advisor_get_column_set_stats: %s", func_err
                        )
                        self._conn.rollback()

                logger.info("advisor_write_stats extension verified successfully")
            except Exception as e:
                self._conn.rollback()
                raise RuntimeError(
                    "advisor_write_stats extension is not available. "
                    "Ensure it is compiled, installed, and listed in "
                    "shared_preload_libraries in postgresql.conf, then "
                    "restart PostgreSQL."
                ) from e

    # ------------------------------------------------------------------
    # Snapshot: capture current state
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Take a point-in-time snapshot of write statistics.

        Captures:
          1. Per-column UPDATE stats from advisor_get_update_stats()
          2. Per-column-SET UPDATE stats from advisor_get_column_set_stats()
          3. Per-table DML counters from pg_stat_user_tables

        Returns:
            A Snapshot combining all sources.
        """
        column_stats = self._snapshot_extension_column_stats()
        column_set_stats = self._snapshot_extension_column_set_stats()
        table_stats = self._snapshot_table_stats()
        return Snapshot(
            column_stats=column_stats,
            column_set_stats=column_set_stats,
            table_stats=table_stats,
        )

    def _snapshot_extension_column_stats(self) -> List[ColumnUpdateStats]:
        """Query advisor_get_update_stats() for per-column UPDATE tracking."""
        results: List[ColumnUpdateStats] = []
        with self._conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM advisor_get_update_stats()")
                for row in cur.fetchall():
                    rel_name = row[0]
                    if rel_name and '.' in rel_name:
                        rel_name = rel_name.split('.')[-1]
                    results.append(ColumnUpdateStats(
                        relation_name=rel_name,
                        column_name=row[1],
                        update_query_count=int(row[2]),
                        rows_updated=int(row[3]),
                    ))
            except Exception as e:
                logger.warning("Failed to query advisor_get_update_stats: %s", e)
                self._conn.rollback()
        return results

    def _snapshot_extension_column_set_stats(self) -> List[ColumnSetUpdateStats]:
        """Query advisor_get_column_set_stats() for multi-column SET update tracking."""
        results: List[ColumnSetUpdateStats] = []
        with self._conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM advisor_get_column_set_stats()")
                for row in cur.fetchall():
                    rel_name = row[0]
                    if rel_name and '.' in rel_name:
                        rel_name = rel_name.split('.')[-1]
                    # Convert postgres array to tuple of strings
                    raw_cols = row[1]
                    if isinstance(raw_cols, list):
                        col_set = tuple(raw_cols)
                    elif isinstance(raw_cols, tuple):
                        col_set = raw_cols
                    else:
                        col_set = tuple(str(raw_cols).strip('{}').split(',')) if raw_cols else ()

                    results.append(ColumnSetUpdateStats(
                        relation_name=rel_name,
                        column_set=col_set,
                        update_query_count=int(row[2]),
                        rows_updated=int(row[3]),
                    ))
            except Exception as e:
                logger.debug("advisor_get_column_set_stats not available or failed: %s", e)
                self._conn.rollback()
        return results

    def _snapshot_table_stats(self) -> Dict[str, Tuple[int, int, int]]:
        """Read cumulative (n_tup_ins, n_tup_upd, n_tup_del) from pg_stat_user_tables."""
        stats: Dict[str, Tuple[int, int, int]] = {}
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT relname,
                       COALESCE(n_tup_ins, 0)::bigint,
                       COALESCE(n_tup_upd, 0)::bigint,
                       COALESCE(n_tup_del, 0)::bigint
                FROM pg_stat_user_tables
            """)
            for row in cur.fetchall():
                stats[row[0]] = (int(row[1]), int(row[2]), int(row[3]))
        return stats

    # ------------------------------------------------------------------
    # Delta computation
    # ------------------------------------------------------------------

    def compute_delta(
        self, before: Snapshot, after: Snapshot
    ) -> Dict[str, TableDMLDelta]:
        """Compute the DML activity delta between two snapshots.

        Combines:
          - INSERT / UPDATE / DELETE row counts from pg_stat_user_tables
          - Per-column-SET UPDATE row counts from advisor_get_column_set_stats()
          - Per-column UPDATE row counts from advisor_get_update_stats()

        Args:
            before: Snapshot taken before the workload.
            after:  Snapshot taken after the workload.

        Returns:
            dict mapping table_name → TableDMLDelta.
        """
        deltas: Dict[str, TableDMLDelta] = {}

        # 1) Compute INSERT / UPDATE / DELETE deltas from pg_stat_user_tables
        for table_name, (ins_after, upd_after, del_after) in after.table_stats.items():
            ins_before, upd_before, del_before = before.table_stats.get(
                table_name, (0, 0, 0)
            )
            delta = TableDMLDelta(
                table_name=table_name,
                delta_inserts=max(0, ins_after - ins_before),
                delta_updates=max(0, upd_after - upd_before),
                delta_deletes=max(0, del_after - del_before),
            )
            deltas[table_name] = delta

        # 2) Compute per-column-SET UPDATE deltas
        before_set_lookup: Dict[Tuple[str, FrozenSet[str]], int] = {}
        for s in before.column_set_stats:
            before_set_lookup[(s.relation_name, frozenset(s.column_set))] = s.rows_updated

        for s in after.column_set_stats:
            set_key = frozenset(s.column_set)
            lookup_key = (s.relation_name, set_key)
            rows_before = before_set_lookup.get(lookup_key, 0)
            row_delta = max(0, s.rows_updated - rows_before)

            if row_delta > 0:
                if s.relation_name not in deltas:
                    deltas[s.relation_name] = TableDMLDelta(
                        table_name=s.relation_name
                    )
                deltas[s.relation_name].column_set_update_rows[set_key] = row_delta

        # 3) Compute per-column UPDATE deltas (as single-column fallback and rollup)
        before_col_lookup: Dict[Tuple[str, str], int] = {}
        for cs in before.column_stats:
            before_col_lookup[(cs.relation_name, cs.column_name)] = cs.rows_updated

        for cs in after.column_stats:
            lookup_key = (cs.relation_name, cs.column_name)
            rows_before = before_col_lookup.get(lookup_key, 0)
            row_delta = max(0, cs.rows_updated - rows_before)

            if row_delta > 0:
                if cs.relation_name not in deltas:
                    deltas[cs.relation_name] = TableDMLDelta(
                        table_name=cs.relation_name
                    )
                deltas[cs.relation_name].column_update_rows[cs.column_name] = row_delta

        return deltas

    # ------------------------------------------------------------------
    # Penalty estimation (B-tree cost model)
    # ------------------------------------------------------------------

    def estimate_penalties(
        self,
        candidate_indexes: Dict[str, list],
        deltas: Dict[str, TableDMLDelta],
    ) -> Dict[Tuple[str, Tuple[str, ...]], float]:
        """Estimate write penalty for each candidate index.

        Args:
            candidate_indexes: {table: [[col1, col2], [col3], ...]}
                               (the project's standard candidate format)
            deltas:            {table: TableDMLDelta} from compute_delta()

        Returns:
            {(table, (col1, col2, ...)): penalty_cost} dict.
            Penalty is in PostgreSQL planner cost units.
        """
        costs = self._get_planner_costs()
        penalties: Dict[Tuple[str, Tuple[str, ...]], float] = {}

        for table, col_lists in candidate_indexes.items():
            delta = deltas.get(table)
            if delta is None:
                # No DML on this table → zero penalty for all its indexes
                for cols in col_lists:
                    penalties[(table, tuple(cols))] = 0.0
                continue

            for cols in col_lists:
                cols_tuple = tuple(cols)
                penalty = self._compute_index_penalty(
                    table, cols_tuple, delta, costs
                )
                penalties[(table, cols_tuple)] = penalty

        return penalties

    def _compute_index_penalty(
        self,
        table: str,
        columns: Tuple[str, ...],
        delta: TableDMLDelta,
        costs: PlannerCosts,
    ) -> float:
        """Compute write penalty for a single (table, columns) index.

        B-tree cost formulas:
          InsertCost = btree_height × random_page_cost + cpu_index_tuple_cost
          DeleteCost = btree_height × random_page_cost
          UpdateCost = InsertCost + DeleteCost  (non-HOT: indexed cols modified)
                     = 0                        (HOT: indexed cols NOT modified)

        Multi-Column Update Handling:
          - If column_set_update_rows is available:
            For each updated column set S with R rows:
              If (S ∩ candidate_columns) ≠ ∅:
                Incurs R × UpdateCost (non-HOT)
              Else:
                Incurs 0 (HOT update)
            This guarantees:
              1. An update on (A, B) is counted for index (A,)
              2. An update on (A, B) is counted for index (B,)
              3. An update on (A, B) is counted ONCE for composite index (A, B)
              4. An update on (A, B) is NOT counted for index (C,)
        """
        # Get B-tree height for this candidate
        btree_height = self._estimate_btree_height(table, columns)

        insert_cost = (
            btree_height * costs.random_page_cost
            + costs.cpu_index_tuple_cost
        )
        delete_cost = btree_height * costs.random_page_cost
        update_cost_non_hot = insert_cost + delete_cost

        scale = self._write_scale
        penalty = 0.0

        # INSERT penalty
        penalty += delta.delta_inserts * scale * insert_cost

        # DELETE penalty
        penalty += delta.delta_deletes * scale * delete_cost

        # UPDATE penalty with HOT optimization
        indexed_cols = set(columns)

        if delta.column_set_update_rows:
            # Primary path: exact column-set update tracking
            for col_set, rows in delta.column_set_update_rows.items():
                if col_set & indexed_cols:
                    # Non-HOT: at least one indexed column was updated
                    penalty += rows * scale * update_cost_non_hot
                # else: HOT update (disjoint set) → 0 penalty

        elif delta.column_update_rows:
            # Secondary fallback: single-column stats
            for col, rows in delta.column_update_rows.items():
                if col in indexed_cols:
                    penalty += rows * scale * update_cost_non_hot
        else:
            # Tertiary fallback: whole-table update count (assume non-HOT)
            penalty += delta.delta_updates * scale * update_cost_non_hot

        return penalty

    # ------------------------------------------------------------------
    # PostgreSQL catalog helpers
    # ------------------------------------------------------------------

    def _get_planner_costs(self) -> PlannerCosts:
        """Fetch planner cost constants from pg_settings (cached)."""
        if self._planner_costs is not None:
            return self._planner_costs

        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT name, setting
                FROM pg_settings
                WHERE name IN (
                    'random_page_cost',
                    'cpu_index_tuple_cost',
                    'seq_page_cost'
                )
            """)
            settings = {row[0]: float(row[1]) for row in cur.fetchall()}

        self._planner_costs = PlannerCosts(
            random_page_cost=settings.get('random_page_cost', 4.0),
            cpu_index_tuple_cost=settings.get('cpu_index_tuple_cost', 0.005),
            seq_page_cost=settings.get('seq_page_cost', 1.0),
        )

        logger.info(
            "Planner costs: random_page_cost=%.2f, cpu_index_tuple_cost=%.4f",
            self._planner_costs.random_page_cost,
            self._planner_costs.cpu_index_tuple_cost,
        )

        return self._planner_costs

    def _get_block_size(self) -> int:
        """Return PostgreSQL block size in bytes (cached)."""
        if self._block_size is not None:
            return self._block_size

        with self._conn.cursor() as cur:
            cur.execute("SELECT current_setting('block_size')::int")
            self._block_size = int(cur.fetchone()[0])

        return self._block_size

    def _estimate_btree_height(
        self, table: str, columns: Tuple[str, ...]
    ) -> int:
        """Estimate B-tree height for a proposed index with per-session caching.

        Uses the standard approximation:
            entries_per_page = floor(block_size × fill_factor / tuple_size)
            height = ceil(log(n_live_tup) / log(entries_per_page))

        Args:
            table:   Table name.
            columns: Tuple of column names in the candidate index.

        Returns:
            Estimated height (integer ≥ 1).
        """
        cache_key = (table, tuple(columns))
        if cache_key in self._height_cache:
            return self._height_cache[cache_key]

        block_size = self._get_block_size()

        # Fetch n_live_tup (cached per table)
        if table in self._table_cardinality_cache:
            n_live_tup = self._table_cardinality_cache[table]
        else:
            with self._conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(n_live_tup, 0)::bigint
                    FROM pg_stat_user_tables
                    WHERE relname = %s
                """, (table,))
                row = cur.fetchone()
                n_live_tup = int(row[0]) if row else 0
            self._table_cardinality_cache[table] = n_live_tup

        if n_live_tup <= 1:
            self._height_cache[cache_key] = 1
            return 1

        # Fetch avg_width for each column from pg_stats
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT attname, COALESCE(avg_width, 4)
                FROM pg_stats
                WHERE tablename = %s AND attname = ANY(%s)
            """, (table, list(columns)))
            widths = {row[0]: int(row[1]) for row in cur.fetchall()}

        total_width = sum(widths.get(col, 4) for col in columns)
        tuple_size = total_width + _INDEX_TUPLE_OVERHEAD

        entries_per_page = max(
            1, int(block_size * _BTREE_FILL_FACTOR / tuple_size)
        )
        height = math.ceil(
            math.log(n_live_tup) / math.log(entries_per_page)
        )

        height = max(1, height)
        self._height_cache[cache_key] = height
        return height
