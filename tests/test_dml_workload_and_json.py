import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from auto_index_selector.Workload.queryLoggerWorkload import (
    extract_workload,
    save_workload_to_json,
    load_workload_from_json,
    CapturedQuery,
    ExtractedWorkload,
    QueryParam,
)
from auto_index_selector.CandidateGeneration.cg_rule_based import generateCandidateIndexes


def test_extract_workload_includes_dml_and_filters_plain_insert(tmp_path):
    """Verify that SELECT, UPDATE, DELETE, and INSERT...SELECT are kept, but plain INSERT...VALUES is excluded."""
    log_file = tmp_path / "query_logger.log"
    out_json = tmp_path / "workload.json"

    log_entries = [
        # SELECT query (kept)
        {"db": "test_db", "tag": "SELECT", "query": "SELECT * FROM customer WHERE c_mktsegment = $1;", "params": [{"param": 1, "type": 25, "val": "BUILDING"}]},
        # UPDATE query (kept)
        {"db": "test_db", "tag": "UPDATE", "query": "UPDATE customer SET c_acctbal = $1 WHERE c_custkey = $2;", "params": [{"param": 1, "type": 1700, "val": "100.5"}, {"param": 2, "type": 23, "val": "42"}]},
        # DELETE query (kept)
        {"db": "test_db", "tag": "DELETE", "query": "DELETE FROM customer WHERE c_phone = $1;", "params": [{"param": 1, "type": 25, "val": "123-456"}]},
        # INSERT ... SELECT query (kept)
        {"db": "test_db", "tag": "INSERT", "query": "INSERT INTO customer_backup SELECT * FROM customer WHERE c_nationkey = $1;", "params": [{"param": 1, "type": 23, "val": "1"}]},
        # Plain INSERT ... VALUES (excluded from W)
        {"db": "test_db", "tag": "INSERT", "query": "INSERT INTO customer VALUES (1, 'name', 'addr');", "params": []},
        # Internal catalog query (excluded)
        {"db": "test_db", "tag": "SELECT", "query": "SELECT * FROM pg_catalog.pg_class;", "params": []},
        # SELECT 1 health check (excluded)
        {"db": "test_db", "tag": "SELECT", "query": "SELECT 1;", "params": []},
        # Other db query (excluded)
        {"db": "other_db", "tag": "SELECT", "query": "SELECT * FROM orders;", "params": []},
    ]

    log_file.write_text("\n".join(json.dumps(e) for e in log_entries) + "\n")

    workload = extract_workload(
        str(log_file),
        db_name="test_db",
        conn=None,
        output_path=str(out_json),
    )

    query_strings = [str(q) for q in workload.queries]
    assert len(query_strings) == 4

    # Check that all 4 expected statements are present
    assert any("SELECT * FROM customer WHERE" in q for q in query_strings)
    assert any("UPDATE customer SET" in q for q in query_strings)
    assert any("DELETE FROM customer WHERE" in q for q in query_strings)
    assert any("INSERT INTO customer_backup SELECT" in q for q in query_strings)

    # Plain INSERT and internal queries must not be in W
    assert not any("INSERT INTO customer VALUES" in q for q in query_strings)
    assert not any("pg_class" in q for q in query_strings)
    assert not any("SELECT 1" in q for q in query_strings)

    # Verify workload.json was created on disk
    assert out_json.is_file()
    saved_data = json.loads(out_json.read_text())
    assert len(saved_data) == 4


