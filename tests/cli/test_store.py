"""The commands when settings are kept in Snowflake, which is the default."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from snowflake.connector import errors
from typer.testing import CliRunner

from langfuse_to_snowflake.cli import app, support
from langfuse_to_snowflake.snowflake import SettingsTable
from langfuse_to_snowflake.sync import SyncService
from langfuse_to_snowflake.sync import runtime as runtime_module
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)
SCORED = "2026-03-09T10:00:00+00:00"
ENV = {
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
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


class Unreachable:
    def __init__(self) -> None:
        self.attempts = 0

    def use(self):
        self.attempts += 1
        raise errors.OperationalError(msg="Could not connect to Snowflake backend")

    def retrying(self, function, *args):
        return function(*args)


@pytest.fixture
def opened(monkeypatch) -> list:
    """Run the sync itself against fakes, and note the settings and sessions it was opened with."""
    opened = []
    record = {"id": "s1", "name": "accuracy", "comment": "private", "timestamp": SCORED}

    @contextmanager
    def fake_open_service(settings, sessions=None):
        warehouse = FakeWarehouse()
        opened.append((settings, sessions, warehouse))
        source = FakeSource({"/api/public/v3/scores": [[dict(record)]]})
        yield SyncService(settings.langfuse, settings.sync, source, warehouse, clock=lambda: NOW)

    monkeypatch.setattr(support, "open_service", fake_open_service)
    return opened


def test_sync_applies_what_the_data_team_saved_in_snowflake(opened, monkeypatch, local_sessions):
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: local_sessions)
    SettingsTable(local_sessions).write("default", {"exclude_fields": ["scores:comment"]})

    result = runner.invoke(app, ["sync"], env=ENV)

    assert result.exit_code == 0, result.output
    assert "Fields left out: scores:comment" in result.output
    ((settings, sessions, warehouse),) = opened
    assert settings.sync.exclude_fields == ("scores:comment",)
    assert warehouse.loaded["scores"] == [{"id": "s1", "name": "accuracy", "timestamp": SCORED}]
    # The sync logs in with the same sessions the settings were read with.
    assert sessions is local_sessions


def test_nothing_is_synced_when_the_saved_settings_cannot_be_read(opened, monkeypatch):
    unreachable = Unreachable()
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: unreachable)

    result = runner.invoke(app, ["sync"], env=ENV)

    assert result.exit_code == 1
    assert "could not be read from Snowflake table DB.LANGFUSE.LANGFUSE_SYNC_SETTINGS" in (
        result.output
    )
    assert "Nothing was synced." in result.output
    assert opened == []


def test_serve_checks_for_the_access_key_before_reaching_snowflake(monkeypatch):
    unreachable = Unreachable()
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: unreachable)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("server started"))

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env=ENV)

    assert result.exit_code == 2
    assert "without an access key" in result.output
    assert unreachable.attempts == 0


def test_serve_does_not_start_without_its_saved_settings(monkeypatch):
    unreachable = Unreachable()
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: unreachable)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("server started"))

    result = runner.invoke(app, ["serve"], env={**ENV, "SYNC_API_KEY": "s3cret"})

    assert result.exit_code == 1
    assert "could not be read from Snowflake table" in result.output


def test_serve_keeps_settings_and_runs_in_snowflake(monkeypatch, local_sessions):
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: local_sessions)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: served.append(app))
    SettingsTable(local_sessions).write("default", {"schedule_minutes": 45})

    result = runner.invoke(app, ["serve"], env=ENV)

    assert result.exit_code == 0, result.output
    services = served[0].state.services
    store = services.deployment.store()
    assert store.current().sync.schedule_minutes == 45
    assert store.location == "Snowflake table DB.LANGFUSE.LANGFUSE_SYNC_SETTINGS"
    assert services.runs.kept_in == "Snowflake table DB.LANGFUSE.LANGFUSE_SYNC_RUNS"


def test_a_file_named_without_choosing_the_file_store_is_explained():
    result = runner.invoke(app, ["sync"], env={**ENV, "SYNC_CONFIG_FILE": "config.json"})

    assert result.exit_code == 2
    assert "SYNC_CONFIG_FILE is only used with SYNC_STORE=file" in result.output
