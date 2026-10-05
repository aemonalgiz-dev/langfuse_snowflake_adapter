from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from langfuse_to_snowflake.api import backend as backend_module
from langfuse_to_snowflake.api.backend import LiveBackend
from langfuse_to_snowflake.api.schemas import ReconcileRequest, SyncRequest
from langfuse_to_snowflake.config import ConfigStore
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)
SCORES = "/api/public/v3/scores"


@pytest.fixture
def wired(settings, monkeypatch):
    """A live backend whose service runs against fakes."""
    source, warehouse = FakeSource(), FakeWarehouse({"scores": NOW})

    @contextmanager
    def fake_open_service(opened_with):
        assert opened_with is settings
        yield SyncService(settings.langfuse, settings.sync, source, warehouse, clock=lambda: NOW)

    monkeypatch.setattr(backend_module, "open_service", fake_open_service)
    return LiveBackend(lambda project: settings), source, warehouse


def test_live_backend_asks_for_the_settings_on_every_run(settings, monkeypatch):
    opened_with = []

    @contextmanager
    def fake_open_service(current):
        opened_with.append(current.sync.sample_rate)
        yield SyncService(
            current.langfuse, current.sync, FakeSource(), FakeWarehouse(), clock=lambda: NOW
        )

    monkeypatch.setattr(backend_module, "open_service", fake_open_service)
    store = ConfigStore(settings)
    backend = LiveBackend(lambda project: store.current())

    backend.read_state("default")
    store.update({"sample_rate": 0.25})
    backend.read_state("default")

    # A change made while the service runs applies to the next run.
    assert opened_with == [1.0, 0.25]


def test_live_backend_runs_a_sync_with_the_request(wired):
    backend, source, warehouse = wired
    source.pages[SCORES] = [[{"id": "s1", "name": "a"}, {"id": "s2", "name": "b"}]]
    snapshots = []

    request = SyncRequest.model_validate(
        {
            "entities": ["scores"],
            "from": "2026-03-01T00:00:00Z",
            "to": "2026-03-02T00:00:00Z",
            "filters": ["scores:name=a"],
        }
    )
    result = backend.run_sync(request, snapshots.append)

    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1"]
    assert result.filters == ["scores:name=a"]
    assert (result.entities[0].rows_fetched, result.entities[0].rows_skipped) == (2, 1)
    assert snapshots  # progress was reported
    assert [entry.entity for entry in backend.read_state("default")] == ["scores"]


def test_live_backend_runs_a_reconcile_with_the_request(wired):
    backend, source, warehouse = wired
    stored = {"id": "s1", "timestamp": "2026-03-05T10:00:00Z", "value": 0.2}
    warehouse.seed(ENTITIES["scores"], [stored])
    source.pages[SCORES] = [[{**stored, "value": 0.9}]]
    snapshots = []

    request = ReconcileRequest.model_validate(
        {"entities": ["scores"], "from": "2026-03-05T00:00:00Z", "to": "2026-03-06T00:00:00Z"}
    )
    result = backend.run_reconcile(request, snapshots.append)

    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_updated) == ("full", 1, 1)
    assert warehouse.tables["scores"][("s1",)]["value"] == 0.9
    assert snapshots
