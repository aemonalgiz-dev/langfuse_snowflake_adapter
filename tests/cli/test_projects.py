"""The commands with several projects configured."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from langfuse_to_snowflake.cli import app, support
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)
SCORES = "/api/public/v3/scores"
ENV = {
    "LANGFUSE_PROJECTS": "support,search",
    "LANGFUSE_SUPPORT_PUBLIC_KEY": "pk-support",
    "LANGFUSE_SUPPORT_SECRET_KEY": "sk-support",
    "LANGFUSE_SEARCH_PUBLIC_KEY": "pk-search",
    "LANGFUSE_SEARCH_SECRET_KEY": "sk-search",
    "SNOWFLAKE_ACCOUNT": "acct",
    "SNOWFLAKE_USER": "loader",
    "SNOWFLAKE_PRIVATE_KEY_PATH": "rsa_key.p8",
    "SNOWFLAKE_WAREHOUSE": "WH",
    "SNOWFLAKE_DATABASE": "DB",
    "SNOWFLAKE_SCHEMA": "LANGFUSE",
    "SYNC_WINDOW_HOURS": "720",
    "SYNC_ENTITIES": "scores",
}

runner = CliRunner()


class Project:
    def __init__(self, name: str) -> None:
        self.source = FakeSource({SCORES: [[{"id": f"{name}-1"}, {"id": f"{name}-2"}]]})
        self.source.get_project = lambda: {"id": f"id-{name}", "name": name}
        self.warehouse = FakeWarehouse()


@pytest.fixture
def projects(monkeypatch) -> dict[str, Project]:
    """Each project gets its own fake Langfuse and warehouse, found by its public key."""
    by_key = {"pk-support": Project("support"), "pk-search": Project("search")}
    opened = []

    @contextmanager
    def fake_open_service(settings):
        project = by_key[settings.langfuse.public_key]
        opened.append(settings.langfuse.name)
        yield SyncService(
            settings.langfuse, settings.sync, project.source, project.warehouse, clock=lambda: NOW
        )

    monkeypatch.setattr(support, "open_service", fake_open_service)
    return {"support": by_key["pk-support"], "search": by_key["pk-search"], "opened": opened}


def test_sync_covers_every_project_each_with_its_own_keys(projects):
    result = runner.invoke(app, ["sync"], env=ENV)

    assert result.exit_code == 0, result.output
    assert projects["opened"] == ["support", "search"]
    assert [row["id"] for row in projects["support"].warehouse.loaded["scores"]] == [
        "support-1",
        "support-2",
    ]
    assert [row["id"] for row in projects["search"].warehouse.loaded["scores"]] == [
        "search-1",
        "search-2",
    ]
    # The output says which project each block is about.
    lines = result.output.splitlines()
    assert lines.index("[support]") < lines.index("[search]")
    assert "Project support (id-support), Langfuse API v4" in result.output


def test_one_project_can_be_picked(projects):
    result = runner.invoke(app, ["--project", "search", "sync"], env=ENV)

    assert result.exit_code == 0, result.output
    assert projects["opened"] == ["search"]
    assert projects["support"].warehouse.loaded == {}


def test_an_unknown_project_is_explained():
    result = runner.invoke(app, ["-p", "billing", "sync"], env=ENV)

    assert result.exit_code == 2
    assert "No project named 'billing'; there is support, search" in result.output


def test_a_failing_project_does_not_stop_the_others(projects):
    def down():
        raise RuntimeError("keys revoked")

    projects["support"].source.get_project = down

    result = runner.invoke(app, ["sync"], env=ENV)

    assert result.exit_code == 1
    assert "RuntimeError: keys revoked" in result.output
    assert "Traceback" not in result.output
    # The second project was still synced.
    assert len(projects["search"].warehouse.loaded["scores"]) == 2


def test_json_output_is_a_list_when_there_are_several_projects(projects):
    result = runner.invoke(app, ["sync", "--json"], env=ENV)

    documents = json.loads(result.output)
    assert [document["project_id"] for document in documents] == ["id-support", "id-search"]

    single = runner.invoke(app, ["-p", "search", "sync", "--json"], env=ENV)
    # Still a list: the shape follows the deployment, not the selection.
    assert [doc["project_id"] for doc in json.loads(single.output)] == ["id-search"]


def test_projects_use_their_own_saved_settings(projects, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"version": 2, "projects": {"search": {"filters": ["scores:id=search-2"]}}})
    )

    result = runner.invoke(app, ["sync"], env={**ENV, "SYNC_CONFIG_FILE": str(config_file)})

    assert result.exit_code == 0, result.output
    assert [row["id"] for row in projects["search"].warehouse.loaded["scores"]] == ["search-2"]
    assert len(projects["support"].warehouse.loaded["scores"]) == 2


def test_check_status_and_reconcile_cover_every_project(projects):
    check = runner.invoke(app, ["check"], env=ENV)
    assert check.exit_code == 0, check.output
    assert check.output.count("Langfuse   ok") == 2

    status = runner.invoke(app, ["status"], env=ENV)
    assert status.output.count("Nothing has been synced yet.") == 2

    reconcile = runner.invoke(app, ["reconcile"], env=ENV)
    assert reconcile.exit_code == 0, reconcile.output
    assert reconcile.output.count("nothing to reconcile") == 2


def test_serve_runs_one_app_for_all_projects(monkeypatch):
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: served.append(app))

    result = runner.invoke(app, ["serve"], env=ENV)

    assert result.exit_code == 0, result.output
    services = served[0].state.services
    assert services.deployment.names == ["support", "search"]
    assert set(services.schedulers) == {"support", "search"}
