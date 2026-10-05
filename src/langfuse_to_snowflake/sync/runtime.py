"""What a running service is made of, put together from the environment.

Where the service keeps what it has to remember follows SYNC_STORE. With
``snowflake`` the settings changed in the web app and the history of runs
live in tables next to the data, and the service itself holds nothing: a
container can be replaced at any time. With ``file`` the settings are in
SYNC_CONFIG_FILE and runs are only remembered until a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import ConfigFile, Deployment, Settings
from ..snowflake import Sessions

if TYPE_CHECKING:
    from ..snowflake import RunsTable


@dataclass(frozen=True)
class Runtime:
    deployment: Deployment
    # Shared by everything that reaches Snowflake, so that one run logs in once.
    sessions: Sessions
    # None where runs are not kept beyond the life of the process.
    runs: RunsTable | None = None


def open_sessions(settings: Settings) -> Sessions:
    return Sessions(settings.snowflake)


def open_runtime(env_file: str | Path | None = ".env") -> Runtime:
    """Every project the environment declares, with the settings saved for each.

    Raises SettingsUnavailable when the saved settings cannot be read. A
    service that does not know what was left out or filtered must not sync.
    """
    projects = Settings.load_all(env_file)
    first = next(iter(projects.values()))
    sessions = open_sessions(first)
    if first.sync.store == "file":
        return Runtime(Deployment.load(projects, ConfigFile(first.sync.config_file)), sessions)

    # Imported here: Snowpark is slow to load, and only needed from this point on.
    from ..snowflake import RunsTable, SettingsTable

    prefix = first.sync.table_prefix
    schema = f"{first.snowflake.database}.{first.snowflake.schema_name}"
    storage = SettingsTable(sessions, table_prefix=prefix, schema=schema)
    runs = RunsTable(sessions, table_prefix=prefix, schema=schema)
    return Runtime(Deployment.load(projects, storage), sessions, runs)
