"""The service with its settings and run history kept in Snowflake (the emulator here)."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from snowflake.connector import errors

from langfuse_to_snowflake.api import backend as backend_module
from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.api.backend import LiveBackend
from langfuse_to_snowflake.config import Deployment
from langfuse_to_snowflake.snowflake import EntityState, RunsTable, SettingsTable
from langfuse_to_snowflake.sync import EntityResult, Runtime, SyncResult, SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


def _result(rows: int) -> SyncResult:
    entity = EntityResult(
        entity="scores",
        table="LANGFUSE_SCORES",
        window_start=NOW,
        window_end=NOW,
        rows_fetched=rows,
        rows_inserted=rows,
    )
    return SyncResult(project_id="proj-1", api_version="v4", started_at=NOW, entities=[entity])


class FakeBackend:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.requests = []

    def run_sync(self, request, progress):
        self.requests.append(request)
        if self.error:
            raise self.error
        return _result(2)

    def run_reconcile(self, request, progress):
        return _result(0)

    def read_state(self, project):
        return [EntityState("scores", NOW, "v4", NOW)]


class Counting:
    """Wraps sessions to count how often one is opened from the outside."""

    def __init__(self, sessions) -> None:
        self._sessions = sessions
        self.depth = 0
        self.logins = 0

    @contextmanager
    def use(self):
        if self.depth == 0:
            self.logins += 1
        self.depth += 1
        try:
            with self._sessions.use() as session:
                yield session
        finally:
            self.depth -= 1

    def __getattr__(self, name):
        return getattr(self._sessions, name)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def sessions(local_sessions) -> Counting:
    return Counting(local_sessions)


def _runtime(settings, sessions) -> Runtime:
    """What ``open_runtime`` gives with SYNC_STORE=snowflake, as after a fresh start."""
    storage = SettingsTable(sessions, schema="DB.LANGFUSE")
    deployment = Deployment.load({"default": settings}, storage)
    return Runtime(deployment, sessions, RunsTable(sessions))


@contextmanager
def _service(settings, sessions, backend):
    app = create_app(_runtime(settings, sessions), backend)
    with TestClient(app) as client:
        client.runs = app.state.runs
        yield client


def _sync(client, **body) -> dict:
    run = client.post("/sync", json=body).json()
    client.runs.wait(run["id"], timeout=10)
    return client.get(f"/runs/{run['id']}").json()


def test_a_run_is_written_to_snowflake_as_it_starts_and_ends(
    settings, sessions, backend, local_session
):
    with _service(settings, sessions, backend) as client:
        run = _sync(client, entities=["scores"])

    assert run["status"] == "succeeded"
    (row,) = local_session.table("LANGFUSE_SYNC_RUNS").collect()
    assert (row["ID"], row["PROJECT"], row["KIND"], row["TRIGGERED_BY"]) == (
        run["id"],
        "default",
        "sync",
        "manual",
    )
    assert row["STATUS"] == "succeeded"
    assert row["STARTED_AT"] is not None and row["FINISHED_AT"] is not None
    assert json.loads(row["REQUEST"])["entities"] == ["scores"]
    assert json.loads(row["RESULT"])["entities"][0]["rows_inserted"] == 2


def test_a_failed_run_is_recorded_with_its_error(settings, sessions, backend, local_session):
    backend.error = RuntimeError("Langfuse said no")

    with _service(settings, sessions, backend) as client:
        run = _sync(client)

    assert run["status"] == "failed"
    (row,) = local_session.table("LANGFUSE_SYNC_RUNS").collect()
    assert (row["STATUS"], row["ERROR"]) == ("failed", "RuntimeError: Langfuse said no")


def test_the_history_of_runs_survives_a_restart(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        first = _sync(client)
        backend.error = RuntimeError("down")
        second = _sync(client)

    # A new container: nothing in memory.
    with _service(settings, sessions, backend) as client:
        runs = client.get("/runs").json()
        again = client.get(f"/runs/{first['id']}").json()

    assert [(run["id"], run["status"]) for run in runs] == [
        (second["id"], "failed"),
        (first["id"], "succeeded"),
    ]
    assert again["result"]["entities"][0]["rows_inserted"] == 2
    assert again["request"] == first["request"]


def test_new_runs_are_listed_before_the_ones_from_before_the_restart(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        earlier = _sync(client)

    with _service(settings, sessions, backend) as client:
        later = _sync(client)
        listed = [run["id"] for run in client.get("/runs").json()]

    assert listed == [later["id"], earlier["id"]]


def test_a_run_the_last_container_never_finished_is_shown_as_interrupted(
    settings, sessions, backend
):
    RunsTable(sessions).started(
        {
            "id": "abandoned",
            "project": "default",
            "kind": "sync",
            "triggered_by": "schedule",
            "status": "running",
            "created_at": NOW - timedelta(hours=1),
            "started_at": NOW - timedelta(hours=1),
            "request": {},
        }
    )

    with _service(settings, sessions, backend) as client:
        (run,) = client.get("/runs").json()

    assert (run["id"], run["status"], run["trigger"]) == ("abandoned", "interrupted", "schedule")
    assert run["finished_at"] is None


def test_a_run_and_its_record_share_one_login(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        before = sessions.logins
        _sync(client)

        # Recording the start, the run itself and recording the end.
        assert sessions.logins == before + 1


def test_history_that_cannot_be_read_does_not_stop_the_service(
    settings, sessions, backend, monkeypatch
):
    with _service(settings, sessions, backend) as client:
        monkeypatch.setattr(
            RunsTable,
            "recent",
            lambda self, limit=50: (_ for _ in ()).throw(errors.OperationalError(msg="down")),
        )

        assert client.get("/runs").json() == []
        assert client.get("/health").json()["status"] == "ok"


def test_a_run_that_cannot_be_recorded_still_counts(settings, sessions, backend, monkeypatch):
    def broken(self, run):
        raise errors.OperationalError(msg="down")

    monkeypatch.setattr(RunsTable, "started", broken)
    monkeypatch.setattr(RunsTable, "finished", broken)

    with _service(settings, sessions, backend) as client:
        run = _sync(client)

    assert run["status"] == "succeeded"
    assert len(backend.requests) == 1


def test_settings_say_where_they_are_kept(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        config = client.get("/config").json()

    assert config["persisted"] is True
    assert config["stored_in"] == "Snowflake table DB.LANGFUSE.LANGFUSE_SYNC_SETTINGS"
    assert config["has_history"] is True


def test_changed_settings_survive_a_restart_and_keep_their_history(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        client.put("/config", json={"sample_rate": 0.5})
        client.put("/config", json={"exclude_fields": ["observations:input"]})

    with _service(settings, sessions, backend) as client:
        config = client.get("/config").json()
        history = client.get("/config/history").json()
        newest = client.get("/config/history", params={"limit": 1}).json()

    assert config["values"]["sample_rate"] == 0.5
    assert config["values"]["exclude_fields"] == ["observations:input"]
    assert config["overridden"] == ["sample_rate", "exclude_fields"]
    assert [change["settings"] for change in history] == [
        {"sample_rate": 0.5, "exclude_fields": ["observations:input"]},
        {"sample_rate": 0.5},
    ]
    assert history[0]["changed_at"] > history[1]["changed_at"]
    assert newest == history[:1]


def test_resetting_is_recorded_too(settings, sessions, backend):
    with _service(settings, sessions, backend) as client:
        client.put("/config", json={"sample_rate": 0.5})
        client.delete("/config")
        history = client.get("/config/history").json()

    assert [change["settings"] for change in history] == [{}, {"sample_rate": 0.5}]


def test_a_change_that_cannot_be_saved_is_refused_and_nothing_changes(
    settings, sessions, backend, monkeypatch
):
    with _service(settings, sessions, backend) as client:
        monkeypatch.setattr(
            SettingsTable,
            "write",
            lambda self, project, overrides: (_ for _ in ()).throw(
                errors.OperationalError(msg="warehouse suspended")
            ),
        )

        response = client.put("/config", json={"sample_rate": 0.5})
        reset = client.delete("/config")

        assert response.status_code == 503
        assert "could not be saved" in response.json()["detail"]
        assert "Nothing was changed" in response.json()["detail"]
        assert reset.status_code == 503
        assert client.get("/config").json()["values"]["sample_rate"] == 1.0


def test_without_snowflake_storage_there_is_no_history_to_show(settings, backend):
    with TestClient(create_app(settings, backend)) as client:
        config = client.get("/config").json()

        assert (config["persisted"], config["stored_in"], config["has_history"]) == (
            False,
            None,
            False,
        )
        assert client.get("/config/history").json() == []


def _live(settings, sessions, monkeypatch, opened: list):
    """The real backend, with the sync itself replaced by fakes."""

    @contextmanager
    def fake_open_service(current, shared=None):
        opened.append(current)
        source = FakeSource({"/api/public/v3/scores": [[{"id": "s1"}]]})
        yield SyncService(
            current.langfuse, current.sync, source, FakeWarehouse(), clock=lambda: NOW
        )

    monkeypatch.setattr(backend_module, "open_service", fake_open_service)
    runtime = _runtime(settings, sessions)
    backend = LiveBackend(
        lambda project: runtime.deployment.store(project).current(),
        refresh=runtime.deployment.refresh,
        sessions=sessions,
    )
    return runtime, backend


def test_a_run_uses_what_was_saved_since_the_service_started(settings, sessions, monkeypatch):
    opened = []
    runtime, backend = _live(settings, sessions, monkeypatch, opened)

    with TestClient(create_app(runtime, backend)) as client:
        client.runs = client.app.state.runs
        # Saved by someone else: another container, or straight in the table.
        SettingsTable(sessions).write("default", {"exclude_fields": ["scores:comment"]})

        run = _sync(client, entities=["scores"])

    assert run["status"] == "succeeded"
    assert opened[0].sync.exclude_fields == ("scores:comment",)
    assert run["result"]["excluded_fields"] == ["scores:comment"]


def test_nothing_is_synced_when_the_saved_settings_cannot_be_read(settings, sessions, monkeypatch):
    opened = []
    runtime, backend = _live(settings, sessions, monkeypatch, opened)

    with TestClient(create_app(runtime, backend)) as client:
        client.runs = client.app.state.runs
        monkeypatch.setattr(
            SettingsTable,
            "read",
            lambda self: (_ for _ in ()).throw(errors.OperationalError(msg="no route")),
        )

        run = _sync(client)

    assert run["status"] == "failed"
    assert "SettingsUnavailable" in run["error"]
    assert "could not be read from Snowflake table" in run["error"]
    # The sync was never opened: better no data than data that should have been left out.
    assert opened == []
