import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.config import ApiSettings, SyncSettings
from langfuse_to_snowflake.snowflake import EntityState
from langfuse_to_snowflake.sync import EntityResult, ReconcileResult, SyncResult

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


def _result(rows: int) -> SyncResult:
    return SyncResult(
        project_id="proj-1",
        api_version="v4",
        started_at=NOW,
        entities=[
            EntityResult(
                entity="scores",
                table="LANGFUSE_SCORES",
                window_start=NOW,
                window_end=NOW,
                rows_fetched=rows,
            )
        ],
    )


def _reconciled() -> SyncResult:
    outcome = ReconcileResult(
        mode="full", window_start=NOW, window_end=NOW, rows_compared=40, rows_updated=3
    )
    return SyncResult(
        project_id="proj-1",
        api_version="v4",
        started_at=NOW,
        entities=[EntityResult(entity="scores", table="LANGFUSE_SCORES", reconcile=outcome)],
    )


class FakeBackend:
    def __init__(self) -> None:
        self.requests = []
        self.reconcile_requests = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.error: Exception | None = None

    def run_sync(self, request, progress):
        self.requests.append(request)
        progress(_result(1))
        self.started.set()
        assert self.release.wait(5)
        if self.error:
            raise self.error
        return _result(2)

    def run_reconcile(self, request, progress):
        self.reconcile_requests.append(request)
        return _reconciled()

    def read_state(self, project):
        return [EntityState("scores", NOW, "v4", NOW, reconciled_at=NOW)]


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def client(settings, backend):
    with TestClient(create_app(settings, backend)) as test_client:
        yield test_client


def _finish(client: TestClient, run_id: str) -> dict:
    client.app.state.runs.wait(run_id, timeout=5)
    return client.get(f"/runs/{run_id}").json()


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sync_runs_in_the_background_and_reports_its_result(client, backend):
    response = client.post("/sync")

    assert response.status_code == 202
    queued = response.json()
    assert queued["kind"] == "sync"
    assert queued["status"] in {"queued", "running", "succeeded"}

    run = _finish(client, queued["id"])
    assert run["status"] == "succeeded"
    assert run["result"]["entities"][0]["rows_fetched"] == 2
    assert run["started_at"] and run["finished_at"]
    request = backend.requests[0]
    assert (request.entities, request.sample_rate, request.filters) == (None, None, None)


def test_sync_passes_the_requested_range_and_entities(client, backend):
    body = {
        "entities": ["scores"],
        "from": "2026-03-01T00:00:00Z",
        "to": "2026-03-02T00:00:00+00:00",
    }

    run = _finish(client, client.post("/sync", json=body).json()["id"])

    request = backend.requests[0]
    assert request.entities == ["scores"]
    assert request.start == datetime(2026, 3, 1, tzinfo=UTC)
    assert request.end == datetime(2026, 3, 2, tzinfo=UTC)
    assert run["request"]["entities"] == ["scores"]


def test_sync_passes_sampling_and_filters(client, backend):
    body = {"sample_rate": 0.1, "filters": ["environment = production", "scores:name=accuracy"]}

    run = _finish(client, client.post("/sync", json=body).json()["id"])

    request = backend.requests[0]
    assert request.sample_rate == 0.1
    assert request.filters == ["environment=production", "scores:name=accuracy"]
    assert run["request"]["filters"] == ["environment=production", "scores:name=accuracy"]


def test_an_empty_filter_list_is_passed_on_as_no_filters(client, backend):
    _finish(client, client.post("/sync", json={"filters": []}).json()["id"])

    assert backend.requests[0].filters == []


@pytest.mark.parametrize(
    "body",
    [
        {"entities": ["prompts"]},
        {"entities": []},
        {"from": "2026-03-02T00:00:00Z", "to": "2026-03-01T00:00:00Z"},
        {"from": "2026-03-02T00:00:00"},  # no timezone
        {"since": "2026-03-01T00:00:00Z"},  # unknown field
        {"sample_rate": 0},
        {"sample_rate": 1.5},
        {"filters": ["just some words"]},
        {"filters": ["prompts:name=x"]},
        {"full": True},  # belongs to /reconcile
    ],
)
def test_invalid_sync_requests_are_rejected(client, backend, body):
    assert client.post("/sync", json=body).status_code == 422
    assert backend.requests == []


def test_reconcile_runs_in_the_background_and_reports_what_it_repaired(client, backend):
    response = client.post("/reconcile")

    assert response.status_code == 202
    assert response.json()["kind"] == "reconcile"

    run = _finish(client, response.json()["id"])
    assert run["status"] == "succeeded"
    outcome = run["result"]["entities"][0]["reconcile"]
    assert (outcome["mode"], outcome["rows_compared"], outcome["rows_updated"]) == ("full", 40, 3)
    assert outcome["rows_deleted_upstream"] is None
    request = backend.reconcile_requests[0]
    assert (request.entities, request.start, request.end, request.full) == (None, None, None, None)
    assert backend.requests == []


