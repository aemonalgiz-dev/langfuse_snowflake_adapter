"""The adapter, run for real: on Snowpark's emulator, and on Snowflake with ``-m live``."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from langfuse_to_snowflake.entities import ENTITIES, build_plan
from langfuse_to_snowflake.snowflake import SchemaConflictError, SnowflakeAdapter, frames
from langfuse_to_snowflake.snowflake.layout import STATE_COLUMNS, entity_columns

OBSERVATIONS = ENTITIES["observations"]
SCORES = ENTITIES["scores"]
SESSIONS = ENTITIES["sessions"]
ALL_GROUPS = ("core", "basic", "time", "trace_context")


def _plan(version="v4", entities=("observations", "scores", "traces", "sessions")):
    return build_plan(version, entities, observation_fields=ALL_GROUPS, expand_metadata=())


def _create(warehouse, entity: str, spec=None, without: tuple[str, ...] = ()) -> None:
    columns = [
        column for column in entity_columns(spec or ENTITIES[entity]) if column[0] not in without
    ]
    frames.empty(warehouse.session, columns).write.save_as_table(warehouse.name(entity))


@pytest.fixture
def adapter(warehouse) -> SnowflakeAdapter:
    adapter = warehouse.adapter()
    adapter.ensure_schema(_plan())
    return adapter


def _score(identifier: str, **fields) -> dict:
    return {
        "id": identifier,
        "name": "accuracy",
        "value": 0.9,
        "timestamp": "2026-03-05T10:00:00.000Z",
        "updatedAt": "2026-03-05T10:00:00.000Z",
        **fields,
    }


def test_load_inserts_typed_rows_and_the_whole_record(adapter, warehouse):
    records = [
        _score("s1", traceId="t1", comment="naïve ✓", environment="production"),
        _score("s2", value="good", dataType="CATEGORICAL", subject={"kind": "SESSION", "id": "x"}),
    ]

    result = adapter.load(SCORES, "proj-1", iter(records))

    assert (result.rows_inserted, result.rows_updated, result.rows_rejected) == (2, 0, 0)
    first, second = warehouse.rows("scores")
    assert first["PROJECT_ID"] == "proj-1"
    assert (first["ID"], first["NAME"], first["COMMENT"]) == ("s1", "accuracy", "naïve ✓")
    assert (first["VALUE_NUMERIC"], first["VALUE_STRING"]) == (0.9, None)
    assert (first["SUBJECT_KIND"], first["TRACE_ID"]) == ("trace", "t1")
    assert frames.as_utc(first["TIMESTAMP"]) == datetime(2026, 3, 5, 10, tzinfo=UTC)
    assert first["RAW"] == records[0]
    assert first["_LOADED_AT"] is not None
    assert (second["VALUE"], second["VALUE_NUMERIC"], second["VALUE_STRING"]) == (
        "good",
        None,
        "good",
    )
    assert (second["SUBJECT_KIND"], second["SESSION_ID"]) == ("session", "x")
    assert second["RAW"] == records[1]


def test_loading_the_same_records_again_changes_nothing(adapter, warehouse):
    records = [_score("s1"), _score("s2")]
    adapter.load(SCORES, "proj-1", records)
    loaded_at = {row["ID"]: row["_LOADED_AT"] for row in warehouse.rows("scores")}

    result = adapter.load(SCORES, "proj-1", records)

    assert (result.rows_inserted, result.rows_updated) == (0, 0)
    assert {row["ID"]: row["_LOADED_AT"] for row in warehouse.rows("scores")} == loaded_at


def test_a_changed_record_updates_its_row_and_only_that_one(adapter, warehouse):
    adapter.load(SCORES, "proj-1", [_score("s1"), _score("s2")])

    edited = _score("s1", value=0.2, comment="re-annotated", updatedAt="2026-03-06T08:00:00Z")
    result = adapter.load(SCORES, "proj-1", [edited, _score("s2")])

    assert (result.rows_inserted, result.rows_updated) == (0, 1)
    first, second = warehouse.rows("scores")
    assert (first["VALUE_NUMERIC"], first["COMMENT"]) == (0.2, "re-annotated")
    assert first["RAW"] == edited
    assert second["VALUE_NUMERIC"] == 0.9


def test_a_field_that_disappears_from_a_record_is_cleared(adapter, warehouse):
    adapter.load(SCORES, "proj-1", [_score("s1", comment="first thoughts")])

    adapter.load(SCORES, "proj-1", [_score("s1")])

    (row,) = warehouse.rows("scores")
    assert row["COMMENT"] is None
    assert "comment" not in row["RAW"]


def test_projects_keep_their_rows_apart(adapter, warehouse):
    adapter.load(SCORES, "proj-1", [_score("s1")])

    result = adapter.load(SCORES, "proj-2", [_score("s1", value=0.1)])

    assert result.rows_inserted == 1
    rows = warehouse.rows("scores", order="PROJECT_ID")
    assert [(row["PROJECT_ID"], row["VALUE_NUMERIC"]) for row in rows] == [
        ("proj-1", 0.9),
        ("proj-2", 0.1),
    ]


def test_observations_are_keyed_on_trace_and_id(adapter, warehouse):
    def observation(trace, identifier, **fields):
        return {"id": identifier, "traceId": trace, "startTime": "2026-03-05T10:00:00Z", **fields}

    adapter.load(OBSERVATIONS, "proj-1", [observation("t1", "o1"), observation("t2", "o1")])
    # A legacy observation may have no trace; it still has to match itself.
    adapter.load(OBSERVATIONS, "proj-1", [observation(None, "o9", name="first")])

    result = adapter.load(
        OBSERVATIONS,
        "proj-1",
        [observation("t1", "o1", name="renamed"), observation(None, "o9", name="second")],
    )

    assert (result.rows_inserted, result.rows_updated) == (0, 2)
    rows = {(row["TRACE_ID"], row["ID"]): row["NAME"] for row in warehouse.rows("observations")}
    assert rows == {("t1", "o1"): "renamed", ("t2", "o1"): None, (None, "o9"): "second"}


def test_the_newest_version_of_a_record_in_a_batch_wins(adapter, warehouse):
    result = adapter.load(
        SCORES,
        "proj-1",
        [
            _score("s1", value=1, updatedAt="2026-03-05T12:00:00Z"),
            _score("s1", value=3, updatedAt="2026-03-05T14:00:00Z"),
            _score("s1", value=2, updatedAt="2026-03-05T13:00:00Z"),
        ],
    )

    assert result.rows_inserted == 1
    (row,) = warehouse.rows("scores")
    assert row["VALUE_NUMERIC"] == 3


def test_load_splits_into_batches_by_row_count(warehouse):
    adapter = warehouse.adapter(batch_max_rows=2)
    adapter.ensure_schema(_plan())
    merges = []
    merge = adapter._merge
    adapter._merge = lambda spec, batch: merges.append(len(batch)) or merge(spec, batch)

    result = adapter.load(SCORES, "proj-1", (_score(f"s{i}") for i in range(5)))

    assert merges == [2, 2, 1]
    assert result.rows_inserted == 5
    assert len(warehouse.rows("scores")) == 5


def test_load_splits_into_batches_by_size(warehouse):
    adapter = warehouse.adapter(batch_max_bytes=300)
    adapter.ensure_schema(_plan())
    merges = []
    merge = adapter._merge
    adapter._merge = lambda spec, batch: merges.append(len(batch)) or merge(spec, batch)

    adapter.load(SCORES, "proj-1", (_score(f"s{i}", comment="x" * 60) for i in range(4)))

    assert merges == [2, 2]


def test_load_with_no_records_does_nothing(adapter, warehouse):
    result = adapter.load(SCORES, "proj-1", iter([]))

    assert (result.rows_inserted, result.rows_updated, result.rows_rejected) == (0, 0, 0)
    assert warehouse.rows("scores") == []


def test_records_that_cannot_be_loaded_are_counted_and_the_rest_carries_on(warehouse, caplog):
    adapter = warehouse.adapter(record_max_bytes=400)
    adapter.ensure_schema(_plan())

    with caplog.at_level("WARNING"):
        result = adapter.load(
            SCORES,
            "proj-1",
            [_score("s1"), {"name": "no id"}, _score("s2", comment="x" * 500), _score("s3")],
        )

    assert (result.rows_inserted, result.rows_rejected) == (2, 2)
    assert result.first_error == "the record has no id"
    assert [row["ID"] for row in warehouse.rows("scores")] == ["s1", "s3"]
    assert "2 record(s) could not be loaded" in caplog.text


def test_a_large_record_arrives_whole(adapter, warehouse):
    # Larger than a statement may be, so it has to travel as data.
    prompt = "The quick brown fox. " * 60_000
    record = _score("big", comment=prompt, metadata={"pieces": ["é", "\\", '"', "\n"]})

    result = adapter.load(SCORES, "proj-1", [record])

    assert result.rows_inserted == 1
    (row,) = warehouse.rows("scores")
    assert len(row["COMMENT"]) == len(prompt) > 1024 * 1024
    assert row["RAW"] == record


def test_ensure_schema_creates_the_tables_then_the_views(warehouse):
    adapter = warehouse.adapter()

    adapter.ensure_schema(_plan())

    names = [
        warehouse.name(n) for n in ("sync_state", "observations", "scores", "traces", "sessions")
    ]
    assert warehouse.catalog.kinds(names) == {
        warehouse.name("sync_state"): "TABLE",
        warehouse.name("observations"): "TABLE",
        warehouse.name("scores"): "TABLE",
        warehouse.name("traces"): "VIEW",
        warehouse.name("sessions"): "VIEW",
    }
    columns = [f.name.strip('"') for f in warehouse.session.table(warehouse.name("scores")).schema]
    assert columns == [name for name, _ in entity_columns(SCORES)]


def test_ensure_schema_on_v3_creates_four_tables_and_no_views(warehouse):
    adapter = warehouse.adapter()

    adapter.ensure_schema(_plan("v3"))

    names = [warehouse.name(n) for n in ("observations", "scores", "traces", "sessions")]
    assert set(warehouse.catalog.kinds(names).values()) == {"TABLE"}
    assert len(warehouse.catalog.kinds(names)) == 4


def test_ensure_schema_keeps_what_is_there(adapter, warehouse):
    adapter.load(SCORES, "proj-1", [_score("s1")])
    adapter.set_watermark("proj-1", "scores", datetime(2026, 3, 6, tzinfo=UTC), "v4")

    adapter.ensure_schema(_plan())

    assert [row["ID"] for row in warehouse.rows("scores")] == ["s1"]
    assert adapter.get_watermarks("proj-1") == {"scores": datetime(2026, 3, 6, tzinfo=UTC)}


def test_ensure_schema_adds_columns_an_older_table_lacks(warehouse):
    _create(warehouse, "scores", without=("COMMENT", "_RAW_HASH"))
    adapter = warehouse.adapter()

    adapter.ensure_schema(_plan())

    columns = {f.name.strip('"') for f in warehouse.session.table(warehouse.name("scores")).schema}
    assert {"COMMENT", "_RAW_HASH"} <= columns
    # Rows loaded before the fingerprint existed are filled in when next read.
    result = adapter.load(SCORES, "proj-1", [_score("s1", comment="kept")])
    assert result.rows_inserted == 1
    assert warehouse.rows("scores")[0]["COMMENT"] == "kept"


def test_a_row_without_a_fingerprint_is_refreshed_when_next_read(adapter, warehouse):
    from snowflake.snowpark import functions as F

    adapter.load(SCORES, "proj-1", [_score("s1")])
    table = warehouse.session.table(warehouse.name("scores"))
    table.update({"_RAW_HASH": F.lit(None)})

    result = adapter.load(SCORES, "proj-1", [_score("s1")])

    assert result.rows_updated == 1
    assert warehouse.rows("scores")[0]["_RAW_HASH"] is not None


def test_ensure_schema_refuses_to_replace_a_legacy_table_with_a_view(warehouse):
    _create(warehouse, "traces")
    adapter = warehouse.adapter()

    with pytest.raises(SchemaConflictError, match="RENAME TO .*TRACES_V3"):
        adapter.ensure_schema(_plan())


def test_ensure_schema_defines_an_existing_view_again(adapter, warehouse):
    adapter.ensure_schema(_plan())

    assert warehouse.catalog.kinds([warehouse.name("traces")]) == {warehouse.name("traces"): "VIEW"}


def test_state_can_be_read_before_anything_was_synced(warehouse):
    adapter = warehouse.adapter()

    assert adapter.get_state("proj-1") == []
    assert adapter.get_watermarks("proj-1") == {}


def test_watermarks_round_trip_in_utc(adapter):
    plus_two = timezone(timedelta(hours=2))
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 15, 14, 30, tzinfo=plus_two), "v4")
    adapter.set_watermark("proj-1", "observations", datetime(2026, 1, 10, tzinfo=UTC), "v4")
    adapter.set_watermark("proj-2", "scores", datetime(2025, 1, 1, tzinfo=UTC), "v3")

    states = adapter.get_state("proj-1")

    assert [state.entity for state in states] == ["observations", "scores"]
    assert states[1].watermark == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    assert states[1].api_version == "v4"
    assert states[1].reconciled_at is None
    assert states[1].updated_at.tzinfo is not None
    assert adapter.get_watermarks("proj-2") == {"scores": datetime(2025, 1, 1, tzinfo=UTC)}


def test_a_watermark_moves_rather_than_piling_up(adapter):
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 10, tzinfo=UTC), "v3")
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 11, tzinfo=UTC), "v4")

    (state,) = adapter.get_state("proj-1")

    assert (state.watermark, state.api_version) == (datetime(2026, 1, 11, tzinfo=UTC), "v4")


def test_reconciled_time_is_recorded_for_synced_entities(adapter):
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 10, tzinfo=UTC), "v4")

    adapter.set_reconciled("proj-1", "scores", datetime(2026, 1, 12, 6, tzinfo=UTC))
    # Never synced, so there is no row to mark and none is made.
    adapter.set_reconciled("proj-1", "observations", datetime(2026, 1, 12, 6, tzinfo=UTC))

    (state,) = adapter.get_state("proj-1")
    assert state.reconciled_at == datetime(2026, 1, 12, 6, tzinfo=UTC)
    # Moving the watermark later leaves it alone.
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 13, tzinfo=UTC), "v4")
    assert adapter.get_state("proj-1")[0].reconciled_at == datetime(2026, 1, 12, 6, tzinfo=UTC)


def test_get_keys_returns_what_is_held_for_a_range(adapter):
    adapter.load(
        OBSERVATIONS,
        "proj-1",
        [
            {"id": "o1", "traceId": "t1", "startTime": "2026-03-05T09:59:59Z"},
            {
                "id": "o2",
                "traceId": "t1",
                "startTime": "2026-03-05T10:00:00Z",
                "updatedAt": "2026-03-05T10:00:05Z",
            },
            {"id": "o3", "traceId": "t2", "startTime": "2026-03-05T10:59:59.999Z"},
            {"id": "o4", "traceId": "t2", "startTime": "2026-03-05T11:00:00Z"},
        ],
    )
    adapter.load(
        OBSERVATIONS, "proj-2", [{"id": "o5", "traceId": "t9", "startTime": "2026-03-05T10:30:00Z"}]
    )

    keys = adapter.get_keys(
        OBSERVATIONS,
        "proj-1",
        datetime(2026, 3, 5, 10, tzinfo=UTC),
        datetime(2026, 3, 5, 11, tzinfo=UTC),
    )

    assert keys == {
        ("t1", "o2"): datetime(2026, 3, 5, 10, 0, 5, tzinfo=UTC),
        ("t2", "o3"): None,
    }


def test_get_keys_for_an_entity_without_updated_at(warehouse):
    adapter = warehouse.adapter()
    adapter.ensure_schema(_plan("v3"))
    adapter.load(SESSIONS, "proj-1", [{"id": "s1", "createdAt": "2026-03-05T10:00:00Z"}])

    assert adapter.get_keys(SESSIONS, "proj-1") == {("s1",): None}


def test_get_keys_without_a_range_returns_everything_held(adapter):
    adapter.load(SCORES, "proj-1", [_score("s1"), _score("s2", timestamp="2020-01-01T00:00:00Z")])

    assert set(adapter.get_keys(SCORES, "proj-1")) == {("s1",), ("s2",)}
    assert adapter.get_keys(SCORES, "proj-2") == {}


def test_earliest_is_the_oldest_timestamp_held(adapter):
    assert adapter.earliest(SCORES, "proj-1") is None

    adapter.load(
        SCORES,
        "proj-1",
        [
            _score("s1"),
            _score("s2", timestamp="2026-02-01T08:00:00+02:00"),
            _score("s3", timestamp=None),
        ],
    )

    assert adapter.earliest(SCORES, "proj-1") == datetime(2026, 2, 1, 6, tzinfo=UTC)
    assert adapter.earliest(SCORES, "proj-2") is None


def test_table_prefix_is_applied_everywhere(warehouse):
    prefix = f"{warehouse.prefix}raw_"
    adapter = warehouse.adapter(table_prefix=prefix)

    adapter.ensure_schema(_plan())
    adapter.load(SCORES, "proj-1", [_score("s1")])
    adapter.set_watermark("proj-1", "scores", datetime(2026, 1, 10, tzinfo=UTC), "v4")

    names = [f"{prefix}{name}".upper() for name in ("SCORES", "SYNC_STATE", "TRACES")]
    assert warehouse.catalog.kinds(names) == dict(
        zip(names, ["TABLE", "TABLE", "VIEW"], strict=True)
    )
    assert adapter.state_table == names[1]
    assert warehouse.session.table(names[0]).count() == 1
    assert warehouse.session.table(names[1]).count() == 1


def test_ping_reports_the_session_context(adapter):
    context = adapter.ping()

    assert set(context) == {"account", "user", "role", "warehouse", "database", "schema"}
    assert all(value and '"' not in value for value in context.values())


def test_the_state_table_has_the_columns_it_should(adapter, warehouse):
    fields = warehouse.session.table(adapter.state_table).schema.fields

    assert [field.name.strip('"') for field in fields] == [name for name, _ in STATE_COLUMNS]


def test_nothing_works_without_connecting_first(local_sessions):
    adapter = SnowflakeAdapter(sessions=local_sessions)

    with pytest.raises(RuntimeError, match="Not connected"):
        adapter.load(SCORES, "proj-1", [_score("s1")])


def test_closing_ends_the_use_of_the_session(local_sessions):
    with SnowflakeAdapter(sessions=local_sessions) as adapter:
        assert adapter.ping()["account"]

    with pytest.raises(RuntimeError, match="Not connected"):
        adapter.ping()
