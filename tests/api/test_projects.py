"""The API with several projects: each has its own settings, runs and schedule."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.config import ConfigFile, ConfigStore, Deployment
from langfuse_to_snowflake.snowflake import EntityState
from langfuse_to_snowflake.sync import EntitySchema, FieldSchema, SyncResult

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


class Backend:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.reconciled: list[str] = []
        self.discovered: list[tuple] = []
        self.error: Exception | None = None

    def run_sync(self, request, progress):
        self.synced.append(request.project)
        return SyncResult(project_id=f"id-{request.project}", api_version="v4", started_at=NOW)

    def run_reconcile(self, request, progress):
        self.reconciled.append(request.project)
        return SyncResult(project_id=f"id-{request.project}", api_version="v4", started_at=NOW)

    def read_state(self, project):
        return [EntityState(f"scores-of-{project}", NOW, "v4", NOW)]

    def discover(self, project, entities, sample):
        self.discovered.append((project, entities, sample))
        if self.error:
            raise self.error
        field = FieldSchema(path="metadata.tier", types=["text"], share=0.5)
        return [
            EntitySchema(
                entity="observations", table="LANGFUSE_OBSERVATIONS", sampled=2, fields=[field]
            )
        ]


@pytest.fixture
def deployment(settings, tmp_path) -> Deployment:
    file = ConfigFile(tmp_path / "config.json")

    def project(name: str, host: str) -> ConfigStore:
        langfuse = settings.langfuse.model_copy(update={"name": name, "host": host})
        return ConfigStore(settings.model_copy(update={"langfuse": langfuse}), file=file)

    stores = {
        "support": project("support", "https://cloud.langfuse.com"),
        "search": project("search", "https://us.cloud.langfuse.com"),
    }
    return Deployment(stores, settings.api)


@pytest.fixture
def backend() -> Backend:
    return Backend()


@pytest.fixture
def client(deployment, backend):
    with TestClient(create_app(deployment, backend)) as test_client:
        yield test_client


def _finish(client: TestClient, run: dict) -> dict:
    client.app.state.runs.wait(run["id"], timeout=5)
    return client.get(f"/runs/{run['id']}").json()


def test_projects_are_listed_with_where_they_read_from(client):
    client.put("/config?project=search", json={"sample_rate": 0.5, "schedule_minutes": 30})

    assert client.get("/projects").json() == [
        {
            "name": "support",
            "langfuse_host": "https://cloud.langfuse.com",
            "api_version": "v4",
            "changed_settings": 0,
            "schedule_minutes": 0,
        },
        {
            "name": "search",
            "langfuse_host": "https://us.cloud.langfuse.com",
            "api_version": "v4",
            "changed_settings": 2,
            "schedule_minutes": 30,
        },
    ]


@pytest.mark.parametrize("path", ["/config", "/entities", "/state", "/schedule", "/schema"])
def test_a_project_must_be_named_when_there_are_several(client, path):
    missing = client.get(path)
    unknown = client.get(f"{path}?project=billing")

    assert missing.status_code == 400
    assert "choose one of support, search" in missing.json()["detail"]
    assert unknown.status_code == 404
    assert "No project named 'billing'" in unknown.json()["detail"]


def test_each_project_has_its_own_settings(client):
    saved = client.put(
        "/config?project=support",
        json={
            "filters": ["environment=production"],
            "exclude_fields": ["observations:input", "metadata.email"],
        },
    )

    assert saved.status_code == 200
    support = saved.json()
    assert support["project"] == "support"
    assert support["overridden"] == ["filters", "exclude_fields"]
    # Excluded fields come back taken apart too, for the field editor.
    assert support["exclude_fields"] == [
        {"text": "observations:input", "entity": "observations", "path": "input"},
        {"text": "metadata.email", "entity": None, "path": "metadata.email"},
    ]
    assert support["deployment"]["langfuse_host"] == "https://cloud.langfuse.com"

    search = client.get("/config?project=search").json()
    assert (search["project"], search["overridden"]) == ("search", [])
    assert search["deployment"]["langfuse_host"] == "https://us.cloud.langfuse.com"

    client.delete("/config?project=support")
    assert client.get("/config?project=support").json()["overridden"] == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (["observations:id"], "id is needed to load observations"),
        (["prompts:name"], "unknown entity"),
        (["two words"], "cannot parse field"),
    ],
)
def test_fields_that_cannot_be_left_out_are_refused(client, fields, message):
    response = client.put("/config?project=support", json={"exclude_fields": fields})

    assert response.status_code == 422
    (problem,) = response.json()["detail"]
    assert problem["field"] == "exclude_fields" and message in problem["message"]


def test_a_view_cannot_have_fields_left_out_on_v4(client):
    response = client.put("/config?project=support", json={"exclude_fields": ["traces:userId"]})

    assert response.status_code == 422
    assert "Use observations instead" in response.json()["detail"][0]["message"]


def test_state_and_entities_are_per_project(client):
    assert client.get("/state?project=search").json()[0]["entity"] == "scores-of-search"
    client.put("/config?project=search", json={"entities": ["scores"]})

    assert [e["name"] for e in client.get("/entities?project=search").json()["entities"]] == [
        "scores"
    ]
    assert len(client.get("/entities?project=support").json()["entities"]) == 7


def test_a_run_is_for_one_project(client, backend):
    assert client.post("/sync").status_code == 400
    assert client.post("/sync", json={"project": "billing"}).status_code == 404

    first = _finish(client, client.post("/sync", json={"project": "search"}).json())
    second = _finish(client, client.post("/reconcile", json={"project": "support"}).json())

    assert (first["project"], first["kind"], first["status"]) == ("search", "sync", "succeeded")
    assert (second["project"], second["kind"]) == ("support", "reconcile")
    assert (backend.synced, backend.reconciled) == (["search"], ["support"])
    # All runs together, or one project's.
    assert [run["project"] for run in client.get("/runs").json()] == ["support", "search"]
    assert [run["id"] for run in client.get("/runs?project=search").json()] == [first["id"]]


def test_each_project_follows_its_own_schedule(client, backend):
    client.put("/config?project=search", json={"schedule_minutes": 15})
    schedulers = client.app.state.services.schedulers

    assert set(schedulers) == {"support", "search"}
    assert client.get("/schedule?project=search").json()["every_minutes"] == 15
    assert client.get("/schedule?project=support").json()["enabled"] is False

    assert not schedulers["support"].tick()
    assert schedulers["search"].tick()
    (run,) = client.get("/runs").json()
    client.app.state.runs.wait(run["id"], timeout=5)
    assert (run["project"], run["trigger"]) == ("search", "schedule")
    assert backend.synced == ["search"]


def test_schema_shows_the_fields_a_projects_data_has(client, backend):
    response = client.get("/schema?project=search&entity=observations&entity=scores&sample=50")

    assert response.status_code == 200
    assert response.json() == [
        {
            "entity": "observations",
            "table": "LANGFUSE_OBSERVATIONS",
            "sampled": 2,
            "fields": [
                {
                    "path": "metadata.tier",
                    "types": ["text"],
                    "share": 0.5,
                    "excluded": False,
                    "required": False,
                    "column": None,
                }
            ],
            "unavailable": None,
        }
    ]
    assert backend.discovered == [("search", ["observations", "scores"], 50)]


def test_schema_defaults_and_limits(client, backend):
    client.get("/schema?project=support")

    assert backend.discovered == [("support", None, 200)]
    assert client.get("/schema?project=support&sample=0").status_code == 422
    assert client.get("/schema?project=support&sample=5000").status_code == 422
    assert client.get("/schema?project=support&entity=prompts").status_code == 422


def test_schema_says_why_langfuse_could_not_be_read(client, backend):
    backend.error = RuntimeError("GET /api/public/v2/observations failed: HTTP 401")

    response = client.get("/schema?project=support")

    assert response.status_code == 503
    assert "HTTP 401" in response.json()["detail"]


def test_with_one_project_it_need_not_be_named(settings, backend):
    with TestClient(create_app(settings, backend)) as client:
        assert [project["name"] for project in client.get("/projects").json()] == ["default"]
        assert client.get("/config").json()["project"] == "default"
        assert client.get("/schema").status_code == 200
        run = _finish(client, client.post("/sync").json())
        assert run["project"] == "default"
        assert backend.synced == ["default"]
