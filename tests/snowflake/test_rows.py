from datetime import UTC, datetime

import pytest

from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.snowflake import rows
from langfuse_to_snowflake.snowflake.layout import batch_columns

SCORES = ENTITIES["scores"]
OBSERVATIONS = ENTITIES["observations"]
LIMITS = {"max_rows": 100, "max_bytes": 10_000_000, "max_record_bytes": 1_000_000}


def _batches(spec, records, **limits):
    return list(rows.batches(spec, "proj-1", records, **{**LIMITS, **limits}))


def _column(spec, row, name: str):
    names = [column for column, _ in batch_columns(spec)]
    return row[names.index(name)]


def test_a_row_holds_the_project_the_typed_columns_the_record_and_its_fingerprint():
    record = {"id": "s1", "name": "accuracy", "value": 0.5, "timestamp": "2026-03-05T10:00:00Z"}

    key, order, row = rows.prepare(SCORES, "proj-1", record, 1_000_000)

    assert key == ("s1",)
    assert order is None  # no updatedAt
    assert len(row) == len(batch_columns(SCORES))
    assert _column(SCORES, row, "PROJECT_ID") == "proj-1"
    assert _column(SCORES, row, "NAME") == "accuracy"
    assert _column(SCORES, row, "VALUE_NUMERIC") == 0.5
    assert _column(SCORES, row, "TIMESTAMP") == datetime(2026, 3, 5, 10, tzinfo=UTC)
    assert _column(SCORES, row, "RAW_JSON") == rows.serialize(record)
    assert _column(SCORES, row, "_RAW_HASH") == rows.fingerprint(rows.serialize(record))


def test_the_fingerprint_ignores_key_order_and_sees_every_change():
    one = rows.serialize({"id": "s1", "value": 1, "nested": {"a": 1, "b": [1, 2]}})
    same = rows.serialize({"nested": {"b": [1, 2], "a": 1}, "value": 1, "id": "s1"})
    other = rows.serialize({"id": "s1", "value": 1, "nested": {"a": 1, "b": [2, 1]}})

    assert rows.fingerprint(one) == rows.fingerprint(same)
    assert rows.fingerprint(one) != rows.fingerprint(other)
    assert len(rows.fingerprint(one)) == 32


def test_the_record_travels_as_plain_ascii_json():
    text = rows.serialize({"id": "s1", "comment": "naïve ✓ 日本"})

    assert text.isascii()
    assert rows.fingerprint(text)


def test_a_record_without_an_id_cannot_be_loaded():
    with pytest.raises(rows.Rejected, match="no id"):
        rows.prepare(SCORES, "proj-1", {"name": "accuracy"}, 1_000_000)


def test_a_record_over_the_size_limit_cannot_be_loaded():
    with pytest.raises(rows.Rejected, match=r"record s1 is \d+ bytes, over the limit of 50"):
        rows.prepare(SCORES, "proj-1", {"id": "s1", "comment": "x" * 100}, 50)


def test_batches_are_cut_by_row_count():
    cut = _batches(SCORES, ({"id": f"s{i}"} for i in range(5)), max_rows=2)

    assert [len(batch.rows) for batch in cut] == [2, 2, 1]


def test_batches_are_cut_by_size():
    records = [{"id": f"s{i}", "comment": "x" * 100} for i in range(4)]

    cut = _batches(SCORES, records, max_bytes=220)

    assert [len(batch.rows) for batch in cut] == [2, 2]


def test_no_records_make_no_batches():
    assert _batches(SCORES, []) == []


def test_records_are_read_lazily_one_batch_at_a_time():
    read = []

    def records():
        for index in range(4):
            read.append(index)
            yield {"id": f"s{index}"}

    cut = rows.batches(SCORES, "proj-1", records(), **{**LIMITS, "max_rows": 2})

    next(cut)
    assert read == [0, 1]


def test_a_batch_holds_each_key_once_as_its_newest_version():
    records = [
        {"id": "s1", "value": 1, "updatedAt": "2026-03-05T12:00:00Z"},
        {"id": "s2", "value": 9},
        {"id": "s1", "value": 3, "updatedAt": "2026-03-05T14:00:00Z"},
        {"id": "s1", "value": 2, "updatedAt": "2026-03-05T13:00:00Z"},
    ]

    (batch,) = _batches(SCORES, records)

    values = {_column(SCORES, row, "ID"): _column(SCORES, row, "VALUE") for row in batch.rows}
    assert values == {"s1": 3, "s2": 9}


def test_between_versions_that_cannot_be_ordered_the_last_one_read_wins():
    records = [
        {"id": "s1", "value": 1},
        {"id": "s1", "value": 2},
        # One with a time always beats one without.
        {"id": "s2", "value": 1, "updatedAt": "2026-03-05T12:00:00Z"},
        {"id": "s2", "value": 2},
        {"id": "s3", "value": 1, "updatedAt": "2026-03-05T12:00:00Z"},
        {"id": "s3", "value": 2, "updatedAt": "2026-03-05T12:00:00Z"},
    ]

    (batch,) = _batches(SCORES, records)

    values = {_column(SCORES, row, "ID"): _column(SCORES, row, "VALUE") for row in batch.rows}
    assert values == {"s1": 2, "s2": 1, "s3": 2}


def test_the_same_id_in_two_traces_is_two_observations():
    records = [{"id": "o1", "traceId": "t1"}, {"id": "o1", "traceId": "t2"}]

    (batch,) = _batches(OBSERVATIONS, records)

    assert len(batch.rows) == 2


def test_rejected_records_are_counted_with_the_first_reason():
    records = [{"id": "s1"}, {"name": "no id"}, {"id": "s2", "comment": "x" * 200}, {"id": "s3"}]

    (batch,) = _batches(SCORES, records, max_record_bytes=100)

    assert len(batch.rows) == 2
    assert batch.rows_rejected == 2
    assert batch.first_error == "the record has no id"


def test_a_batch_of_nothing_but_rejected_records_still_reports_them():
    (batch,) = _batches(SCORES, [{"name": "no id"}])

    assert (batch.rows, batch.rows_rejected) == ([], 1)
