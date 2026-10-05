"""The real client, adapter and sync service together, over a fake HTTP transport
and a fake Snowflake connection."""

from datetime import UTC, datetime

import httpx

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.langfuse import LangfuseClient
from langfuse_to_snowflake.snowflake import SnowflakeAdapter
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeConnection

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


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
            return httpx.Response(200, json={"data": rows, "meta": {"cursor": "next"}})
        rows = [{"id": "o3", "traceId": "t2", "type": "SPAN", "input": '{"q": "hi"}'}]
        return httpx.Response(200, json={"data": rows, "meta": {}})
    if path == "/api/public/v3/scores":
        rows = [
            {"id": "s1", "name": "accuracy", "value": 0.9, "subject": {"kind": "trace", "id": "t1"}}
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


def _run(langfuse_settings, snowflake_settings, connection, **sync):
    """Run one sync and return its result, the adapter and the requests made."""
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _langfuse(request)

    sync_settings = SyncSettings(_env_file=None, **sync)
    adapter = SnowflakeAdapter(snowflake_settings, connection=connection)
    with LangfuseClient(langfuse_settings, transport=httpx.MockTransport(transport)) as client:
        service = SyncService(langfuse_settings, sync_settings, client, adapter, clock=lambda: NOW)
        return service.run(), adapter, requests


def test_v4_sync_end_to_end(langfuse_settings, snowflake_settings):
    connection = FakeConnection()

    result, adapter, requests = _run(
        langfuse_settings, snowflake_settings, connection, window_hours=24 * 30
    )

    # Langfuse was asked for every field group over one bounded window.
    observations = next(r for r in requests if r.url.path.endswith("/v2/observations"))
    assert observations.url.params["fromStartTime"] == "2026-02-08T12:00:00.000Z"
    assert observations.url.params["toStartTime"] == "2026-03-10T12:00:00.000Z"
    assert observations.url.params["fields"].split(",")[-1] == "trace_context"

    # Tables, then the views derived from them, then one load table per entity.
    created = [
        statement.split("(")[0].split(" COPY")[0].strip()
        for statement in connection.executed("CREATE")
    ]
    assert created == [
        "CREATE TABLE IF NOT EXISTS LANGFUSE_SYNC_STATE",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_OBSERVATIONS",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_SCORES",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_COMMENTS",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_ANNOTATION_QUEUES",
        "CREATE TABLE IF NOT EXISTS LANGFUSE_ANNOTATION_QUEUE_ITEMS",
        "CREATE OR REPLACE VIEW LANGFUSE_TRACES",
        "CREATE OR REPLACE VIEW LANGFUSE_SESSIONS",
        "CREATE TEMPORARY TABLE IF NOT EXISTS LANGFUSE_OBSERVATIONS__LOAD",
        "CREATE TEMPORARY TABLE IF NOT EXISTS LANGFUSE_SCORES__LOAD",
        "CREATE TEMPORARY TABLE IF NOT EXISTS LANGFUSE_COMMENTS__LOAD",
        "CREATE TEMPORARY TABLE IF NOT EXISTS LANGFUSE_ANNOTATION_QUEUES__LOAD",
        "CREATE TEMPORARY TABLE IF NOT EXISTS LANGFUSE_ANNOTATION_QUEUE_ITEMS__LOAD",
    ]

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

    # Both pages of observations were staged as one file, records intact.
    staged = {name.rsplit("_", 1)[0]: rows for name, rows in connection.staged.items()}
    assert [row["id"] for row in staged["observations"]] == ["o1", "o2", "o3"]
    assert staged["observations"][2]["input"] == '{"q": "hi"}'
    assert staged["scores"][0]["subject"] == {"kind": "trace", "id": "t1"}
    assert [row["content"] for row in staged["comments"]] == ["looks wrong"]
    assert [row["id"] for row in staged["annotation_queues"]] == ["q/1", "q2"]
    assert [row["id"] for row in staged["annotation_queue_items"]] == ["i1"]

    merges = [params for statement, params in connection.statements if "__LOAD\n" in statement]
    assert merges == [{"project_id": "proj-1"}] * 5

    # Watermarks were written for everything that was extracted, not for the views.
    assert adapter.get_watermarks("proj-1") == dict.fromkeys(
        ("annotation_queue_items", "annotation_queues", "comments", "observations", "scores"), NOW
    )
    assert connection.state[("proj-1", "scores")]["api_version"] == "v4"

    by_entity = {item.entity: item for item in result.entities}
    assert (by_entity["observations"].rows_fetched, by_entity["observations"].rows_inserted) == (
        3,
        3,
    )
    assert (by_entity["scores"].rows_fetched, by_entity["scores"].rows_inserted) == (1, 1)
    items = by_entity["annotation_queue_items"]
    assert (items.snapshot, items.rows_fetched, items.rows_deleted_upstream) == (True, 1, 0)
    assert result.views == ["LANGFUSE_TRACES", "LANGFUSE_SESSIONS"]


def test_second_run_is_incremental(langfuse_settings, snowflake_settings):
    connection = FakeConnection()
    seed = SnowflakeAdapter(snowflake_settings, connection=connection)
    seed.set_watermark("proj-1", "scores", datetime(2026, 3, 10, 9, tzinfo=UTC), "v4")

    _, adapter, requests = _run(
        langfuse_settings, snowflake_settings, connection, entities="scores", lookback_minutes=60
    )

    scores = [r for r in requests if r.url.path.endswith("/v3/scores")]
    assert len(scores) == 1
    assert scores[0].url.params["fromTimestamp"] == "2026-03-10T08:00:00.000Z"
    assert adapter.get_watermarks("proj-1") == {"scores": NOW}


def test_reconcile_end_to_end(langfuse_settings, snowflake_settings):
    """Snowflake holds o1 as it is, a stale o2 and a deleted o9; o3 was never loaded."""
    connection = FakeConnection()
    day = datetime(2026, 3, 5, tzinfo=UTC)
    listed = [
        {"id": "o1", "traceId": "t1", "startTime": "2026-03-05T01:10:00.000Z"},
        {"id": "o2", "traceId": "t1", "startTime": "2026-03-05T04:20:00.000Z"},
        {"id": "o3", "traceId": "t2", "startTime": "2026-03-05T04:50:00.000Z"},
    ]
    for record in listed:
        record["updatedAt"] = "2026-03-05T08:00:00.000Z"
    connection.keys["LANGFUSE_OBSERVATIONS"] = [
        ("t1", "o1", datetime(2026, 3, 5, 8, tzinfo=UTC)),
        ("t1", "o2", datetime(2026, 3, 5, 4, 21, tzinfo=UTC)),
        ("t9", "o9", datetime(2026, 3, 5, 6, tzinfo=UTC)),
    ]
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/public/projects":
            return httpx.Response(200, json={"data": [{"id": "proj-1", "name": "demo"}]})
        full = "io" in request.url.params["fields"]
        rows = listed[1:] if full else listed
        return httpx.Response(200, json={"data": rows, "meta": {}})

    sync_settings = SyncSettings(_env_file=None, entities="observations")
    adapter = SnowflakeAdapter(snowflake_settings, connection=connection)
    with LangfuseClient(langfuse_settings, transport=httpx.MockTransport(transport)) as client:
        service = SyncService(langfuse_settings, sync_settings, client, adapter, clock=lambda: NOW)
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
    (staged,) = connection.staged.values()
    assert [row["id"] for row in staged] == ["o2", "o3"]
    assert len(connection.executed("MERGE INTO LANGFUSE_OBSERVATIONS")) == 1

    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_deleted_upstream) == ("listing", 3, 1)
    # An explicit range is not recorded as a completed reconcile.
    assert connection.executed("UPDATE") == []


def test_filters_reach_the_api_and_the_staged_file(langfuse_settings, snowflake_settings):
    connection = FakeConnection()

    result, _, requests = _run(
        langfuse_settings,
        snowflake_settings,
        connection,
        entities="observations",
        window_hours=24 * 30,
        filters="observations:type=GENERATION",
    )

    observations = next(r for r in requests if r.url.path.endswith("/v2/observations"))
    assert observations.url.params["type"] == "GENERATION"
    # The fake API ignores the parameter; the local check still drops the spans.
    (staged,) = connection.staged.values()
    assert [row["id"] for row in staged] == ["o2"]
    assert (result.entities[0].rows_fetched, result.entities[0].rows_skipped) == (3, 2)
