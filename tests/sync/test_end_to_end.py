"""The real client, adapter and sync service together: Langfuse behind a fake
HTTP transport, Snowflake as Snowpark's local emulator."""

import json
from datetime import UTC, datetime

import httpx

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.langfuse import LangfuseClient
from langfuse_to_snowflake.snowflake import SnowflakeAdapter
from langfuse_to_snowflake.sync import SyncService

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)
RECENT = "2026-03-09T10:00:00.000Z"


def _langfuse(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path == "/api/public/projects":
        return httpx.Response(200, json={"data": [{"id": "proj-1", "name": "demo"}]})
    if path == "/api/public/v2/observations":
        if params.get("cursor") is None:
            rows = [
                {"id": "o1", "traceId": "t1", "type": "SPAN", "sessionId": "sess-1"},
                {"id": "o2", "traceId": "t1", "type": "GENERATION", "totalCost": 0.002},
            ]
            for row in rows:
                row["startTime"] = RECENT
            return httpx.Response(200, json={"data": rows, "meta": {"cursor": "next"}})
        rows = [
            {
                "id": "o3",
                "traceId": "t2",
                "type": "SPAN",
                "input": '{"q": "hi"}',
                "startTime": RECENT,
            }
        ]
        return httpx.Response(200, json={"data": rows, "meta": {}})
    if path == "/api/public/v3/scores":
        rows = [
            {
                "id": "s1",
                "name": "accuracy",
                "value": 0.9,
                "subject": {"kind": "trace", "id": "t1"},
                "timestamp": RECENT,
            }
        ]
        return httpx.Response(200, json={"data": rows, "meta": {}})
    done = {"page": 1, "totalPages": 1}
    if path == "/api/public/comments":
        rows = [{"id": "c1", "objectType": "TRACE", "objectId": "t1", "content": "looks wrong"}]
        return httpx.Response(200, json={"data": rows, "meta": done})
    if path == "/api/public/annotation-queues":
        rows = [{"id": "q/1", "name": "weekly review"}, {"id": "q2", "name": "empty"}]
        return httpx.Response(200, json={"data": rows, "meta": done})
    # The queue ID contains a slash, so it only matches if it arrives escaped.
    if request.url.raw_path.decode().startswith("/api/public/annotation-queues/q%2F1/items"):
        rows = [{"id": "i1", "queueId": "q/1", "objectType": "TRACE", "objectId": "t1"}]
        return httpx.Response(200, json={"data": rows, "meta": done})
    if path == "/api/public/annotation-queues/q2/items":
        return httpx.Response(200, json={"data": [], "meta": done})
    return httpx.Response(404, json={"message": f"no route for {path}"})


def _adapter(local_sessions) -> SnowflakeAdapter:
    adapter = SnowflakeAdapter(sessions=local_sessions)
    adapter.connect()
    return adapter


def _run(langfuse_settings, local_sessions, **sync):
    """Run one sync and return its result, the adapter and the requests made."""
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _langfuse(request)

    sync_settings = SyncSettings(_env_file=None, **sync)
    adapter = _adapter(local_sessions)
    with LangfuseClient(langfuse_settings, transport=httpx.MockTransport(transport)) as client:
        service = SyncService(langfuse_settings, sync_settings, client, adapter, clock=lambda: NOW)
        return service.run(), adapter, requests


def _rows(session, table: str) -> list[dict]:
    return [row.as_dict() for row in session.table(table).sort("ID").collect()]


def test_v4_sync_end_to_end(langfuse_settings, local_sessions, local_session, local_catalog):
    result, adapter, requests = _run(langfuse_settings, local_sessions, window_hours=24 * 30)

    # Langfuse was asked for every field group over one bounded window.
    observations = next(r for r in requests if r.url.path.endswith("/v2/observations"))
    assert observations.url.params["fromStartTime"] == "2026-02-08T12:00:00.000Z"
    assert observations.url.params["toStartTime"] == "2026-03-10T12:00:00.000Z"
    assert observations.url.params["fields"].split(",")[-1] == "trace_context"

    # A table for the state and for each entity, and the views derived from them.
    names = [
        "LANGFUSE_SYNC_STATE",
        "LANGFUSE_OBSERVATIONS",
        "LANGFUSE_SCORES",
        "LANGFUSE_COMMENTS",
        "LANGFUSE_ANNOTATION_QUEUES",
        "LANGFUSE_ANNOTATION_QUEUE_ITEMS",
        "LANGFUSE_TRACES",
        "LANGFUSE_SESSIONS",
    ]
    assert local_catalog.kinds(names) == dict(zip(names, ["TABLE"] * 6 + ["VIEW"] * 2, strict=True))
    assert "FROM LANGFUSE_OBSERVATIONS" in local_catalog.views["LANGFUSE_TRACES"]

    # The review entities have no time filter: one plain request each, and one per queue.
    review = [r for r in requests if "comments" in r.url.path or "annotation" in r.url.path]
    assert [r.url.raw_path.decode().split("?")[0] for r in review] == [
        "/api/public/comments",
        "/api/public/annotation-queues",
        "/api/public/annotation-queues/q%2F1/items",
        "/api/public/annotation-queues/q2/items",
    ]
    assert all("fromTimestamp" not in r.url.params for r in review)
    assert all(r.url.params["page"] == "1" for r in review)

    # Both pages of observations are in the table, typed, with the records intact.
    loaded = _rows(local_session, "LANGFUSE_OBSERVATIONS")
    assert [(row["PROJECT_ID"], row["TRACE_ID"], row["ID"]) for row in loaded] == [
        ("proj-1", "t1", "o1"),
        ("proj-1", "t1", "o2"),
        ("proj-1", "t2", "o3"),
    ]
    assert (loaded[0]["SESSION_ID"], loaded[1]["TOTAL_COST"]) == ("sess-1", 0.002)
    assert json.loads(loaded[2]["RAW"])["input"] == '{"q": "hi"}'
    (score,) = _rows(local_session, "LANGFUSE_SCORES")
    assert (score["SUBJECT_KIND"], score["TRACE_ID"], score["VALUE_NUMERIC"]) == (
        "trace",
        "t1",
        0.9,
    )
    assert json.loads(score["RAW"])["subject"] == {"kind": "trace", "id": "t1"}
    assert [row["CONTENT"] for row in _rows(local_session, "LANGFUSE_COMMENTS")] == ["looks wrong"]
    assert [row["ID"] for row in _rows(local_session, "LANGFUSE_ANNOTATION_QUEUES")] == [
        "q/1",
        "q2",
    ]
    (item,) = _rows(local_session, "LANGFUSE_ANNOTATION_QUEUE_ITEMS")
    assert (item["ID"], item["QUEUE_ID"]) == ("i1", "q/1")

    # Watermarks were written for everything that was extracted, not for the views.
    states = adapter.get_state("proj-1")
    assert {state.entity: state.watermark for state in states} == dict.fromkeys(
        ("annotation_queue_items", "annotation_queues", "comments", "observations", "scores"), NOW
    )
    assert {state.api_version for state in states} == {"v4"}

    by_entity = {item.entity: item for item in result.entities}
    assert (by_entity["observations"].rows_fetched, by_entity["observations"].rows_inserted) == (
        3,
        3,
    )
    assert (by_entity["scores"].rows_fetched, by_entity["scores"].rows_inserted) == (1, 1)
    items = by_entity["annotation_queue_items"]
    assert (items.snapshot, items.rows_fetched, items.rows_deleted_upstream) == (True, 1, 0)
    assert result.views == ["LANGFUSE_TRACES", "LANGFUSE_SESSIONS"]


def test_a_second_run_reads_only_the_lookback_and_changes_nothing(
    langfuse_settings, local_sessions, local_session
):
    _run(langfuse_settings, local_sessions, entities="scores", window_hours=24 * 30)
    loaded_at = _rows(local_session, "LANGFUSE_SCORES")[0]["_LOADED_AT"]

    result, adapter, requests = _run(
        langfuse_settings, local_sessions, entities="scores", lookback_minutes=60
    )

    scores = [r for r in requests if r.url.path.endswith("/v3/scores")]
    assert len(scores) == 1
    assert scores[0].url.params["fromTimestamp"] == "2026-03-10T11:00:00.000Z"
    assert adapter.get_watermarks("proj-1") == {"scores": NOW}
    # The same record came back; the row was left as it was.
    (entity,) = result.entities
    assert (entity.rows_inserted, entity.rows_updated) == (0, 0)
    assert _rows(local_session, "LANGFUSE_SCORES")[0]["_LOADED_AT"] == loaded_at


def test_reconcile_end_to_end(langfuse_settings, local_sessions, local_session):
    """Snowflake holds o1 as it is, a stale o2 and a deleted o9; o3 was never loaded."""
    day = datetime(2026, 3, 5, tzinfo=UTC)
    listed = [
        {"id": "o1", "traceId": "t1", "startTime": "2026-03-05T01:10:00.000Z"},
        {"id": "o2", "traceId": "t1", "startTime": "2026-03-05T04:20:00.000Z"},
        {"id": "o3", "traceId": "t2", "startTime": "2026-03-05T04:50:00.000Z"},
    ]
    for record in listed:
        record["updatedAt"] = "2026-03-05T08:00:00.000Z"
    adapter = _adapter(local_sessions)
    seed = SyncSettings(_env_file=None, entities="observations")
    with LangfuseClient(langfuse_settings, transport=httpx.MockTransport(_langfuse)) as client:
        SyncService(langfuse_settings, seed, client, adapter, clock=lambda: NOW).init()
    adapter.load(
        ENTITIES["observations"],
        "proj-1",
        [
            listed[0],
            {**listed[1], "name": "before the edit", "updatedAt": "2026-03-05T04:21:00.000Z"},
            {
                "id": "o9",
                "traceId": "t9",
                "startTime": "2026-03-05T06:00:00.000Z",
                "updatedAt": "2026-03-05T06:00:00.000Z",
            },
        ],
    )
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/public/projects":
            return httpx.Response(200, json={"data": [{"id": "proj-1", "name": "demo"}]})
        full = "io" in request.url.params["fields"]
        rows = listed[1:] if full else listed
        return httpx.Response(200, json={"data": rows, "meta": {}})

    with LangfuseClient(langfuse_settings, transport=httpx.MockTransport(transport)) as client:
        service = SyncService(langfuse_settings, seed, client, adapter, clock=lambda: NOW)
        result = service.reconcile(start=day, end=datetime(2026, 3, 6, tzinfo=UTC))

    listing, reread = (r.url.params for r in requests if r.url.path.endswith("/observations"))
    assert listing["fields"] == "core,basic,time,trace_context"
    assert (listing["fromStartTime"], listing["toStartTime"]) == (
        "2026-03-05T00:00:00.000Z",
        "2026-03-06T00:00:00.000Z",
    )
    # Only the hour holding the stale and the missing record is fetched in full.
    assert (reread["fromStartTime"], reread["toStartTime"]) == (
        "2026-03-05T04:00:00.000Z",
        "2026-03-05T05:00:00.000Z",
    )

    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_deleted_upstream) == ("listing", 3, 1)
    assert (outcome.rows_inserted, outcome.rows_updated) == (1, 1)
    held = {row["ID"]: row for row in _rows(local_session, "LANGFUSE_OBSERVATIONS")}
    # The edit arrived, the missing record was loaded, and the deleted one is still there.
    assert sorted(held) == ["o1", "o2", "o3", "o9"]
    assert held["o2"]["NAME"] is None
    assert json.loads(held["o2"]["RAW"]) == listed[1]
    # An explicit range is not recorded as a completed reconcile.
    assert adapter.get_state("proj-1") == []


def test_filters_reach_the_api_and_what_is_loaded(langfuse_settings, local_sessions, local_session):
    result, _, requests = _run(
        langfuse_settings,
        local_sessions,
        entities="observations",
        window_hours=24 * 30,
        filters="observations:type=GENERATION",
    )

    observations = next(r for r in requests if r.url.path.endswith("/v2/observations"))
    assert observations.url.params["type"] == "GENERATION"
    # The fake API ignores the parameter; the local check still drops the spans.
    assert [row["ID"] for row in _rows(local_session, "LANGFUSE_OBSERVATIONS")] == ["o2"]
    assert (result.entities[0].rows_fetched, result.entities[0].rows_skipped) == (3, 2)


def test_fields_left_out_never_reach_snowflake(langfuse_settings, local_sessions, local_session):
    _run(
        langfuse_settings,
        local_sessions,
        entities="observations",
        window_hours=24 * 30,
        exclude_fields="observations:input,observations:sessionId",
    )

    rows = _rows(local_session, "LANGFUSE_OBSERVATIONS")
    assert all("input" not in json.loads(row["RAW"]) for row in rows)
    assert all("sessionId" not in json.loads(row["RAW"]) for row in rows)
    assert {row["SESSION_ID"] for row in rows} == {None}
