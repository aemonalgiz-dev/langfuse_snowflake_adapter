import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.config import ApiSettings, ConfigStore
from langfuse_to_snowflake.sync import SyncResult

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


class RecordingBackend:
    """Remembers the sampling in force each time a sync starts."""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store
        self.sample_rates: list[float] = []

    def run_sync(self, request, progress):
        self.sample_rates.append(self._store.current().sync.sample_rate)
        return SyncResult(project_id="proj-1", api_version="v4", started_at=NOW)

    def run_reconcile(self, request, progress):
        return SyncResult(project_id="proj-1", api_version="v4", started_at=NOW)

    def read_state(self, project):
        raise RuntimeError("250001: could not connect to Snowflake")


@pytest.fixture
def store(settings, tmp_path) -> ConfigStore:
    return ConfigStore(settings, tmp_path / "config.json")


@pytest.fixture
def backend(store) -> RecordingBackend:
    return RecordingBackend(store)


@pytest.fixture
def client(store, backend):
    with TestClient(create_app(store, backend)) as test_client:
        yield test_client


def test_config_describes_values_defaults_choices_and_the_deployment(client):
    payload = client.get("/config").json()

    assert payload["values"] == payload["defaults"]
    assert payload["overridden"] == []
    assert payload["persisted"] is True
    assert set(payload["values"]) == {
        "entities",
        "sample_rate",
        "filters",
        "exclude_fields",
        "schedule_minutes",
        "lookback_minutes",
        "initial_backfill_days",
        "window_hours",
        "reconcile_every_hours",
        "reconcile_days",
        "reconcile_full",
        "check_deletions",
        "observation_fields",
        "expand_metadata",
    }
    assert payload["choices"]["entities"][-1] == "annotation_queue_items"
    assert "trace_context" in payload["choices"]["observation_fields"]
    assert payload["choices"]["operators"][:2] == ["=", "!="]
    assert {"environment", "userId", "metadata.", "input"} <= set(
        payload["choices"]["fields"]["observations"]
    )
    assert payload["deployment"] == {
        "langfuse_host": "https://langfuse.test",
        "api_version": "v4",
        "snowflake_account": "acct",
        "snowflake_user": "loader",
        "snowflake_role": None,
        "snowflake_warehouse": "WH",
        "snowflake_database": "DB",
        "snowflake_schema": "LANGFUSE",
        "table_prefix": "LANGFUSE_",
    }


def test_no_secret_is_ever_part_of_the_config(client):
    text = client.get("/config").text

    for secret in ("sk-test", "pk-test", "unused", "private_key", "secret_key"):
        assert secret not in text


def test_a_change_is_applied_reported_and_written_to_the_file(client, store):
    response = client.put(
        "/config",
        json={
            "sample_rate": 0.1,
            "filters": ["environment = production,staging", "observations:totalCost>=0.01"],
            "schedule_minutes": 30,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["values"]["sample_rate"] == 0.1
    assert payload["overridden"] == ["sample_rate", "filters", "schedule_minutes"]
    # Filters also come back taken apart, for the filter editor.
    assert payload["filters"] == [
        {
            "text": "environment=production,staging",
            "entity": None,
            "field": "environment",
            "op": "=",
            "value": "production,staging",
        },
        {
            "text": "observations:totalCost>=0.01",
            "entity": "observations",
            "field": "totalCost",
            "op": ">=",
            "value": "0.01",
        },
    ]
    saved = json.loads(store.path.read_text())
    assert saved["projects"]["default"]["schedule_minutes"] == 30
    assert client.get("/schedule").json()["every_minutes"] == 30
    assert client.get("/entities").json()["sample_rate"] == 0.1


def test_a_change_applies_to_the_next_run(client, backend):
    def sync() -> None:
        run = client.post("/sync").json()
        client.app.state.runs.wait(run["id"], timeout=5)

    sync()
    client.put("/config", json={"sample_rate": 0.25})
    sync()

    assert backend.sample_rates == [1.0, 0.25]


def test_null_puts_a_setting_back_to_its_default(client):
    client.put("/config", json={"sample_rate": 0.1, "schedule_minutes": 30})

    payload = client.put("/config", json={"sample_rate": None}).json()

    assert payload["values"]["sample_rate"] == 1.0
    assert payload["overridden"] == ["schedule_minutes"]


def test_delete_resets_everything(client, store):
    client.put("/config", json={"sample_rate": 0.1, "schedule_minutes": 30})

    payload = client.delete("/config").json()

    assert payload["overridden"] == []
    assert payload["values"] == payload["defaults"]
    assert json.loads(store.path.read_text())["projects"] == {"default": {}}


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"sample_rate": 0}, "sample_rate", "greater than 0"),
        ({"entities": []}, "entities", "at least one entity"),
        ({"filters": ["just some words"]}, "filters", "cannot parse filter"),
        ({"filters": ["traces:userId=alice"]}, "", "derived from observations"),
        ({"observation_fields": ["core"]}, "", "need the observation field group"),
    ],
)
def test_invalid_settings_are_reported_per_field_and_change_nothing(
    client, store, body, field, message
):
    response = client.put("/config", json=body)

    assert response.status_code == 422
    (problem,) = response.json()["detail"]
    assert problem["field"] == field and message in problem["message"]
    assert client.get("/config").json()["overridden"] == []
    assert not store.path.exists()


@pytest.mark.parametrize(
    "body",
    [{"api_version": "v3"}, {"table_prefix": "X_"}, {"secret_key": "x"}, {"sample_rate": "lots"}],
)
def test_deployment_settings_and_malformed_values_are_refused(client, body):
    assert client.put("/config", json=body).status_code == 422
    assert client.get("/config").json()["overridden"] == []


def test_without_a_config_file_the_page_is_told_changes_are_not_kept(settings, backend):
    with TestClient(create_app(settings, backend)) as client:
        assert client.get("/config").json()["persisted"] is False
        assert client.put("/config", json={"sample_rate": 0.5}).status_code == 200
        assert client.get("/config").json()["values"]["sample_rate"] == 0.5


def test_state_says_why_it_could_not_be_read(client):
    response = client.get("/state")

    assert response.status_code == 503
    assert response.json()["detail"] == "RuntimeError: 250001: could not connect to Snowflake"


def test_the_scheduler_starts_syncs_marked_as_scheduled(settings, backend):
    with TestClient(create_app(settings, backend)) as client:
        client.put("/config", json={"schedule_minutes": 60})
        scheduler = client.app.state.services.schedulers["default"]

        assert scheduler.tick()
        (run,) = client.get("/runs").json()
        client.app.state.runs.wait(run["id"], timeout=5)

        assert (run["kind"], run["trigger"]) == ("sync", "schedule")
        status = client.get("/schedule").json()
        assert status["enabled"] and status["last_run_at"] and status["next_run_at"]
        # Not due again until the interval has passed.
        assert not scheduler.tick()


def test_config_endpoints_need_the_api_key(settings, backend):
    secured = settings.model_copy(update={"api": ApiSettings(_env_file=None, key="s3cret")})

    with TestClient(create_app(secured, backend)) as client:
        for method, path in [
            ("GET", "/config"),
            ("PUT", "/config"),
            ("DELETE", "/config"),
            ("GET", "/schedule"),
        ]:
            assert client.request(method, path, json={}).status_code == 401
        allowed = client.put("/config", json={"sample_rate": 0.5}, headers={"X-API-Key": "s3cret"})
        assert allowed.status_code == 200
