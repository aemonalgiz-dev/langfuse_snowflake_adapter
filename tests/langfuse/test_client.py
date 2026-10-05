import base64
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from langfuse_to_snowflake.entities import Endpoint, build_plan
from langfuse_to_snowflake.langfuse import LangfuseApiError, LangfuseClient, format_timestamp

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)

CURSOR = Endpoint("/api/public/v2/observations", "cursor", "fromStartTime", "toStartTime", 1000)
PAGED = Endpoint("/api/public/traces", "page", "fromTimestamp", "toTimestamp", 100)


def make_client(settings, handler):
    """A client over a mock transport; returns it with the requests and sleeps it made."""
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = LangfuseClient(settings, transport=httpx.MockTransport(record), sleep=sleeps.append)
    return client, requests, sleeps


def test_format_timestamp_is_utc_with_milliseconds():
    local = datetime(2026, 1, 1, 2, 30, 15, 123456, tzinfo=timezone(timedelta(hours=2)))

    assert format_timestamp(local) == "2026-01-01T00:30:15.123Z"
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 1, 1))  # noqa: DTZ001 - naive on purpose


def test_cursor_pagination_follows_the_cursor_until_it_is_absent(langfuse_settings):
    pages = {
        None: {"data": [{"id": "a"}, {"id": "b"}], "meta": {"cursor": "c1"}},
        "c1": {"data": [{"id": "c"}], "meta": {"cursor": "c2"}},
        "c2": {"data": [{"id": "d"}], "meta": {}},
    }
    client, requests, _ = make_client(
        langfuse_settings,
        lambda request: httpx.Response(200, json=pages[request.url.params.get("cursor")]),
    )

    rows = [row["id"] for page in client.iter_pages(CURSOR, START, END) for row in page]

    assert rows == ["a", "b", "c", "d"]
    first = requests[0].url.params
    assert first["fromStartTime"] == "2026-01-01T00:00:00.000Z"
    assert first["toStartTime"] == "2026-01-02T00:00:00.000Z"
    assert first["limit"] == "1000"
    assert "cursor" not in first
    assert [request.url.params.get("cursor") for request in requests] == [None, "c1", "c2"]


def test_a_repeated_cursor_aborts_instead_of_looping_forever(langfuse_settings):
    client, _, _ = make_client(
        langfuse_settings,
        lambda request: httpx.Response(
            200, json={"data": [{"id": "a"}], "meta": {"cursor": "same"}}
        ),
    )

    with pytest.raises(LangfuseApiError, match="same cursor twice"):
        list(client.iter_pages(CURSOR, START, END))


def test_page_pagination_stops_at_total_pages(langfuse_settings):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200, json={"data": [{"id": f"t{page}"}], "meta": {"page": page, "totalPages": 3}}
        )

    client, requests, _ = make_client(langfuse_settings, handler)

    rows = [row["id"] for page in client.iter_pages(PAGED, START, END) for row in page]

    assert rows == ["t1", "t2", "t3"]
    assert [request.url.params["page"] for request in requests] == ["1", "2", "3"]
    assert requests[0].url.params["limit"] == "100"
    assert requests[0].url.params["fromTimestamp"] == "2026-01-01T00:00:00.000Z"


def test_page_pagination_stops_on_an_empty_page(langfuse_settings):
    client, requests, _ = make_client(
        langfuse_settings, lambda request: httpx.Response(200, json={"data": [], "meta": {}})
    )

    assert list(client.iter_pages(PAGED, START, END)) == []
    assert len(requests) == 1


def test_an_endpoint_without_a_time_filter_is_read_in_full(langfuse_settings):
    snapshot = Endpoint("/api/public/comments", "page", None, None, 100)

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200, json={"data": [{"id": f"c{page}"}], "meta": {"page": page, "totalPages": 2}}
        )

    client, requests, _ = make_client(langfuse_settings, handler)

    rows = [row["id"] for page in client.iter_pages(snapshot) for row in page]

    assert rows == ["c1", "c2"]
    assert dict(requests[0].url.params) == {"limit": "100", "page": "1"}


def test_an_endpoint_with_a_time_filter_needs_a_range(langfuse_settings):
    client, requests, _ = make_client(langfuse_settings, lambda request: httpx.Response(200))

    with pytest.raises(ValueError, match="needs a time range"):
        list(client.iter_pages(PAGED))
    assert requests == []


def test_page_size_is_capped_at_the_endpoint_maximum(langfuse_settings):
    settings = langfuse_settings.model_copy(update={"page_size": 250})
    client, requests, _ = make_client(
        settings, lambda request: httpx.Response(200, json={"data": [], "meta": {}})
    )

    list(client.iter_pages(CURSOR, START, END))
    list(client.iter_pages(PAGED, START, END))

    assert [request.url.params["limit"] for request in requests] == ["250", "100"]


def test_endpoint_params_are_sent(langfuse_settings):
    endpoint = build_plan("v4", ["scores"]).extracts[0].endpoint
    client, requests, _ = make_client(
        langfuse_settings, lambda request: httpx.Response(200, json={"data": [], "meta": {}})
    )

    list(client.iter_pages(endpoint, START, END))

    assert requests[0].url.path == "/api/public/v3/scores"
    assert requests[0].url.params["fields"] == "details,subject,annotation"


