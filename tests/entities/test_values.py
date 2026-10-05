from datetime import UTC, datetime, timedelta, timezone

import pytest

from langfuse_to_snowflake.entities import ENTITIES, column_values
from langfuse_to_snowflake.entities import values as v

OBSERVATIONS = ENTITIES["observations"]
SCORES = ENTITIES["scores"]


def test_lookup_follows_dotted_paths_and_stops_quietly():
    record = {"usage": {"input": 5, "details": {"cached": 2}}, "tags": ["a"], "empty": None}

    assert v.lookup(record, "usage.input") == 5
    assert v.lookup(record, "usage.details.cached") == 2
    assert v.lookup(record, "usage.output") is None
    assert v.lookup(record, "empty.anything") is None
    assert v.lookup(record, "tags.0") is None
    assert v.lookup(record, "missing") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text", "text"),
        ("", ""),
        (None, None),
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (1.5, "1.5"),
        ({"b": 1, "a": [1, 2]}, '{"b":1,"a":[1,2]}'),
        (["x", 1], '["x",1]'),
    ],
)
def test_as_string(value, expected):
    assert v.as_string(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-05T10:00:01.250Z", datetime(2026, 3, 5, 10, 0, 1, 250000, tzinfo=UTC)),
        ("2026-03-05T12:00:00+02:00", datetime(2026, 3, 5, 10, tzinfo=UTC)),
        ("2026-03-05T10:00:00", datetime(2026, 3, 5, 10, tzinfo=UTC)),
        ("2026-03-05", datetime(2026, 3, 5, tzinfo=UTC)),
        (" 2026-03-05T10:00:00Z ", datetime(2026, 3, 5, 10, tzinfo=UTC)),
        ("not a date", None),
        ("", None),
        (None, None),
        (1772704800, None),
        ({"at": "2026-03-05"}, None),
    ],
)
def test_as_timestamp(value, expected):
    assert v.as_timestamp(value) == expected


def test_timestamps_come_out_in_utc_whatever_they_came_in():
    parsed = v.as_timestamp("2026-03-05T12:00:00+02:00")

    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 3, 5, 12, tzinfo=timezone(timedelta(hours=2)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.002, 0.002),
        (3, 3.0),
        ("1.5", 1.5),
        ("1e3", 1000.0),
        (True, None),
        ("abc", None),
        ("", None),
        (None, None),
        ([1], None),
    ],
)
def test_as_float(value, expected):
    assert v.as_float(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12, 12),
        ("12", 12),
        (" 12 ", 12),
        (2.5, 3),
        (-2.5, -3),
        (2.4, 2),
        ("2.5", 3),
        (10**20, 10**20),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        ("NaN", None),
        ("twelve", None),
        (None, None),
        ({"n": 1}, None),
    ],
)
def test_as_number(value, expected):
    assert v.as_number(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("0", False),
        ("off", False),
        (1, True),
        (0, False),
        (2, None),
        ("maybe", None),
        ("", None),
        (None, None),
        ([], None),
    ],
)
def test_as_boolean(value, expected):
    assert v.as_boolean(value) is expected


def test_a_column_takes_the_first_path_that_holds_a_value_of_its_type():
    tokens = next(column for column in OBSERVATIONS.columns if column.name == "INPUT_TOKENS")

    assert v.column_value(tokens, {"inputUsage": 4, "usageDetails": {"input": 9}}) == 4
    assert v.column_value(tokens, {"inputUsage": None, "usageDetails": {"input": 9}}) == 9
    assert v.column_value(tokens, {"usageDetails": None, "usage": {"input": 7}}) == 7
    # A value that is there but is no number does not stop the search.
    assert v.column_value(tokens, {"inputUsage": "n/a", "usage": {"input": 7}}) == 7
    assert v.column_value(tokens, {}) is None


def test_every_column_of_an_entity_gets_a_value_in_order():
    values = column_values(OBSERVATIONS, {"id": "o1", "traceId": "t1"})

    assert list(values) == [column.name for column in OBSERVATIONS.columns]
    assert (values["ID"], values["TRACE_ID"], values["NAME"]) == ("o1", "t1", None)


