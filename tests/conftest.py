import os

import pytest

from langfuse_to_snowflake.config import (
    ApiSettings,
    LangfuseSettings,
    Settings,
    SnowflakeSettings,
    SyncSettings,
)

# On a CI runner (GITHUB_ACTIONS, FORCE_COLOR) Typer styles its help and error
# output for a terminal, which puts escape codes between the words the tests
# look for. Typer reads this once, when it is first imported, so it is set
# here rather than in a fixture.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    """Keep the developer's own environment and .env out of the tests."""
    for name in list(os.environ):
        if name.startswith(("LANGFUSE_", "SNOWFLAKE_", "SYNC_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def langfuse_settings() -> LangfuseSettings:
    return LangfuseSettings(
        _env_file=None,
        host="https://langfuse.test",
        public_key="pk-test",
        secret_key="sk-test",
        max_retries=3,
    )


@pytest.fixture
def snowflake_settings() -> SnowflakeSettings:
    return SnowflakeSettings(
        _env_file=None,
        account="acct",
        user="loader",
        private_key="unused",
        warehouse="WH",
        database="DB",
        schema_name="LANGFUSE",
    )


@pytest.fixture
def settings(langfuse_settings, snowflake_settings) -> Settings:
    return Settings(
        langfuse=langfuse_settings,
        snowflake=snowflake_settings,
        sync=SyncSettings(_env_file=None),
        api=ApiSettings(_env_file=None),
    )


@pytest.fixture
def local_session():
    """A Snowpark session on the local emulator: in-process, empty, and gone afterwards."""
    from snowflake.snowpark import Session

    session = Session.builder.config("local_testing", True).create()
    yield session
    session.close()


@pytest.fixture
def local_catalog(local_session):
    from tests.fakes import LocalCatalog

    return LocalCatalog(local_session)


@pytest.fixture
def local_sessions(local_session, local_catalog, snowflake_settings):
    """What the adapter and the service's own tables open sessions with, on the emulator."""
    from langfuse_to_snowflake.snowflake import Sessions

    return Sessions(
        snowflake_settings,
        session=local_session,
        catalog=lambda _: local_catalog,
        sleep=lambda _: None,
    )