def test_list_params_are_sent_as_repeated_parameters(langfuse_settings):
    endpoint = Endpoint(
        "/api/public/v2/observations",
        "cursor",
        "fromStartTime",
        "toStartTime",
        1000,
        {"environment": ["production", "staging"], "level": "ERROR"},
    )
    client, requests, _ = make_client(
        langfuse_settings, lambda request: httpx.Response(200, json={"data": [], "meta": {}})
    )

    list(client.iter_pages(endpoint, START, END))

    assert requests[0].url.params.get_list("environment") == ["production", "staging"]
    assert requests[0].url.params["level"] == "ERROR"


def test_requests_use_basic_auth_with_the_project_keys(langfuse_settings):
    client, requests, _ = make_client(
        langfuse_settings,
        lambda request: httpx.Response(200, json={"data": [{"id": "proj-1", "name": "demo"}]}),
    )

    assert client.get_project() == {"id": "proj-1", "name": "demo"}
    expected = base64.b64encode(b"pk-test:sk-test").decode()
    assert requests[0].headers["Authorization"] == f"Basic {expected}"
    assert str(requests[0].url) == "https://langfuse.test/api/public/projects"


def test_get_project_fails_when_the_keys_have_no_project(langfuse_settings):
    client, _, _ = make_client(
        langfuse_settings, lambda request: httpx.Response(200, json={"data": []})
    )

    with pytest.raises(LangfuseApiError, match="not associated"):
        client.get_project()


def test_rate_limits_wait_for_retry_after(langfuse_settings):
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json={"data": [{"id": "proj-1"}]}),
        ]
    )
    client, requests, sleeps = make_client(langfuse_settings, lambda request: next(responses))

    assert client.get_project()["id"] == "proj-1"
    assert sleeps == [7.0]
    assert len(requests) == 2


def test_rate_limits_without_retry_after_back_off(langfuse_settings):
    responses = iter([httpx.Response(429), httpx.Response(200, json={"data": [{"id": "proj-1"}]})])
    client, _, sleeps = make_client(langfuse_settings, lambda request: next(responses))

    assert client.get_project()["id"] == "proj-1"
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 2


def test_a_very_long_retry_after_fails_fast(langfuse_settings):
    client, requests, sleeps = make_client(
        langfuse_settings,
        lambda request: httpx.Response(429, headers={"Retry-After": "3600"}),
    )

    with pytest.raises(LangfuseApiError, match="rate limited for another 3600s"):
        client.get_project()
    assert sleeps == [] and len(requests) == 1


def test_server_errors_and_transport_failures_are_retried_with_backoff(langfuse_settings):
    outcomes = iter(
        [
            httpx.ConnectError("connection refused"),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"data": [{"id": "proj-1"}]}),
        ]
    )

    def handler(request):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client, requests, sleeps = make_client(langfuse_settings, handler)

    assert client.get_project()["id"] == "proj-1"
    assert len(requests) == 3
    assert len(sleeps) == 2 and 1 <= sleeps[0] <= 2 and 2 <= sleeps[1] <= 3


def test_retries_are_bounded(langfuse_settings):
    client, requests, sleeps = make_client(
        langfuse_settings, lambda request: httpx.Response(500, text="boom")
    )

    with pytest.raises(
        LangfuseApiError, match="GET /api/public/projects failed after 4 attempts: HTTP 500: boom"
    ) as error:
        client.get_project()
    assert error.value.status_code == 500
    assert len(requests) == 4 and len(sleeps) == 3


def test_retries_can_be_disabled(langfuse_settings):
    settings = langfuse_settings.model_copy(update={"max_retries": 0})
    client, requests, sleeps = make_client(
        settings, lambda request: httpx.Response(500, text="boom")
    )

    with pytest.raises(LangfuseApiError, match="failed after 1 attempts"):
        client.get_project()
    assert len(requests) == 1 and sleeps == []


def test_client_errors_are_not_retried(langfuse_settings):
    client, requests, sleeps = make_client(
        langfuse_settings, lambda request: httpx.Response(401, json={"message": "Invalid keys"})
    )

    with pytest.raises(LangfuseApiError, match="HTTP 401") as error:
        client.get_project()
    assert error.value.status_code == 401
    assert len(requests) == 1 and sleeps == []


def test_retries_are_logged(langfuse_settings, caplog):
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"data": [{"id": "proj-1"}]}),
        ]
    )
    client, _, _ = make_client(langfuse_settings, lambda request: next(responses))

    with caplog.at_level("WARNING"):
        client.get_project()

    assert "GET /api/public/projects failed (HTTP 429: ); retrying in 2.0s" in caplog.text


def test_deprecation_notices_are_logged_once_per_endpoint(langfuse_settings, caplog):
    payload = {
        "data": [{"id": "t1"}],
        "meta": {"page": 1, "totalPages": 1},
        "_deprecation": {"message": "Use v2 observations.", "sunsetAt": "2026-11-16"},
    }
    client, _, _ = make_client(langfuse_settings, lambda request: httpx.Response(200, json=payload))

    with caplog.at_level("WARNING"):
        list(client.iter_pages(PAGED, START, END))
        list(client.iter_pages(PAGED, START, END))

    warnings = [record.getMessage() for record in caplog.records]
    assert warnings == [
        "/api/public/traces is deprecated (sunset 2026-11-16): Use v2 observations."
    ]
