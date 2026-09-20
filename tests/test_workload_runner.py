"""
tests/test_workload_runner.py
-----------------------------
Unit tests for the unified workload_runner package.
"""

import sys
from pathlib import Path
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_index_selector.workload_runner import WorkloadRunner, load_runner_config
from auto_index_selector.workload_runner.param_generator import ParameterGenerator


def test_load_runner_config():
    cfg = load_runner_config()
    assert isinstance(cfg, dict)
    assert "database" in cfg
    assert "queries_path" in cfg
    assert "iterations" in cfg


def test_param_generator_returns_valid_tuples():
    gen = ParameterGenerator(seed=42)
    p1 = gen.generate("update_customer_balance")
    assert isinstance(p1, tuple)
    assert len(p1) == 2

    p2 = gen.generate("insert_lineitem")
    assert isinstance(p2, tuple)
    assert len(p2) == 16


def test_query_discovery_tpch():
    runner = WorkloadRunner({"queries_path": "workloads/tpch"})
    assert len(runner.read_files) == 22
    assert len(runner.dml_files) == 54


def test_query_discovery_pgbench():
    runner = WorkloadRunner({"queries_path": "workloads/pgbench"})
    assert len(runner.read_files) == 10
    assert len(runner.dml_files) == 4


def test_runner_execution_with_mock_conn():
    class _MockCursor:
        def __init__(self):
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            return []

    class _MockConn:
        def __init__(self):
            self.cursor_obj = _MockCursor()
            self.committed = False
            self.rolled_back = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    mock_conn = _MockConn()
    runner = WorkloadRunner({
        "queries_path": "workloads/pgbench",
        "iterations": 2,
        "execute_dml": True,
    })
    stats = runner.run(conn=mock_conn)

    assert stats["iterations"] == 2
    assert stats["executed_reads"] == 20  # 10 queries * 2 rounds
    assert stats["executed_dml"] == 8    # 4 dml * 2 rounds
    assert mock_conn.committed is True
