"""
Regression tests for strict advisor_write_stats tracking without fallbacks.

Ensures:
1. Column-set update statistics are captured accurately from advisor_get_column_set_stats().
2. The delta between before- and after-snapshots accurately computes column_set_update_rows.
3. If advisor_write_stats is missing or fails, RuntimeError is raised immediately without any fallback.
"""

import sys
from pathlib import Path
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_index_selector.CostEstimator.write_penalty_estimator import (
    ColumnSetUpdateStats,
    Snapshot,
    WritePenaltyEstimator,
)


class _MockCursor:
    def __init__(self, dispatch_fn=None, should_raise=False):
        self.dispatch_fn = dispatch_fn
        self.should_raise = should_raise
        self.executed = []
        self._current_rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self.should_raise:
            raise RuntimeError("advisor_write_stats must be preloaded via shared_preload_libraries")
        if self.dispatch_fn:
            self._current_rows = self.dispatch_fn(sql)
        else:
            self._current_rows = []

    def fetchall(self):
        return self._current_rows


class _MockConn:
    def __init__(self, dispatch_fn=None, should_raise=False):
        self.dispatch_fn = dispatch_fn
        self.should_raise = should_raise
        self.rolled_back = False
        self.committed = False

    def cursor(self):
        return _MockCursor(self.dispatch_fn, self.should_raise)

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


class TestAdvisorWriteStatsSnapshot:
    def test_snapshot_captures_column_set_and_table_stats(self):
        def dispatch(sql: str):
            if "advisor_get_column_set_stats" in sql:
                return [
                    ("public.pgbench_accounts", ["abalance"], 5, 20),
                    ("public.pgbench_accounts", ["aid", "bid"], 2, 10),
                ]
            elif "pg_stat_user_tables" in sql:
                return [
                    ("pgbench_accounts", 100, 25),
                    ("pgbench_history", 100, 0),
                ]
            return []

        conn = _MockConn(dispatch_fn=dispatch)
        est = WritePenaltyEstimator(conn)
        snap = est.snapshot()

        # Check column set update stats from advisor_write_stats
        assert len(snap.column_set_stats) == 2
        assert snap.column_set_stats[0].relation_name == "pgbench_accounts"
        assert snap.column_set_stats[0].column_set == ("abalance",)
        assert snap.column_set_stats[0].rows_updated == 20

        # Check insert and delete stats from pg_stat_user_tables
        assert snap.table_stats["pgbench_accounts"] == (100, 25)
        assert snap.table_stats["pgbench_history"] == (100, 0)

    def test_compute_delta_calculates_column_set_updates_and_deletes(self):
        est = WritePenaltyEstimator(None)
        before = Snapshot(
            column_set_stats=[
                ColumnSetUpdateStats("pgbench_accounts", ("abalance",), 5, 20),
            ],
            table_stats={
                "pgbench_accounts": (100, 20),
                "pgbench_tellers": (5, 2),
            }
        )
        after = Snapshot(
            column_set_stats=[
                ColumnSetUpdateStats("pgbench_accounts", ("abalance",), 8, 35),
                ColumnSetUpdateStats("pgbench_accounts", ("aid", "bid"), 2, 10),
            ],
            table_stats={
                "pgbench_accounts": (150, 45),  # +50 ins, +25 del
                "pgbench_tellers": (5, 2),     # +0 ins, +0 del
            }
        )

        deltas = est.compute_delta(before, after)
        assert "pgbench_accounts" in deltas
        acct_delta = deltas["pgbench_accounts"]

        # Assert insert and delete deltas from pg_stat_user_tables
        assert acct_delta.delta_inserts == 50
        assert acct_delta.delta_deletes == 25

        # Assert update deltas strictly from advisor_write_stats
        assert acct_delta.column_set_update_rows[frozenset({"abalance"})] == 15
        assert acct_delta.column_set_update_rows[frozenset({"aid", "bid"})] == 10

        # Untouched tables have no delta entries
        assert "pgbench_tellers" not in deltas

    def test_no_fallback_raises_error_if_extension_fails(self):
        """Strict fail-fast: if advisor_get_column_set_stats fails, raise RuntimeError immediately."""
        conn = _MockConn(should_raise=True)
        est = WritePenaltyEstimator(conn)

        with pytest.raises(RuntimeError, match="advisor_write_stats must be preloaded"):
            est.snapshot()

        assert conn.rolled_back, "Transaction must be rolled back on failure"


class TestWritePenaltyEstimatorCostModel:
    def test_cost_model_breakdown_and_hot_detection(self):
        est = WritePenaltyEstimator(None, write_scale=1.0, cache_hit_ratio=0.9)
        # Mock table with 10 inserts, 5 deletes, 20 updates on col_a
        from auto_index_selector.CostEstimator.write_penalty_estimator import TableDMLDelta
        delta = TableDMLDelta(
            table_name="test_tbl",
            delta_inserts=10,
            delta_deletes=5,
            column_set_update_rows={
                frozenset({"col_a"}): 20,
            },
        )
        delta_map = {"test_tbl": delta}

        # 1. Non-HOT: index on col_a
        breakdown_non_hot = est.estimate_index_penalty("test_tbl", ("col_a",), delta_map)
        assert "insert_cost" in breakdown_non_hot
        assert "delete_cost" in breakdown_non_hot
        assert "update_cost" in breakdown_non_hot
        assert "total_penalty" in breakdown_non_hot
        assert "btree_height" in breakdown_non_hot
        assert breakdown_non_hot["is_hot"] is False
        assert breakdown_non_hot["update_cost"] > 0
        assert breakdown_non_hot["total_penalty"] == (
            breakdown_non_hot["insert_cost"] + breakdown_non_hot["delete_cost"] + breakdown_non_hot["update_cost"]
        )

        # 2. HOT: index on col_b (disjoint from updated set col_a)
        breakdown_hot = est.estimate_index_penalty("test_tbl", ("col_b",), delta_map)
        assert breakdown_hot["is_hot"] is True
        assert breakdown_hot["update_cost"] == 0.0
        assert breakdown_hot["total_penalty"] == breakdown_hot["insert_cost"] + breakdown_hot["delete_cost"]

        # 3. get_penalty_function returns float and matches total_penalty
        fn = est.get_penalty_function(delta_map)
        assert callable(fn)
        pen_float = fn("test_tbl", ("col_a",))
        assert isinstance(pen_float, float)
        assert pen_float == breakdown_non_hot["total_penalty"]