def test_save_and_load_workload_json(tmp_path):
    """Verify round-trip serialization and deserialization of ExtractedWorkload."""
    out_json = tmp_path / "workload.json"

    q1 = CapturedQuery(
        "UPDATE customer SET c_acctbal = $1 WHERE c_custkey = $2;",
        params=[QueryParam(position=1, pg_type="numeric", value=150.0), QueryParam(position=2, pg_type="int4", value=99)],
        duration_ms=2.5,
        rows=1,
    )
    q2 = CapturedQuery(
        "DELETE FROM orders WHERE o_orderkey = $1;",
        params=[QueryParam(position=1, pg_type="int4", value=500)],
        duration_ms=1.1,
        rows=1,
    )

    workload = ExtractedWorkload(
        queries=[q1, q2],
        query_weights={q1: 10.0, q2: 3.0},
        schema={"customer": {"c_custkey": "INT", "c_acctbal": "NUMERIC"}},
    )

    save_workload_to_json(workload, str(out_json))
    assert out_json.is_file()

    loaded = load_workload_from_json(str(out_json), conn=None)
    assert len(loaded.queries) == 2
    assert loaded.query_weights[q1] == 10.0
    assert loaded.query_weights[q2] == 3.0

    loaded_q1 = next(q for q in loaded.queries if "UPDATE" in str(q))
    assert len(loaded_q1.params) == 2
    assert loaded_q1.params[0].value == 150.0
    assert loaded_q1.params[1].value == 99


def test_same_query_different_params_stored_separately(tmp_path):
    """Verify that same query template with different parameter values creates distinct entries in W and workload.json."""
    log_file = tmp_path / "query_logger.log"
    out_json = tmp_path / "workload.json"

    log_entries = [
        # Query 1: $1 = 42
        {"db": "test_db", "tag": "SELECT", "query": "SELECT * FROM customer WHERE c_custkey = $1;", "params": [{"param": 1, "type": 23, "val": "42"}]},
        # Query 2: $1 = 99 (same template, different param -> MUST BE SEPARATE)
        {"db": "test_db", "tag": "SELECT", "query": "SELECT * FROM customer WHERE c_custkey = $1;", "params": [{"param": 1, "type": 23, "val": "99"}]},
        # Query 3: $1 = 42 (same template, SAME param -> increments weight of Query 1)
        {"db": "test_db", "tag": "SELECT", "query": "SELECT * FROM customer WHERE c_custkey = $1;", "params": [{"param": 1, "type": 23, "val": "42"}]},
    ]

    log_file.write_text("\n".join(json.dumps(e) for e in log_entries) + "\n")

    workload = extract_workload(
        str(log_file),
        db_name="test_db",
        conn=None,
        output_path=str(out_json),
    )

    # Must produce exactly 2 distinct query entries
    assert len(workload.queries) == 2

    # Query with 42 should have weight 2.0
    q_42 = next(q for q in workload.queries if q.params[0].value == 42)
    q_99 = next(q for q in workload.queries if q.params[0].value == 99)

    assert workload.query_weights[q_42] == 2.0
    assert workload.query_weights[q_99] == 1.0

    # Also verify workload.json stores both
    saved_data = json.loads(out_json.read_text())
    assert len(saved_data) == 2
    item_42 = next(item for item in saved_data if item["params"][0]["value"] == 42)
    item_99 = next(item for item in saved_data if item["params"][0]["value"] == 99)
    assert item_42["weight"] == 2.0
    assert item_99["weight"] == 1.0


def test_candidate_generation_supports_update_and_delete():
    """Verify that cg_rule_based generates candidate indexes for UPDATE and DELETE WHERE clauses."""
    schema = {
        "customer": {"c_custkey": "INT", "c_mktsegment": "TEXT", "c_acctbal": "NUMERIC", "c_phone": "TEXT"},
        "orders": {"o_orderkey": "INT", "o_custkey": "INT", "o_orderstatus": "CHAR"},
    }

    queries = [
        "UPDATE customer SET c_acctbal = 100 WHERE c_mktsegment = 'BUILDING';",
        "DELETE FROM customer WHERE c_phone = '123-456';",
        "DELETE FROM orders WHERE o_orderstatus = 'F';",
    ]

    candidates = generateCandidateIndexes(queries, schema)

    assert "customer" in candidates
    customer_candidates = [tuple(cols) for cols in candidates["customer"]]
    assert ("c_mktsegment",) in customer_candidates
    assert ("c_phone",) in customer_candidates

    assert "orders" in candidates
    orders_candidates = [tuple(cols) for cols in candidates["orders"]]
    assert ("o_orderstatus",) in orders_candidates