def test_a_json_null_is_a_null_whatever_the_column_type():
    record = dict.fromkeys(
        ("name", "startTime", "latency", "promptVersion", "tags", "isRootObservation")
    )

    values = column_values(OBSERVATIONS, {"id": "o1", **record})

    assert [
        values[name] for name in ("NAME", "START_TIME", "LATENCY", "PROMPT_VERSION", "TAGS")
    ] == [None] * 5


def test_variant_columns_keep_the_value_as_it_is():
    values = column_values(OBSERVATIONS, {"id": "o1", "tags": ["a", {"b": 1}]})

    assert values["TAGS"] == ["a", {"b": 1}]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"isRootObservation": True, "parentObservationId": "o0"}, True),
        ({"isRootObservation": False}, False),
        ({"isRootObservation": "true"}, True),
        ({"parentObservationId": "o0"}, False),
        ({"parentObservationId": None}, True),
        ({}, True),
    ],
)
def test_the_root_observation_is_the_one_that_says_so_or_has_no_parent(record, expected):
    assert column_values(OBSERVATIONS, {"id": "o1", **record})["IS_ROOT_OBSERVATION"] is expected


@pytest.mark.parametrize(
    ("record", "numeric", "string"),
    [
        ({"value": 0.5}, 0.5, None),
        ({"value": 3}, 3.0, None),
        ({"value": True}, 1.0, "true"),
        ({"value": False}, 0.0, "false"),
        ({"value": "good"}, None, "good"),
        # Scores v2: a number, with the label beside it.
        ({"value": 1, "stringValue": "good"}, 1.0, "good"),
        ({"value": None}, None, None),
        ({"value": {"odd": 1}}, None, None),
        ({}, None, None),
    ],
)
def test_a_score_value_is_split_into_a_number_and_a_string(record, numeric, string):
    values = column_values(SCORES, {"id": "s1", **record})

    assert (values["VALUE_NUMERIC"], values["VALUE_STRING"]) == (numeric, string)
    assert values["VALUE"] == record.get("value")


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # Scores v3 names what was scored under ``subject``.
        (
            {"subject": {"kind": "TRACE", "id": "t1"}},
            ("trace", "t1", None, None, None),
        ),
        (
            {"subject": {"kind": "observation", "id": "o1", "traceId": "t1"}},
            ("observation", "t1", "o1", None, None),
        ),
        ({"subject": {"kind": "session", "id": "x"}}, ("session", None, None, "x", None)),
        ({"subject": {"kind": "experiment", "id": "r1"}}, ("experiment", None, None, None, "r1")),
        # Scores v2 used one flat field per kind.
        ({"traceId": "t1"}, ("trace", "t1", None, None, None)),
        ({"traceId": "t1", "observationId": "o1"}, ("observation", "t1", "o1", None, None)),
        ({"sessionId": "x"}, ("session", None, None, "x", None)),
        ({"datasetRunId": "r1"}, ("experiment", None, None, None, "r1")),
        ({}, (None, None, None, None, None)),
    ],
)
def test_what_a_score_is_attached_to_reads_the_same_from_both_api_generations(record, expected):
    values = column_values(SCORES, {"id": "s1", **record})

    assert (
        values["SUBJECT_KIND"],
        values["TRACE_ID"],
        values["OBSERVATION_ID"],
        values["SESSION_ID"],
        values["EXPERIMENT_ID"],
    ) == expected


def test_no_conversion_ever_fails_on_an_odd_value():
    odd = {"id": "o1", **dict.fromkeys(("name", "startTime", "latency", "tags"), object)}
    odd.update(usage=[1, 2], costDetails="free", promptVersion={"v": 1}, level=["ERROR"])
    odd["name"] = 12
    odd["startTime"] = ["2026"]
    odd["latency"] = {"ms": 1}
    odd["tags"] = "a,b"

    values = column_values(OBSERVATIONS, odd)

    assert values["NAME"] == "12"
    assert values["START_TIME"] is None
    assert values["LATENCY"] is None
    assert values["INPUT_TOKENS"] is None
    assert values["TOTAL_COST"] is None
    assert values["PROMPT_VERSION"] is None
    assert values["LEVEL"] == '["ERROR"]'
    assert values["TAGS"] == "a,b"
