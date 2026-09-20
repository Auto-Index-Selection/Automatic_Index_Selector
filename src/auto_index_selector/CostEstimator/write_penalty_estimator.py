"""
CostEstimator/write_penalty_estimator.py
-----------------------------------------
Strict B-tree write penalty estimator for the Automatic Index Selector pipeline.

Integrates:
  - B-tree maintenance cost theory (insert, delete, and non-HOT update overheads).
  - PostgreSQL catalog metadata for INSERTS and DELETES (pg_stat_user_tables).
  - Column-set UPDATE tracking strictly from the advisor_write_stats C extension.

NO SILENT FALLBACK FOR UPDATES:
  Updates are tracked strictly via advisor_write_stats. If the extension is not
  preloaded in shared_preload_libraries or fails, an explicit RuntimeError is raised
  immediately to halt the pipeline cleanly. Table-level update counters are NEVER used
  as a fallback.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple, Optional, FrozenSet, Set

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
class ColumnSetUpdateStats:
    """Per-column-SET update statistics from advisor_get_column_set_stats()."""
    relation_name: str
    column_set: Tuple[str, ...]
    update_query_count: int
    rows_updated: int


@dataclass
class TableDMLDelta:
    """Aggregated DML delta for a single table across the workload window."""
    table_name: str
    delta_inserts: int = 0
    delta_deletes: int = 0
    # Per-column-set update rows: {frozenset({'colA', 'colB'}): rows_updated_delta}
    # Tracked strictly via advisor_write_stats C extension (NO fallback to table updates).
    column_set_update_rows: Dict[FrozenSet[str], int] = field(default_factory=dict)

    @property
    def delta_updates(self) -> int:
        """Total row updates across all column-sets tracked by advisor_write_stats."""
        return sum(self.column_set_update_rows.values())

    @property
    def total_dml(self) -> int:
        """Total DML modifications (inserts + deletes + column-set updates)."""
        return self.delta_inserts + self.delta_deletes + self.delta_updates


TableDMLDeltaMap = Dict[str, TableDMLDelta]


@dataclass
class Snapshot:
    """Point-in-time snapshot of advisor_write_stats and table insert/delete statistics."""
    column_set_stats: List[ColumnSetUpdateStats]
    # Per-table INSERT and DELETE counters from pg_stat_user_tables:
    #   {table_name: (n_tup_ins, n_tup_del)}
    table_stats: Dict[str, Tuple[int, int]]


# ---------------------------------------------------------------------------
# Main estimator class
# ---------------------------------------------------------------------------

class WritePenaltyEstimator:
    """B-tree write penalty estimator."""

    def __init__(self, conn, write_scale: float = 1.0):
        """Initialise the estimator."""
        self._conn = conn
        self._write_scale = write_scale
        self._planner_costs: Optional[PlannerCosts] = None
        self._block_size: Optional[int] = None
        self._height_cache: Dict[Tuple[str, Tuple[str, ...]], int] = {}
        self._table_cardinality_cache: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Extension management
    # ------------------------------------------------------------------

    def ensure_extension(self) -> None:
        """Verify that advisor_write_stats extension is installed and preloaded."""
        with self._conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats;")
                self._conn.commit()
                # Test whether the extension shared memory hook is active
                cur.execute("SELECT * FROM advisor_get_column_set_stats() LIMIT 0;")
                self._conn.commit()
                logger.info("advisor_write_stats extension verified and operational.")
            except Exception as e:
                if self._conn:
                    self._conn.rollback()
                raise RuntimeError(
                    "advisor_write_stats extension is unavailable or not loaded in shared_preload_libraries. "
                    "Ensure 'advisor_write_stats' is in shared_preload_libraries in postgresql.conf and restart PostgreSQL. "
                    f"Underlying error: {e}"
                ) from e

    # ------------------------------------------------------------------
    # Snapshot: capture current state
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Take a point-in-time snapshot of write statistics."""
        column_set_stats = self._snapshot_extension_column_set_stats()
        table_stats = self._snapshot_table_stats()
        return Snapshot(column_set_stats=column_set_stats, table_stats=table_stats)

    def _snapshot_extension_column_set_stats(self) -> List[ColumnSetUpdateStats]:
        """Query advisor_get_column_set_stats() strictly; fails immediately on error."""
        results: List[ColumnSetUpdateStats] = []
        with self._conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM advisor_get_column_set_stats();")
                for row in cur.fetchall():
                    rel_name = row[0].split('.')[-1] if row[0] else ""
                    raw_cols = row[1]
                    col_set = tuple(raw_cols) if raw_cols else ()

                    results.append(ColumnSetUpdateStats(
                        relation_name=rel_name,
                        column_set=col_set,
                        update_query_count=int(row[2]),
                        rows_updated=int(row[3]),
                    ))
            except Exception as e:
                if self._conn:
                    self._conn.rollback()
                raise RuntimeError(
                    f"advisor_get_column_set_stats() failed: {e}. "
                    "advisor_write_stats must be preloaded via shared_preload_libraries."
                ) from e
        return results

    def _snapshot_table_stats(self) -> Dict[str, Tuple[int, int]]:
        """
        Read cumulative (n_tup_ins, n_tup_del) from pg_stat_user_tables.
        Clears the transaction's stats cache first so deltas are never zeroed.
        """
        stats: Dict[str, Tuple[int, int]] = {}
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot();")
            cur.execute("""
                SELECT relname,
                       COALESCE(n_tup_ins, 0)::bigint,
                       COALESCE(n_tup_del, 0)::bigint
                FROM pg_stat_user_tables;
            """)
            for row in cur.fetchall():
                stats[row[0]] = (int(row[1]), int(row[2]))
        return stats

    # ------------------------------------------------------------------
    # Delta computation
    # ------------------------------------------------------------------

    def compute_delta(
        self, before: Snapshot, after: Snapshot
    ) -> Dict[str, TableDMLDelta]:
        """Compute DML activity delta between two snapshots."""
        deltas: Dict[str, TableDMLDelta] = {}

        # 1. Compute INSERT and DELETE deltas from pg_stat_user_tables
        for table_name, (ins_after, del_after) in after.table_stats.items():
            ins_before, del_before = before.table_stats.get(table_name, (0, 0))
            delta_ins = max(0, ins_after - ins_before)
            delta_del = max(0, del_after - del_before)
            if delta_ins > 0 or delta_del > 0:
                deltas[table_name] = TableDMLDelta(
                    table_name=table_name,
                    delta_inserts=delta_ins,
                    delta_deletes=delta_del,
                )

        # 2. Compute per-column-SET UPDATE deltas strictly from advisor_write_stats
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

        return deltas

    # ------------------------------------------------------------------
    # Penalty estimation (B-tree cost model)
    # ------------------------------------------------------------------

    def estimate_penalties(
        self,
        candidate_indexes: Dict[str, list],
        deltas: Dict[str, TableDMLDelta],
    ) -> Dict[Tuple[str, Tuple[str, ...]], float]:
        """Estimate write penalty for each candidate index."""
        costs = self._get_planner_costs()
        penalties: Dict[Tuple[str, Tuple[str, ...]], float] = {}

        for table, col_lists in candidate_indexes.items():
            delta = deltas.get(table)
            if delta is None:
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

    def estimate_index_penalty(
        self,
        table: str,
        columns: Tuple[str, ...],
        delta_map: Optional[TableDMLDeltaMap] = None,
    ) -> float:
        """Compute write penalty for a single (table, columns) index on demand."""
        if not delta_map:
            return 0.0
        delta = delta_map.get(table)
        if not delta:
            return 0.0
        costs = self._get_planner_costs()
        return self._compute_index_penalty(table, tuple(columns), delta, costs)

    def get_penalty_function(
        self,
        delta_map: Optional[TableDMLDeltaMap] = None,
    ) -> Callable[[str, Tuple[str, ...]], float]:
        """Return a memoized callable (table, columns) -> float penalty for ANY index."""
        memo: Dict[Tuple[str, Tuple[str, ...]], float] = {}

        def _penalty_fn(table: str, columns: Tuple[str, ...]) -> float:
            cols_tuple = tuple(columns) if isinstance(columns, (list, tuple)) else (columns,)
            key = (table, cols_tuple)
            if key not in memo:
                memo[key] = self.estimate_index_penalty(table, cols_tuple, delta_map)
            return memo[key]

        return _penalty_fn

    def _compute_index_penalty(
        self,
        table: str,
        columns: Tuple[str, ...],
        delta: TableDMLDelta,
        costs: PlannerCosts,
    ) -> float:
        """Compute write penalty for a single (table, columns) index."""
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

        # UPDATE penalty strictly from column_set_update_rows (NO fallback to table updates)
        indexed_cols = set(columns)
        for col_set, rows in delta.column_set_update_rows.items():
            if col_set & indexed_cols:
                # Non-HOT: at least one indexed column was modified
                penalty += rows * scale * update_cost_non_hot
            # else: HOT update (disjoint set) → 0 penalty

        return penalty

    # ------------------------------------------------------------------
    # B-tree geometry helpers
    # ------------------------------------------------------------------

    def _get_planner_costs(self) -> PlannerCosts:
        """Fetch PostgreSQL planner cost constants from pg_settings."""
        if self._planner_costs is not None:
            return self._planner_costs

        costs = PlannerCosts()
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
            for name, val in cur.fetchall():
                try:
                    v = float(val)
                    if name == "random_page_cost":
                        costs.random_page_cost = v
                    elif name == "cpu_index_tuple_cost":
                        costs.cpu_index_tuple_cost = v
                    elif name == "seq_page_cost":
                        costs.seq_page_cost = v
                except (ValueError, TypeError):
                    pass

        self._planner_costs = costs
        return costs

    def _get_block_size(self) -> int:
        """Fetch block_size in bytes from pg_settings."""
        if self._block_size is not None:
            return self._block_size

        with self._conn.cursor() as cur:
            cur.execute("SHOW block_size")
            val = cur.fetchone()[0]
            self._block_size = int(val)

        return self._block_size

    def _estimate_btree_height(
        self, table: str, columns: Tuple[str, ...]
    ) -> int:
        """Estimate B-tree height analytically from table cardinality and key width."""
        cache_key = (table, columns)
        if cache_key in self._height_cache:
            return self._height_cache[cache_key]

        total_rows = self._get_table_cardinality(table)
        if total_rows <= 0:
            self._height_cache[cache_key] = 1
            return 1

        avg_entry_bytes = self._estimate_index_entry_bytes(table, columns)
        block_size = self._get_block_size()
        usable_bytes = (block_size - 24) * _BTREE_FILL_FACTOR

        fanout = max(2, int(usable_bytes / avg_entry_bytes))
        height = max(1, math.ceil(math.log(max(1, total_rows), fanout)))

        self._height_cache[cache_key] = height
        return height

    def _get_table_cardinality(self, table: str) -> int:
        """Get live row count estimate from pg_class.reltuples."""
        if table in self._table_cardinality_cache:
            return self._table_cardinality_cache[table]

        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT reltuples
                FROM pg_class
                WHERE relname = %s
                  AND relnamespace = (
                      SELECT oid FROM pg_namespace WHERE nspname = 'public'
                  )
            """, (table,))
            row = cur.fetchone()
            cardinality = int(row[0]) if (row and row[0] is not None and row[0] >= 0) else 0

        self._table_cardinality_cache[table] = cardinality
        return cardinality

    def _estimate_index_entry_bytes(
        self, table: str, columns: Tuple[str, ...]
    ) -> int:
        """Estimate the byte width of a B-tree index entry."""
        total_width = 0
        with self._conn.cursor() as cur:
            for col in columns:
                cur.execute("""
                    SELECT avg_width
                    FROM pg_stats
                    WHERE tablename = %s AND attname = %s
                """, (table, col))
                row = cur.fetchone()
                col_width = int(row[0]) if (row and row[0] is not None) else 8
                total_width += col_width

        return _INDEX_TUPLE_OVERHEAD + max(total_width, 8)
