"""Putting a running service together from the environment."""

import json

import pytest
from snowflake.connector import errors

from langfuse_to_snowflake.config import SettingsUnavailable
from langfuse_to_snowflake.snowflake import Sessions
from langfuse_to_snowflake.sync import open_runtime
from langfuse_to_snowflake.sync import runtime as runtime_module

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
    "SNOWFLAKE_DATABASE": "ANALYTICS",
    "SNOWFLAKE_SCHEMA": "LANGFUSE",
}


class Unreachable:
    """Sessions on an account that cannot be reached."""

    def __init__(self) -> None:
        self.attempts = 0

    def use(self):
        self.attempts += 1
        raise errors.OperationalError(msg="Could not connect to Snowflake backend")

    def retrying(self, function, *args):
        return function(*args)


@pytest.fixture
def environment(monkeypatch):
    def set_environment(**extra: str) -> None:
        for name, value in {**ENV, **extra}.items():
            monkeypatch.setenv(name, value)

    return set_environment


@pytest.fixture
def on_emulator(monkeypatch, local_sessions):
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: local_sessions)
    return local_sessions


def test_by_default_settings_and_runs_are_kept_in_snowflake(environment, on_emulator):
    environment()

    runtime = open_runtime(env_file=None)

    assert runtime.deployment.names == ["support", "search"]
    assert runtime.sessions is on_emulator
    assert runtime.runs is not None and runtime.runs.name == "LANGFUSE_SYNC_RUNS"
    store = runtime.deployment.store("support")
    assert store.location == "Snowflake table ANALYTICS.LANGFUSE.LANGFUSE_SYNC_SETTINGS"
    assert store.keeps_history


def test_what_one_service_saves_the_next_one_starts_with(environment, on_emulator, local_session):
    environment(SYNC_TABLE_PREFIX="lf_")
    first = open_runtime(env_file=None)
    first.deployment.store("support").update(
        {"exclude_fields": ["observations:input"], "schedule_minutes": 15}
    )

    # A new container: nothing carried over but the environment.
    second = open_runtime(env_file=None)

    support = second.deployment.store("support").current()
    assert support.sync.exclude_fields == ("observations:input",)
    assert support.sync.schedule_minutes == 15
    assert second.deployment.store("search").overridden() == []
    (row,) = local_session.table("LF_SYNC_SETTINGS").collect()
    assert row["PROJECT"] == "support"
    assert json.loads(row["SETTINGS"]) == {
        "exclude_fields": ["observations:input"],
        "schedule_minutes": 15,
    }


def test_a_service_that_cannot_read_its_settings_does_not_start(environment, monkeypatch):
    environment()
    unreachable = Unreachable()
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: unreachable)

    with pytest.raises(SettingsUnavailable) as error:
        open_runtime(env_file=None)

    assert "Snowflake table ANALYTICS.LANGFUSE.LANGFUSE_SYNC_SETTINGS" in str(error.value)
    assert "Could not connect" in str(error.value)


def test_with_the_file_store_snowflake_is_not_touched_to_start(environment, monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "projects": {"search": {"sample_rate": 0.5}}}))
    environment(SYNC_STORE="file", SYNC_CONFIG_FILE=str(path))
    unreachable = Unreachable()
    monkeypatch.setattr(runtime_module, "open_sessions", lambda settings: unreachable)

    runtime = open_runtime(env_file=None)

    assert unreachable.attempts == 0
    assert runtime.runs is None
    store = runtime.deployment.store("search")
    assert (store.current().sync.sample_rate, store.location) == (0.5, str(path))
    assert not store.keeps_history


def test_sessions_are_opened_on_the_configured_account(environment):
    environment(SYNC_STORE="file")

    runtime = open_runtime(env_file=None)

    assert isinstance(runtime.sessions, Sessions)
    assert runtime.sessions._settings.account == "acct"
