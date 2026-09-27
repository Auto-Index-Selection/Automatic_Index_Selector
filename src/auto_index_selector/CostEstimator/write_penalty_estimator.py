"""
CostEstimator/write_penalty_estimator.py
-----------------------------------------
B-tree Write Penalty Estimator.
Integrates:
  - B-tree maintenance cost theory (insert, delete, and non-HOT update overheads).
  - PostgreSQL catalog metadata for INSERTS and DELETES (pg_stat_user_tables).
  - Column-set UPDATE tracking strictly from the advisor_write_stats C extension.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple, Optional, FrozenSet, Set

logger = logging.getLogger(__name__)

_INDEX_TUPLE_OVERHEAD: int = 8        # bytes per index tuple (item pointer + header)
_BTREE_FILL_FACTOR: float = 0.9      # default B-tree fill factor


@dataclass
class PlannerCosts:
    """PostgreSQL planner cost constants from pg_settings and buffer cache stats."""
    random_page_cost: float = 4.0
    cpu_index_tuple_cost: float = 0.005
    seq_page_cost: float = 1.0
    cpu_operator_cost: float = 0.0025
    buffer_hit_ratio: float = 0.85


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
    column_set_update_rows: Dict[FrozenSet[str], int] = field(default_factory=dict)

    @property
    def delta_updates(self) -> int:
        return sum(self.column_set_update_rows.values())

    @property
    def total_dml(self) -> int:
        return self.delta_inserts + self.delta_deletes + self.delta_updates


TableDMLDeltaMap = Dict[str, TableDMLDelta]


@dataclass
class Snapshot:
    """Point-in-time snapshot of advisor_write_stats and table insert/delete statistics."""
    column_set_stats: List[ColumnSetUpdateStats]
    table_stats: Dict[str, Tuple[int, int]]


class WritePenaltyEstimator:
    """B-tree write penalty estimator."""

    def __init__(self, conn, write_scale: float = 1.0, cache_hit_ratio: Optional[float] = None):
        self._conn = conn
        self._write_scale = write_scale
        self._cache_hit_ratio = cache_hit_ratio
        self._planner_costs: Optional[PlannerCosts] = None
        self._block_size: Optional[int] = None
        self._height_cache: Dict[Tuple[str, Tuple[str, ...]], int] = {}
        self._table_cardinality_cache: Dict[str, int] = {}

    def ensure_extension(self) -> None:
        """Verify that advisor_write_stats extension is installed and preloaded."""
        with self._conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS advisor_write_stats;")
                self._conn.commit()
                cur.execute("SELECT * FROM advisor_get_column_set_stats() LIMIT 0;")
                self._conn.commit()
            except Exception as e:
                if self._conn:
                    self._conn.rollback()
                raise RuntimeError(
                    f"advisor_write_stats extension is unavailable or not loaded in shared_preload_libraries: {e}"
                ) from e

    def snapshot(self) -> Snapshot:
        """Take a point-in-time snapshot of write statistics."""
        column_set_stats = self._snapshot_extension_column_set_stats()
        table_stats = self._snapshot_table_stats()
        return Snapshot(column_set_stats=column_set_stats, table_stats=table_stats)

    def _snapshot_extension_column_set_stats(self) -> List[ColumnSetUpdateStats]:
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
        stats: Dict[str, Tuple[int, int]] = {}
        with self._conn.cursor() as cur:
            try:
                cur.execute("SELECT pg_stat_force_next_flush();")
            except Exception:
                pass
            try:
                cur.execute("SET stats_fetch_consistency = 'none';")
            except Exception:
                pass
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

    def compute_delta(self, before: Snapshot, after: Snapshot) -> Dict[str, TableDMLDelta]:
        """Compute DML activity delta between two snapshots."""
        deltas: Dict[str, TableDMLDelta] = {}

        # 1. INSERT and DELETE deltas from pg_stat_user_tables
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

        # 2. Per-column-SET UPDATE deltas strictly from advisor_write_stats
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
                    deltas[s.relation_name] = TableDMLDelta(table_name=s.relation_name)
                deltas[s.relation_name].column_set_update_rows[set_key] = row_delta

        return deltas

    def estimate_index_penalty(
        self,
        table: str,
        columns: Tuple[str, ...],
        delta_map: Optional[TableDMLDeltaMap] = None,
    ) -> Dict[str, float]:
        """
        Compute write penalty components for a single (table, columns) index.
        Returns a breakdown dict: {
            'insert_cost': float,
            'delete_cost': float,
            'update_cost': float,
            'total_penalty': float,
            'btree_height': int,
            'is_hot': bool
        }
        """
        costs = self._get_planner_costs()
        btree_height = self._estimate_btree_height(table, columns)

        # ---------------------------------------------------------------------
        # PostgreSQL B-Tree Architecture & Buffer Cache Cost Modeling:
        # 1. Root & Internal levels (height - 1): Permanently cached in shared_buffers.
        #    Traversing them consumes CPU cycles (cpu_operator_cost), not disk I/O.
        # 2. Leaf level insertion/deletion: Weighted by buffer cache hit ratio.
        #    When cached (p_hit), costs standard sequential/cached page cost (seq_page_cost).
        #    When missed (1 - p_hit), physical random page I/O is incurred (random_page_cost).
        # ---------------------------------------------------------------------
        internal_levels = max(0, btree_height - 1)
        traversal_cost = internal_levels * (costs.cpu_operator_cost * 10.0 + costs.cpu_index_tuple_cost)

        p_hit = self._cache_hit_ratio if self._cache_hit_ratio is not None else costs.buffer_hit_ratio
        p_hit = min(0.999, max(0.0, p_hit))
        effective_leaf_cost = (1.0 - p_hit) * costs.random_page_cost + p_hit * costs.seq_page_cost

        unit_insert_cost = traversal_cost + effective_leaf_cost + costs.cpu_index_tuple_cost
        unit_delete_cost = traversal_cost + effective_leaf_cost
        unit_update_cost_non_hot = unit_insert_cost + unit_delete_cost

        scale = self._write_scale
        delta = delta_map.get(table) if delta_map else None

        if delta is None:
            return {
                "insert_cost": 0.0,
                "delete_cost": 0.0,
                "update_cost": 0.0,
                "total_penalty": 0.0,
                "btree_height": btree_height,
                "is_hot": True,
            }

        ins_penalty = delta.delta_inserts * scale * unit_insert_cost
        del_penalty = delta.delta_deletes * scale * unit_delete_cost

        upd_penalty = 0.0
        indexed_cols = set(columns)
        has_non_hot_update = False

        for col_set, rows in delta.column_set_update_rows.items():
            if col_set & indexed_cols:
                # Non-HOT: at least one indexed column was modified
                upd_penalty += rows * scale * unit_update_cost_non_hot
                has_non_hot_update = True
            # else: HOT update -> 0 penalty

        total_penalty = ins_penalty + del_penalty + upd_penalty
        return {
            "insert_cost": ins_penalty,
            "delete_cost": del_penalty,
            "update_cost": upd_penalty,
            "total_penalty": total_penalty,
            "btree_height": btree_height,
            "is_hot": not has_non_hot_update and bool(delta.column_set_update_rows),
        }

    def estimate_penalties(
        self,
        candidate_indexes: Dict[str, list],
        deltas: Dict[str, TableDMLDelta],
    ) -> Dict[Tuple[str, Tuple[str, ...]], float]:
        """Estimate write penalty for each candidate index."""
        penalties: Dict[Tuple[str, Tuple[str, ...]], float] = {}

        for table, col_lists in candidate_indexes.items():
            for cols in col_lists:
                cols_tuple = tuple(cols)
                breakdown = self.estimate_index_penalty(table, cols_tuple, deltas)
                penalties[(table, cols_tuple)] = breakdown["total_penalty"]

        return penalties

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
                res = self.estimate_index_penalty(table, cols_tuple, delta_map)
                memo[key] = res["total_penalty"] if isinstance(res, dict) else float(res)
            return memo[key]

        return _penalty_fn

    def _get_planner_costs(self) -> PlannerCosts:
        if self._planner_costs is not None:
            return self._planner_costs

        costs = PlannerCosts()
        if self._conn is None:
            self._planner_costs = costs
            return costs

        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT name, setting
                FROM pg_settings
                WHERE name IN ('random_page_cost', 'cpu_index_tuple_cost', 'seq_page_cost', 'cpu_operator_cost');
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
                    elif name == "cpu_operator_cost":
                        costs.cpu_operator_cost = v
                except (ValueError, TypeError):
                    pass

            # Detect buffer cache hit ratio from pg_statio_user_indexes if available
            try:
                cur.execute("""
                    SELECT 
                        coalesce(sum(idx_blks_hit)::float / nullif(sum(idx_blks_hit + idx_blks_read), 0), 0.85)
                    FROM pg_statio_user_indexes;
                """)
                row = cur.fetchone()
                if row and row[0] is not None:
                    costs.buffer_hit_ratio = float(row[0])
            except Exception:
                costs.buffer_hit_ratio = 0.85

        self._planner_costs = costs
        return costs

    def _get_block_size(self) -> int:
        if self._block_size is not None:
            return self._block_size
        if self._conn is None:
            self._block_size = 8192
            return 8192

        with self._conn.cursor() as cur:
            cur.execute("SHOW block_size;")
            val = cur.fetchone()[0]
            self._block_size = int(val)

        return self._block_size

    def _estimate_btree_height(self, table: str, columns: Tuple[str, ...]) -> int:
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
        if table in self._table_cardinality_cache:
            return self._table_cardinality_cache[table]
        if self._conn is None:
            return 0

        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT reltuples
                FROM pg_class
                WHERE relname = %s
                  AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public');
            """, (table,))
            row = cur.fetchone()
            cardinality = int(row[0]) if (row and row[0] is not None and row[0] >= 0) else 0

        self._table_cardinality_cache[table] = cardinality
        return cardinality

    def _estimate_index_entry_bytes(self, table: str, columns: Tuple[str, ...]) -> int:
        if self._conn is None:
            return _INDEX_TUPLE_OVERHEAD + max(len(columns) * 8, 8)

        total_width = 0
        with self._conn.cursor() as cur:
            for col in columns:
                cur.execute("""
                    SELECT avg_width
                    FROM pg_stats
                    WHERE tablename = %s AND attname = %s;
                """, (table, col))
                row = cur.fetchone()
                col_width = int(row[0]) if (row and row[0] is not None) else 8
                total_width += col_width

        return _INDEX_TUPLE_OVERHEAD + max(total_width, 8)
