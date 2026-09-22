import pytest
from pathlib import Path
from unittest.mock import MagicMock

from auto_index_selector.Workload.queryLoggerWorkload import validate_log_path
from auto_index_selector.__main__ import observe_workload


def test_validate_log_path_missing_raises_valueerror():
    """Missing or empty log_path must raise ValueError immediately."""
    with pytest.raises(ValueError) as exc_info:
        validate_log_path(None)
    assert "log_file" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_log_path("")
    assert "log_file" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        validate_log_path("   ")
    assert "log_file" in str(exc_info.value)


def test_validate_log_path_existing_file(tmp_path):
    """Direct filesystem check succeeds when file exists."""
    log_file = tmp_path / "test_query_logger.log"
    log_file.write_text('{"query": "SELECT 1;"}\n')

    resolved = validate_log_path(str(log_file))
    assert resolved == str(log_file.resolve())


def test_validate_log_path_via_postgres_stat():
    """When OS direct access fails (e.g. restricted PGDATA permissions), verify pg_stat_file fallback."""
    fake_conn = MagicMock()
    mock_cursor = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (1024,)

    resolved = validate_log_path("/var/lib/postgresql/16/main/query_logger.log", conn=fake_conn)
    assert resolved == "/var/lib/postgresql/16/main/query_logger.log"
    mock_cursor.execute.assert_called_once()
    assert "pg_stat_file" in mock_cursor.execute.call_args[0][0]


def test_validate_log_path_nonexistent_raises_filenotfound():
    """Nonexistent log file path must raise FileNotFoundError."""
    fake_conn = MagicMock()
    mock_cursor = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.execute.side_effect = Exception("No such file or directory")

    with pytest.raises(FileNotFoundError) as exc_info:
        validate_log_path("/nonexistent/path/query_logger.log", conn=fake_conn)

    assert "query_logger log file not found" in str(exc_info.value)
    fake_conn.rollback.assert_called_once()


def test_observe_workload_raises_on_missing_log_file():
    """observe_workload must raise ValueError if log_file is omitted."""
    fake_conn = MagicMock()

    cfg = {
        "workload": {
            "mode": "custom",
            "log_file": "",
        },
        "write_penalty": {"enabled": False},
    }

    with pytest.raises(ValueError):
        observe_workload(fake_conn, cfg, verbose=False)


def test_observe_workload_raises_on_invalid_log_file():
    """observe_workload must raise FileNotFoundError if log_file does not exist."""
    fake_conn = MagicMock()
    mock_cursor = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.execute.side_effect = Exception("No such file or directory")

    cfg = {
        "workload": {
            "mode": "custom",
            "log_file": "/nonexistent/path/query_logger.log",
        },
        "write_penalty": {"enabled": False},
    }

    with pytest.raises(FileNotFoundError):
        observe_workload(fake_conn, cfg, verbose=False)