def test_reconcile_passes_its_options(client, backend):
    body = {"entities": ["scores"], "from": "2026-03-01T00:00:00Z", "full": True}

    _finish(client, client.post("/reconcile", json=body).json()["id"])

    request = backend.reconcile_requests[0]
    assert request.entities == ["scores"]
    assert request.start == datetime(2026, 3, 1, tzinfo=UTC)
    assert request.full is True


@pytest.mark.parametrize(
    "body",
    [
        {"entities": ["prompts"]},
        {"from": "2026-03-02T00:00:00Z", "to": "2026-03-01T00:00:00Z"},
        {"sample_rate": 0.5},  # belongs to /sync
        {"filters": ["environment=production"]},
    ],
)
def test_invalid_reconcile_requests_are_rejected(client, backend, body):
    assert client.post("/reconcile", json=body).status_code == 422
    assert backend.reconcile_requests == []


def test_only_one_run_is_active_at_a_time(client, backend):
    backend.release.clear()
    first = client.post("/sync").json()
    assert backend.started.wait(5)

    running = client.get(f"/runs/{first['id']}").json()
    assert running["status"] == "running"
    # Progress is visible while the run is still going.
    assert running["result"]["entities"][0]["rows_fetched"] == 1

    for path in ("/sync", "/reconcile"):
        conflict = client.post(path)
        assert conflict.status_code == 409
        assert first["id"] in conflict.json()["detail"]
    assert backend.reconcile_requests == []

    backend.release.set()
    assert _finish(client, first["id"])["status"] == "succeeded"
    assert client.post("/reconcile").status_code == 202


def test_a_failed_sync_is_reported_and_frees_the_slot(client, backend):
    backend.error = RuntimeError("warehouse is suspended")

    run = _finish(client, client.post("/sync").json()["id"])

    assert run["status"] == "failed"
    assert run["error"] == "RuntimeError: warehouse is suspended"
    # The last progress snapshot survives the failure.
    assert run["result"]["entities"][0]["rows_fetched"] == 1

    backend.error = None
    assert client.post("/sync").status_code == 202


def test_runs_are_listed_newest_first(client):
    first = client.post("/sync").json()["id"]
    _finish(client, first)
    second = client.post("/reconcile").json()["id"]
    _finish(client, second)

    runs = client.get("/runs").json()
    assert [(run["id"], run["kind"]) for run in runs] == [(second, "reconcile"), (first, "sync")]


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_state(client):
    assert client.get("/state").json() == [
        {
            "entity": "scores",
            "watermark": "2026-03-10T12:00:00Z",
            "api_version": "v4",
            "updated_at": "2026-03-10T12:00:00Z",
            "reconciled_at": "2026-03-10T12:00:00Z",
        }
    ]


def test_entities_describe_tables_views_and_the_configured_selection(settings, backend):
    sync = SyncSettings(_env_file=None, sample_rate=0.2, filters="environment=production")
    configured = settings.model_copy(update={"sync": sync})

    with TestClient(create_app(configured, backend)) as client:
        payload = client.get("/entities").json()

    assert payload["api_version"] == "v4"
    assert (payload["sample_rate"], payload["filters"]) == (0.2, ["environment=production"])
    by_name = {entity["name"]: entity for entity in payload["entities"]}
    assert by_name["observations"] == {
        "name": "observations",
        "kind": "table",
        "object_name": "LANGFUSE_OBSERVATIONS",
        "endpoint": "/api/public/v2/observations",
        "derived_from": None,
        "snapshot": False,
    }
    assert by_name["traces"]["kind"] == "view"
    assert by_name["traces"]["derived_from"] == "observations"
    # Langfuse cannot filter these by time, so every sync reads them in full.
    assert {name for name, entity in by_name.items() if entity["snapshot"]} == {
        "comments",
        "annotation_queues",
        "annotation_queue_items",
    }


def test_api_key_is_enforced_when_configured(settings, backend):
    secured = settings.model_copy(update={"api": ApiSettings(_env_file=None, key="s3cret")})

    with TestClient(create_app(secured, backend)) as client:
        assert client.get("/health").status_code == 200
        for method, path in [
            ("GET", "/entities"),
            ("POST", "/sync"),
            ("POST", "/reconcile"),
            ("GET", "/runs"),
            ("GET", "/runs/any"),
            ("GET", "/state"),
        ]:
            assert client.request(method, path).status_code == 401
            assert client.request(method, path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert backend.requests == [] and backend.reconcile_requests == []

        accepted = client.post("/sync", headers={"X-API-Key": "s3cret"})
        assert accepted.status_code == 202
        client.app.state.runs.wait(accepted.json()["id"], timeout=5)
