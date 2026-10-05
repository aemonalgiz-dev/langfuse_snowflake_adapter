from datetime import UTC, datetime, timedelta, timezone

import pytest

from langfuse_to_snowflake.entities import ENTITIES, build_plan
from langfuse_to_snowflake.snowflake import SchemaConflictError, SnowflakeAdapter
from tests.fakes import FakeConnection


@pytest.fixture
def connection() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def adapter(snowflake_settings, connection) -> SnowflakeAdapter:
    return SnowflakeAdapter(snowflake_settings, connection=connection)


def _kinds(connection: FakeConnection) -> list[str]:
    return [statement.split()[0] for statement, _ in connection.statements]


def test_load_stages_copies_and_merges(adapter, connection):
    records = [{"id": "s1", "name": "accuracy", "value": 0.9}, {"id": "s2", "value": "naïve ✓"}]

    result = adapter.load(ENTITIES["scores"], "proj-1", iter(records))

    assert _kinds(connection) == ["CREATE", "TRUNCATE", "PUT", "COPY", "MERGE"]
    (staged,) = connection.staged.values()
    assert staged == records
    merge, params = connection.statements[-1]
    assert merge.startswith("MERGE INTO LANGFUSE_SCORES AS t")
    assert "FROM LANGFUSE_SCORES__LOAD" in merge
    assert params == {"project_id": "proj-1"}
    assert (result.rows_inserted, result.rows_updated, result.rows_rejected) == (2, 0, 0)


def test_load_splits_into_batches_by_row_count(snowflake_settings, connection):
    adapter = SnowflakeAdapter(snowflake_settings, batch_max_rows=2, connection=connection)

    result = adapter.load(ENTITIES["scores"], "proj-1", ({"id": f"s{i}"} for i in range(5)))

    assert sorted(len(rows) for rows in connection.staged.values()) == [1, 2, 2]
    assert len(connection.executed("MERGE")) == 3
    # The load table is created once and emptied before every batch.
    assert len(connection.executed("CREATE TEMPORARY TABLE")) == 1
    assert len(connection.executed("TRUNCATE")) == 3
    assert result.rows_inserted == 5


def test_load_splits_into_batches_by_size(snowflake_settings, connection):
    adapter = SnowflakeAdapter(snowflake_settings, batch_max_bytes=60, connection=connection)

    adapter.load(ENTITIES["scores"], "proj-1", ({"id": f"s{i}", "pad": "x" * 30} for i in range(4)))

    assert sorted(len(rows) for rows in connection.staged.values()) == [2, 2]


def test_load_with_no_records_does_nothing(adapter, connection):
    result = adapter.load(ENTITIES["scores"], "proj-1", iter([]))

    assert connection.statements == []
    assert result.rows_inserted == 0


def test_load_reports_rows_snowflake_rejected(adapter, connection, caplog):
    connection.reject_per_file = 1

    with caplog.at_level("WARNING"):
        result = adapter.load(ENTITIES["scores"], "proj-1", [{"id": "s1"}, {"id": "s2"}])

    assert (result.rows_inserted, result.rows_rejected) == (1, 1)
    assert result.first_error == "Error parsing JSON"
    assert "rejected 1 record(s)" in caplog.text


