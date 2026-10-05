"""The two views are SQL the emulator cannot run, so this checks what can be
checked without Snowflake: that they parse as Snowflake SQL and say what they should."""

import json
from datetime import UTC, datetime

import pytest
import sqlglot
from sqlglot import expressions

from langfuse_to_snowflake.entities import ENTITIES, VIEWS, build_plan
from langfuse_to_snowflake.snowflake import frames, views
from langfuse_to_snowflake.snowflake.layout import entity_columns

NAMES = sorted(VIEWS["v4"])


def _statement(name: str) -> str:
    return views.create_view(name, f"LANGFUSE_{name.upper()}", "LANGFUSE_OBSERVATIONS")


def _parsed(name: str) -> expressions.Expression:
    return sqlglot.parse_one(_statement(name), dialect="snowflake")


@pytest.mark.parametrize("name", NAMES)
def test_each_view_parses_as_snowflake_sql(name):
    parsed = _parsed(name)

    assert isinstance(parsed, expressions.Create)
    assert parsed.args["kind"] == "VIEW"
    assert parsed.args["replace"] is True


@pytest.mark.parametrize("name", NAMES)
def test_each_view_keeps_its_grants_when_defined_again(name):
    assert _statement(name).startswith(
        f"CREATE OR REPLACE VIEW LANGFUSE_{name.upper()} COPY GRANTS AS"
    )


@pytest.mark.parametrize("name", NAMES)
def test_each_view_reads_only_columns_the_observations_table_has(name):
    parsed = _parsed(name)
    held = {column for column, _ in entity_columns(ENTITIES["observations"])}

    read = {column.name for column in parsed.find_all(expressions.Column)}
    tables = {table.name for table in parsed.find_all(expressions.Table)}

    assert read <= held, f"{name} reads {sorted(read - held)}"
    assert tables == {f"LANGFUSE_{name.upper()}", "LANGFUSE_OBSERVATIONS"}


def test_views_are_one_row_per_project_and_trace_or_session():
    def grouped(name: str) -> list[str]:
        return [column.name for column in _parsed(name).find(expressions.Group).expressions]

    assert grouped("traces") == ["PROJECT_ID", "TRACE_ID"]
    assert grouped("sessions") == ["PROJECT_ID", "SESSION_ID"]


def test_only_the_views_a_plan_can_ask_for_are_defined():
    with pytest.raises(KeyError):
        views.create_view("scores", "V", "T")


@pytest.mark.live
def test_the_views_rebuild_traces_and_sessions_from_observations(live_warehouse):
    """On Snowflake itself: what the views return for a handful of observations."""
    groups = ("core", "basic", "time", "io", "usage", "metrics", "trace_context")
    plan = build_plan(
        "v4", ("observations", "traces", "sessions"), observation_fields=groups, expand_metadata=()
    )
    adapter = live_warehouse.adapter()
    adapter.ensure_schema(plan)
    shared = {"userId": "u1", "sessionId": "s1", "traceName": "chat", "tags": ["a", "b"]}
    adapter.load(
        ENTITIES["observations"],
        "proj-1",
        [
            {
                "id": "o1",
                "traceId": "t1",
                "name": "root",
                "startTime": "2026-03-05T10:00:00.000Z",
                "endTime": "2026-03-05T10:00:02.500Z",
                "level": "DEFAULT",
                "input": {"q": "hi"},
                "output": "hello",
                "usageDetails": {"input": 5, "output": 7, "total": 12},
                "costDetails": {"total": 0.25},
                **shared,
            },
            {
                "id": "o2",
                "traceId": "t1",
                "name": "llm",
                "parentObservationId": "o1",
                "startTime": "2026-03-05T10:00:01.000Z",
                "endTime": "2026-03-05T10:00:04.000Z",
                "level": "ERROR",
                "usageDetails": {"input": 3, "output": 1, "total": 4},
                "totalCost": 0.5,
                **shared,
            },
            {
                "id": "o3",
                "traceId": "t2",
                "name": "other",
                "startTime": "2026-03-05T12:00:00.000Z",
                "userId": "u2",
                "sessionId": "s1",
            },
        ],
    )

    first, second = live_warehouse.rows("traces")
    (session,) = live_warehouse.rows("sessions")

    assert (first["ID"], first["NAME"], first["USER_ID"], first["SESSION_ID"]) == (
        "t1",
        "chat",
        "u1",
        "s1",
    )
    assert frames.as_utc(first["TIMESTAMP"]) == datetime(2026, 3, 5, 10, tzinfo=UTC)
    assert frames.as_utc(first["END_TIME"]) == datetime(2026, 3, 5, 10, 0, 4, tzinfo=UTC)
    assert first["TAGS"] == ["a", "b"]
    assert (json.loads(first["INPUT"]), first["OUTPUT"]) == ({"q": "hi"}, "hello")
    assert (first["OBSERVATION_COUNT"], first["ERROR_COUNT"]) == (2, 1)
    assert (first["INPUT_TOKENS"], first["OUTPUT_TOKENS"], first["TOTAL_TOKENS"]) == (8, 8, 16)
    assert (float(first["TOTAL_COST"]), float(first["LATENCY"])) == (0.75, 4.0)
    # No trace name on its observations: the root observation names the trace.
    assert (second["ID"], second["NAME"], second["TAGS"], second["INPUT"]) == (
        "t2",
        "other",
        None,
        None,
    )
    assert (second["OBSERVATION_COUNT"], second["ERROR_COUNT"], float(second["LATENCY"])) == (
        1,
        0,
        0.0,
    )

    assert session["ID"] == "s1"
    assert frames.as_utc(session["CREATED_AT"]) == datetime(2026, 3, 5, 10, tzinfo=UTC)
    assert frames.as_utc(session["LAST_ACTIVITY_AT"]) == datetime(2026, 3, 5, 12, tzinfo=UTC)
    users = session["USER_IDS"]
    assert sorted(json.loads(users) if isinstance(users, str) else users) == ["u1", "u2"]
    assert (session["TRACE_COUNT"], session["OBSERVATION_COUNT"]) == (2, 3)
    assert (session["TOTAL_TOKENS"], float(session["TOTAL_COST"])) == (16, 0.75)
