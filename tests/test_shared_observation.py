"""
Regression guards for the observe-once / select-many split.

The TPC-C suite's central claim is that every (CandidateGeneration,
ConfigSelection) combination is evaluated against the *identical* workload, so
differences between them are attributable to the algorithms rather than to
workload sampling noise. If `select_indexes` ever mutated the shared
observation, or a combination silently re-observed, that claim would quietly
stop holding while the numbers still looked plausible.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import auto_index_selector.__main__ as M
from auto_index_selector.__main__ import WorkloadObservation


class _Snap:
    entries: dict = {}


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the database-facing calls so the split can be tested offline."""
    monkeypatch.setattr(M, "take_snapshot", lambda conn: _Snap())
    monkeypatch.setattr(
        M, "get_delta_workload",
        lambda conn, a, b: (["SELECT 1 FROM customer"], {"customer": ["c_id"]},
                            {"SELECT 1 FROM customer": 7.0}),
    )


class TestObservationHook:
    def test_hook_replaces_the_sleep(self, stub_pipeline, monkeypatch):
        """An inline workload must run *instead of* waiting, not as well as."""
        import time as _time
        slept, ran = [], []
        monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))

        obs = M.observe_workload(
            None,
            {"write_penalty": {"enabled": False, "window_duration_seconds": 30}},
            observation_hook=lambda: ran.append("workload"),
            verbose=False,
        )
        assert ran == ["workload"], "the hook never ran"
        assert slept == [], f"still slept {slept} despite an inline workload"
        assert len(obs.W) == 1

    def test_without_a_hook_the_window_still_sleeps(self, stub_pipeline, monkeypatch):
        """Existing callers (tests/pgbench) must keep their old behaviour."""
        import time as _time
        slept = []
        monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))

        M.observe_workload(
            None,
            {"write_penalty": {"enabled": False, "window_duration_seconds": 30}},
            observation_hook=None,
            verbose=False,
        )
        assert slept == [30]

    def test_empty_observation_is_falsy(self):
        assert not WorkloadObservation()
        assert WorkloadObservation(W=["SELECT 1"])


class TestSharedObservation:
    @staticmethod
    def _cfg(cs_module, **kwargs):
        return {
            "candidate_generation": {"module": "cg_rule_based"},
            "config_selection": {"module": cs_module, **kwargs},
        }

    def test_every_combination_sees_the_same_workload(self, monkeypatch):
        """
        Run several combinations against one observation and assert each was
        handed the identical W and weights -- this is the controlled comparison
        the whole TPC-C design rests on.
        """
        seen = []

        class FakeCG:
            __name__ = "fake_cg"

            @staticmethod
            def generateCandidateIndexes(W, schema):
                # Compare by content, not identity: each stage is handed its own
                # defensive copy, so the objects differ while the workload must not.
                seen.append(("cg", tuple(W),
                             tuple((t, tuple(c)) for t, c in sorted(schema.items()))))
                return {"customer": [("c_id",)]}

        class FakeCS:
            __name__ = "fake_cs"

            @staticmethod
            def selectConfiguration(conn, W, candidates, **kwargs):
                seen.append(("cs", tuple(W), tuple(sorted(kwargs["query_weights"].items()))))
                return frozenset({("customer", ("c_id",))})

        monkeypatch.setattr(
            M, "import_selected_module",
            lambda section, cfg: FakeCG if section == "candidate_generation" else FakeCS,
        )

        observation = WorkloadObservation(
            W=["SELECT 1 FROM customer", "SELECT 2 FROM orders"],
            schema={"customer": ["c_id"]},
            query_weights={"SELECT 1 FROM customer": 7.0, "SELECT 2 FROM orders": 3.0},
        )

        for cs, kwargs in [("config_sel", {"k": 2}), ("cs_drop", {"budget_mb": 100}),
                           ("cs_extend", {"budget_mb": 250})]:
            M.select_indexes(None, self._cfg(cs, **kwargs), observation, verbose=False)

        cg_workloads = {entry[1:] for entry in seen if entry[0] == "cg"}
        cs_workloads = {entry[1:] for entry in seen if entry[0] == "cs"}
        assert len(cg_workloads) == 1, "candidate generation saw differing workloads"
        assert len(cs_workloads) == 1, "configuration selection saw differing workloads"

    def test_selection_does_not_mutate_the_observation(self, monkeypatch):
        """A combination must not leave changes behind for the next one."""
        class Fake:
            __name__ = "fake"

            @staticmethod
            def generateCandidateIndexes(W, schema):
                W.append("INJECTED")          # a badly behaved generator
                return {"customer": [("c_id",)]}

            @staticmethod
            def selectConfiguration(conn, W, candidates, **kwargs):
                return frozenset()

        monkeypatch.setattr(M, "import_selected_module", lambda section, cfg: Fake)
        observation = WorkloadObservation(W=["SELECT 1"], schema={}, query_weights={})
        before = list(observation.W)

        M.select_indexes(None, self._cfg("config_sel"), observation, verbose=False)

        assert observation.W == before, (
            "select_indexes let a stage mutate the shared observation; later "
            "combinations would silently see a different workload"
        )

    def test_empty_observation_selects_nothing(self):
        assert M.select_indexes(None, self._cfg("config_sel"), WorkloadObservation()) == set()

    def test_schema_structure_is_preserved_not_flattened(self, monkeypatch):
        """
        The defensive copy must preserve the schema's real shape.

        get_delta_workload returns {table: {column: type}} plus a "_functions"
        entry. Rebuilding it as {table: list(cols)} silently discards the types,
        and every candidate generator that calls `attrs.keys()` then dies with
        "'list' object has no attribute 'keys'".
        """
        received = {}

        class Fake:
            __name__ = "fake"

            @staticmethod
            def generateCandidateIndexes(W, schema):
                received.update(schema)
                # What cg_rule_based.normalizeColumn actually does:
                for table, attrs in schema.items():
                    if table != "_functions":
                        assert hasattr(attrs, "keys"), (
                            f"schema[{table!r}] arrived as {type(attrs).__name__}, "
                            f"not a mapping"
                        )
                return {"customer": [("c_id",)]}

            @staticmethod
            def selectConfiguration(conn, W, candidates, **kwargs):
                return frozenset()

        monkeypatch.setattr(M, "import_selected_module", lambda section, cfg: Fake)
        observation = WorkloadObservation(
            W=["SELECT 1"],
            schema={"customer": {"c_id": "integer", "c_last": "character varying"},
                    "_functions": {"now": ["timestamptz"]}},
            query_weights={},
        )
        M.select_indexes(None, self._cfg("config_sel"), observation, verbose=False)

        assert received["customer"] == {"c_id": "integer", "c_last": "character varying"}
        assert received["_functions"] == {"now": ["timestamptz"]}
