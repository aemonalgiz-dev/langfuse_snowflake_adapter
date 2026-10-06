"""Where the adapter's tests run: Snowpark's emulator, and a real account when asked.

Every test that takes ``warehouse`` runs on the emulator. With ``pytest -m
live`` the same tests run against the Snowflake account in the environment or
the project's ``.env``, in tables under a prefix of their own that are dropped
afterwards. Without an account they are skipped.

That second way has never been run: there has been no account to run it on.
It is kept for whoever deploys this against one.
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from langfuse_to_snowflake.config import SnowflakeSettings
from langfuse_to_snowflake.snowflake import Catalog, Sessions, SnowflakeAdapter

ROOT = Path(__file__).resolve().parents[2]

# Taken before the suite's own fixture clears the environment for each test.
# An empty value is what a CI runner gives for a secret that is not set.
_ENVIRONMENT = {
    name: value for name, value in os.environ.items() if name.startswith("SNOWFLAKE_") and value
}


@dataclass
class Warehouse:
    sessions: Sessions
    session: Any
    catalog: Any
    prefix: str
    live: bool

    def adapter(self, **options: Any) -> SnowflakeAdapter:
        options.setdefault("table_prefix", self.prefix)
        adapter = SnowflakeAdapter(sessions=self.sessions, **options)
        adapter.connect()
        return adapter

    def name(self, entity: str) -> str:
        return f"{self.prefix}{entity}".upper()

    def rows(self, entity: str, order: str = "ID") -> list[dict[str, Any]]:
        """An entity's table as dicts, with VARIANT columns parsed."""
        table = self.session.table(self.name(entity)).sort(order)
        variants = {
            field.name.strip('"')
            for field in table.schema.fields
            if type(field.datatype).__name__ == "VariantType"
        }
        return [
            {
                name: json.loads(value) if name in variants and value is not None else value
                for name, value in row.as_dict().items()
            }
            for row in table.collect()
        ]


def _live_settings(monkeypatch) -> SnowflakeSettings:
    for name, value in _ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    env_file = ROOT / ".env"
    try:
        return SnowflakeSettings(_env_file=env_file if env_file.exists() else None)
    except ValidationError as exc:
        missing = ", ".join(str(error["loc"][0]) for error in exc.errors())
        pytest.skip(f"no Snowflake account to test with (missing or invalid: {missing})")


def _drop_everything(session: Any, prefix: str) -> None:
    from snowflake.snowpark import functions as F

    found = (
        session.table("INFORMATION_SCHEMA.TABLES")
        .filter(
            (F.col("TABLE_SCHEMA") == F.current_schema()) & F.col("TABLE_NAME").startswith(prefix)
        )
        .select("TABLE_NAME", "TABLE_TYPE")
        .collect()
    )
    for row in found:
        kind = "VIEW" if row["TABLE_TYPE"] == "VIEW" else "TABLE"
        session.sql(f"DROP {kind} IF EXISTS {row['TABLE_NAME']}").collect()


@contextmanager
def _on_snowflake(monkeypatch) -> Iterator[Warehouse]:
    sessions = Sessions(_live_settings(monkeypatch))
    # Each test gets tables of its own, so runs never see each other's rows.
    prefix = f"L2S_TEST_{uuid4().hex[:10].upper()}_"
    with sessions.use() as session:
        try:
            yield Warehouse(sessions, session, Catalog(session), prefix, live=True)
        finally:
            _drop_everything(session, prefix)


@pytest.fixture(params=["emulator", pytest.param("snowflake", marks=pytest.mark.live)])
def warehouse(request, monkeypatch) -> Iterator[Warehouse]:
    if request.param == "emulator":
        session = request.getfixturevalue("local_session")
        catalog = request.getfixturevalue("local_catalog")
        sessions = request.getfixturevalue("local_sessions")
        yield Warehouse(sessions, session, catalog, "LANGFUSE_", live=False)
    else:
        with _on_snowflake(monkeypatch) as warehouse:
            yield warehouse


@pytest.fixture
def live_warehouse(monkeypatch) -> Iterator[Warehouse]:
    """For what only Snowflake itself can run. Mark the test ``live``."""
    with _on_snowflake(monkeypatch) as warehouse:
        yield warehouse
