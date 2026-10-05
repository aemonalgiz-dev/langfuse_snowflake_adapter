"""Tests against a real Langfuse project. Read-only, and never run by default.

Run them with ``pytest -m live``. The credentials come from the environment
(as on a CI runner) or from the project's ``.env``; without any, the tests are
skipped rather than failed.
"""

import os
from pathlib import Path

import pytest

from langfuse_to_snowflake.config import ConfigError, LangfuseSettings, load_projects

ROOT = Path(__file__).resolve().parents[2]

# Taken before the suite's own fixture clears the environment for each test.
_ENVIRONMENT = {name: value for name, value in os.environ.items() if name.startswith("LANGFUSE_")}


@pytest.fixture
def live_langfuse(monkeypatch) -> LangfuseSettings:
    """The first configured Langfuse project, or a skip when there is none."""
    for name, value in _ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    env_file = ROOT / ".env"
    try:
        projects = load_projects(env_file if env_file.exists() else None)
    except ConfigError as exc:
        pytest.skip(f"no Langfuse credentials to test with: {exc}")
    return next(iter(projects.values()))