def test_load_cleans_up_when_the_source_fails_midway(adapter, connection, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    def records():
        yield {"id": "s1"}
        raise RuntimeError("langfuse went away")

    with pytest.raises(RuntimeError, match="langfuse went away"):
        adapter.load(ENTITIES["scores"], "proj-1", records())

    # Nothing half-written is loaded, and the spool directory is gone.
    assert connection.executed("MERGE") == []
    assert list(tmp_path.iterdir()) == []


def test_ensure_schema_creates_tables_then_views(adapter, connection):
    adapter.ensure_schema(build_plan("v4", ["observations", "scores", "traces", "sessions"]))

    created = [statement.splitlines()[0] for statement in connection.executed("CREATE")]
    assert created == [
        "CREATE TABLE IF NOT EXISTS LANGFUSE_SYNC_STATE (",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_OBSERVATIONS (",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_SCORES (",
        "CREATE OR REPLACE VIEW LANGFUSE_TRACES COPY GRANTS AS",
        "CREATE OR REPLACE VIEW LANGFUSE_SESSIONS COPY GRANTS AS",
    ]


def test_ensure_schema_on_v3_creates_four_tables_and_no_views(adapter, connection):
    adapter.ensure_schema(build_plan("v3", ["observations", "scores", "traces", "sessions"]))

    assert len(connection.executed("CREATE TABLE IF NOT EXISTS")) == 5
    assert connection.executed("CREATE OR REPLACE VIEW") == []


def test_ensure_schema_refuses_to_replace_a_legacy_table_with_a_view(adapter, connection):
    connection.object_types["LANGFUSE_TRACES"] = "BASE TABLE"

    with pytest.raises(SchemaConflictError, match="RENAME TO LANGFUSE_TRACES_V3"):
        adapter.ensure_schema(build_plan("v4", ["traces"]))

    assert connection.executed("CREATE OR REPLACE VIEW") == []


def test_ensure_schema_replaces_an_existing_view(adapter, connection):
    connection.object_types["LANGFUSE_TRACES"] = "VIEW"

    adapter.ensure_schema(build_plan("v4", ["traces"]))

    assert len(connection.executed("CREATE OR REPLACE VIEW LANGFUSE_TRACES")) == 1


def test_the_state_table_is_created_once_per_connection(adapter, connection):
    adapter.ensure_schema(build_plan("v4", ["scores"]))
    adapter.get_watermarks("proj-1")
    adapter.get_state("proj-1")

    assert len(connection.executed("CREATE TABLE IF NOT EXISTS LANGFUSE_SYNC_STATE")) == 1
    # A state table from before reconciliation existed gains its column.
    assert connection.executed("ALTER TABLE") == [
        'ALTER TABLE LANGFUSE_SYNC_STATE ADD COLUMN IF NOT EXISTS "RECONCILED_AT" TIMESTAMP_TZ'
    ]


def test_reconciled_time_is_recorded_for_synced_entities(adapter, connection):
    adapter.set_watermark("proj-1", "scores", datetime(2026, 3, 1, tzinfo=UTC), "v4")
    (before,) = adapter.get_state("proj-1")
    assert before.reconciled_at is None

    eastern = timezone(timedelta(hours=-5))
    adapter.set_reconciled("proj-1", "scores", datetime(2026, 3, 2, 7, tzinfo=eastern))
    # An entity that was never synced has no row to record it on.
    adapter.set_reconciled("proj-1", "observations", datetime(2026, 3, 2, tzinfo=UTC))

    (after,) = adapter.get_state("proj-1")
    assert after.reconciled_at == datetime(2026, 3, 2, 12, tzinfo=UTC)
    assert after.watermark == datetime(2026, 3, 1, tzinfo=UTC)


def test_get_keys_returns_what_is_held_for_a_range(adapter, connection):
    updated = datetime(2026, 3, 5, 9, tzinfo=UTC)
    connection.keys["LANGFUSE_OBSERVATIONS"] = [("t1", "o1", updated), (None, "o2", None)]

    keys = adapter.get_keys(
        ENTITIES["observations"],
        "proj-1",
        datetime(2026, 3, 5, tzinfo=UTC),
        datetime(2026, 3, 6, tzinfo=UTC),
    )

    assert keys == {("t1", "o1"): updated, (None, "o2"): None}
    statement, params = connection.statements[-1]
    assert statement == (
        'SELECT "TRACE_ID", "ID", "UPDATED_AT" FROM LANGFUSE_OBSERVATIONS '
        'WHERE "PROJECT_ID" = %(project_id)s '
        'AND "START_TIME" >= %(start)s::TIMESTAMP_TZ AND "START_TIME" < %(end)s::TIMESTAMP_TZ'
    )
    assert params == {
        "project_id": "proj-1",
        "start": "2026-03-05T00:00:00+00:00",
        "end": "2026-03-06T00:00:00+00:00",
    }


def test_get_keys_for_an_entity_without_updated_at(adapter, connection):
    connection.keys["LANGFUSE_SESSIONS"] = [("sess-1", None)]
    day = datetime(2026, 3, 5, tzinfo=UTC)

    assert adapter.get_keys(ENTITIES["sessions"], "proj-1", day, day + timedelta(days=1)) == {
        ("sess-1",): None
    }
    statement, _ = connection.statements[-1]
    assert statement.startswith('SELECT "ID", NULL AS "UPDATED_AT" FROM LANGFUSE_SESSIONS')
    assert '"CREATED_AT" >= %(start)s' in statement


def test_get_keys_without_a_range_returns_everything_held(adapter, connection):
    connection.keys["LANGFUSE_COMMENTS"] = [("c1", None), ("c2", None)]

    assert set(adapter.get_keys(ENTITIES["comments"], "proj-1")) == {("c1",), ("c2",)}
    statement, params = connection.statements[-1]
    assert statement == (
        'SELECT "ID", "UPDATED_AT" FROM LANGFUSE_COMMENTS WHERE "PROJECT_ID" = %(project_id)s'
    )
    assert params == {"project_id": "proj-1"}


def test_earliest_is_the_oldest_timestamp_held(adapter, connection):
    assert adapter.earliest(ENTITIES["scores"], "proj-1") is None

    connection.earliest["LANGFUSE_SCORES"] = datetime(2026, 3, 1, 8, tzinfo=UTC)

    assert adapter.earliest(ENTITIES["scores"], "proj-1") == datetime(2026, 3, 1, 8, tzinfo=UTC)
    statement, params = connection.statements[-1]
    assert statement == (
        'SELECT MIN("TIMESTAMP") AS "EARLIEST" FROM LANGFUSE_SCORES '
        'WHERE "PROJECT_ID" = %(project_id)s'
    )
    assert params == {"project_id": "proj-1"}


def test_watermarks_round_trip_in_utc(adapter):
    eastern = timezone(timedelta(hours=-5))
    adapter.set_watermark("proj-1", "scores", datetime(2026, 3, 1, 7, 0, tzinfo=eastern), "v4")
    adapter.set_watermark("proj-2", "scores", datetime(2026, 1, 1, tzinfo=UTC), "v3")

    assert adapter.get_watermarks("proj-1") == {"scores": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    (state,) = adapter.get_state("proj-2")
    assert (state.entity, state.api_version) == ("scores", "v3")


def test_table_prefix_is_applied_everywhere(snowflake_settings, connection):
    adapter = SnowflakeAdapter(snowflake_settings, table_prefix="lf_", connection=connection)

    adapter.ensure_schema(build_plan("v4", ["traces"]))
    adapter.load(ENTITIES["observations"], "proj-1", [{"id": "o1"}])

    text = "\n".join(statement for statement, _ in connection.statements)
    assert "LANGFUSE_" not in text
    for name in ("LF_SYNC_STATE", "LF_OBSERVATIONS", "LF_OBSERVATIONS__LOAD", "LF_TRACES"):
        assert name in text


def test_statements_need_a_connection(snowflake_settings):
    with pytest.raises(RuntimeError, match="Not connected"):
        SnowflakeAdapter(snowflake_settings).ping()


def test_close_closes_the_connection(adapter, connection):
    adapter.close()

    assert connection.closed
