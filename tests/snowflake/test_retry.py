from datetime import UTC, datetime

import pytest
from snowflake.connector import errors

from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.snowflake import SnowflakeAdapter
from langfuse_to_snowflake.snowflake.retry import is_transient
from tests.fakes import FakeConnection


@pytest.fixture
def connection() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def adapter(snowflake_settings, connection, sleeps) -> SnowflakeAdapter:
    return SnowflakeAdapter(snowflake_settings, connection=connection, sleep=sleeps.append)


def _outage() -> Exception:
    return errors.OperationalError(msg="Service temporarily unavailable")


def _kinds(connection: FakeConnection) -> list[str]:
    return [statement.split()[0] for statement, _ in connection.statements]


def test_only_network_and_service_errors_are_transient():
    assert is_transient(errors.OperationalError(msg="network"))
    assert is_transient(errors.ServiceUnavailableError(msg="503"))
    assert not is_transient(errors.ProgrammingError(msg="syntax error"))
    assert not is_transient(errors.NonRetryableTlsError(msg="bad certificate"))
    assert not is_transient(ValueError("not a Snowflake error"))


def test_a_failed_merge_repeats_the_whole_file_from_a_clean_load_table(adapter, connection, sleeps):
    connection.fail_next("MERGE INTO LANGFUSE_SCORES", _outage())

    result = adapter.load(ENTITIES["scores"], "proj-1", [{"id": "s1"}, {"id": "s2"}])

    assert _kinds(connection) == [
        "CREATE",
        *("TRUNCATE", "PUT", "COPY", "MERGE"),
        *("TRUNCATE", "PUT", "COPY", "MERGE"),
    ]
    assert result.rows_inserted == 2
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 2


def test_a_failed_upload_is_retried(adapter, connection, sleeps):
    connection.fail_next("PUT", _outage())

    result = adapter.load(ENTITIES["scores"], "proj-1", [{"id": "s1"}])

    assert _kinds(connection) == ["CREATE", "TRUNCATE", "PUT", "TRUNCATE", "PUT", "COPY", "MERGE"]
    assert result.rows_inserted == 1


def test_statement_errors_are_not_retried(adapter, connection, sleeps):
    connection.fail_next("MERGE INTO LANGFUSE_SCORES", errors.ProgrammingError(msg="bad SQL"))

    with pytest.raises(errors.ProgrammingError):
        adapter.load(ENTITIES["scores"], "proj-1", [{"id": "s1"}])

    assert len(connection.executed("MERGE")) == 1
    assert sleeps == []


def test_retries_are_bounded(adapter, connection, sleeps):
    for _ in range(5):
        connection.fail_next("COPY", _outage())

    with pytest.raises(errors.OperationalError):
        adapter.load(ENTITIES["scores"], "proj-1", [{"id": "s1"}])

    # SNOWFLAKE_MAX_RETRIES defaults to 2: three attempts in total.
    assert len(connection.executed("COPY")) == 3
    assert len(sleeps) == 2
    assert connection.executed("MERGE") == []


def test_retries_can_be_disabled(snowflake_settings, connection, sleeps):
    settings = snowflake_settings.model_copy(update={"max_retries": 0})
    adapter = SnowflakeAdapter(settings, connection=connection, sleep=sleeps.append)
    connection.fail_next("SELECT CURRENT_ACCOUNT", _outage())

    with pytest.raises(errors.OperationalError):
        adapter.ping()
    assert sleeps == []


def test_standalone_statements_are_retried(adapter, connection, sleeps):
    connection.fail_next("MERGE INTO LANGFUSE_SYNC_STATE", _outage())

    adapter.set_watermark("proj-1", "scores", datetime(2026, 3, 1, tzinfo=UTC), "v4")

    assert adapter.get_watermarks("proj-1") == {"scores": datetime(2026, 3, 1, tzinfo=UTC)}
    assert len(sleeps) == 1


def test_connecting_is_retried(snowflake_settings, connection, sleeps, monkeypatch, caplog):
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise _outage()
        return connection

    monkeypatch.setattr("snowflake.connector.connect", connect)
    monkeypatch.setattr(
        "langfuse_to_snowflake.snowflake.adapter.load_private_key", lambda settings: b"der"
    )
    adapter = SnowflakeAdapter(snowflake_settings, sleep=sleeps.append)

    with caplog.at_level("WARNING"), adapter:
        adapter.ping()

    assert len(attempts) == 2 and len(sleeps) == 1
    assert "Snowflake operation failed" in caplog.text
    assert attempts[0]["private_key"] == b"der"
    assert attempts[0]["account"] == "acct"
    assert attempts[0]["schema"] == "LANGFUSE"
    assert attempts[0]["validate_default_parameters"] is True
    assert connection.closed
