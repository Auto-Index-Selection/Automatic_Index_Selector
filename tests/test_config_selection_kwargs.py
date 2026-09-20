"""
Regression guards for how run_auto_index_selector assembles the kwargs it
forwards to a ConfigSelection module.

The bug these cover: config.toml spells the unconstrained storage budget as the
*string* "inf". That string used to reach cs_extend, whose "is there a limit?"
test compares against float("inf"). A string never equals a float, so the test
passed, and cs_extend replaced the caller's budget_mb with infinity -- silently
turning a storage-budget sweep into five identical unconstrained runs.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_index_selector.__main__ import _as_storage_budget
from auto_index_selector.ConfigSelection import cs_extend


class TestStorageBudgetCoercion:
    @pytest.mark.parametrize("value", ["inf", "Inf", "INF", None, "", "garbage"])
    def test_unconstrained_spellings_become_real_infinity(self, value):
        result = _as_storage_budget(value)
        assert isinstance(result, float)
        assert result == float("inf")

    @pytest.mark.parametrize("value,expected", [
        (104857600, 104857600.0),
        ("104857600", 104857600.0),
        (104857600.0, 104857600.0),
    ])
    def test_numeric_budgets_survive_as_floats(self, value, expected):
        result = _as_storage_budget(value)
        assert isinstance(result, float)
        assert result == expected

    def test_string_inf_is_not_mistaken_for_a_limit(self):
        """The exact comparison cs_extend makes."""
        assert not (_as_storage_budget("inf") != float("inf"))


class TestExtendBudgetPrecedence:
    """An explicit budget_mb must win over storage_budget, as in dropHeuristic."""

    @staticmethod
    def _capture_budget(monkeypatch, **kwargs):
        seen = {}

        class FakeAlgo:
            def __init__(self, **init_kwargs):
                seen.update(init_kwargs)

            def run(self, _candidates):
                return [], None, None

        monkeypatch.setattr(cs_extend, "ExtendAlgorithm", FakeAlgo)
        monkeypatch.setattr(cs_extend, "extractCandidatePool", lambda _d: [])
        cs_extend.extendAlgorithm(conn=None, W=[], candidate_dict={}, **kwargs)
        return seen["budget_mb"]

    def test_explicit_budget_survives_string_inf_storage_budget(self, monkeypatch):
        """The exact shape run_experiments.py sends for the cs_extend sweep."""
        assert self._capture_budget(
            monkeypatch, budget_mb=100.0, storage_budget="inf"
        ) == 100.0

    def test_explicit_budget_survives_float_inf_storage_budget(self, monkeypatch):
        assert self._capture_budget(
            monkeypatch, budget_mb=100.0, storage_budget=float("inf")
        ) == 100.0

    def test_storage_budget_bytes_used_when_no_budget_mb(self, monkeypatch):
        assert self._capture_budget(
            monkeypatch, storage_budget=250 * 1024 * 1024
        ) == 250.0

    def test_falls_back_to_default_when_nothing_supplied(self, monkeypatch):
        assert self._capture_budget(monkeypatch) == 500.0

    def test_budget_sweep_produces_distinct_budgets(self, monkeypatch):
        """The sweep must actually sweep -- this is what silently failed."""
        budgets = [
            self._capture_budget(monkeypatch, budget_mb=float(mb), storage_budget="inf")
            for mb in (100, 250, 500, 1000)
        ]
        assert budgets == [100.0, 250.0, 500.0, 1000.0]
        assert len(set(budgets)) == 4, "all budgets collapsed to one value"
