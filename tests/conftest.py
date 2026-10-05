import os

import pytest

from langfuse_to_snowflake.config import (
    ApiSettings,
    LangfuseSettings,
    Settings,
    SnowflakeSettings,
    SyncSettings,
)


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
