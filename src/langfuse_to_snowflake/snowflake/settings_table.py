"""Where the settings changed in the web app are kept: a table in Snowflake.

Every change adds a row, so the table is also the history of who had what
configured when. The newest row of a project is the one in effect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from snowflake.snowpark import functions as F

from ..entities import settings_table_name, validate_prefix
from . import frames
from .layout import SETTINGS_COLUMNS
from .session import Sessions


@dataclass(frozen=True)
class SettingsChange:
    project: str
    changed_at: datetime
    settings: dict[str, Any]


class SettingsTable:
    def __init__(
        self,
        sessions: Sessions,
        *,
        table_prefix: str = "LANGFUSE_",
        schema: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self.name = settings_table_name(validate_prefix(table_prefix))
        self._schema = schema
        self._clock = clock
        self._ready = False

    @property
    def location(self) -> str:
        """Where the settings are, in words for the people who look for them."""
        qualified = f"{self._schema}.{self.name}" if self._schema else self.name
        return f"Snowflake table {qualified}"

    def read(self) -> dict[str, dict[str, Any]]:
        """The settings in effect, by project. Empty if nothing was saved yet."""
        latest: dict[str, SettingsChange] = {}
        for change in self._changes():
            held = latest.get(change.project)
            if held is None or change.changed_at >= held.changed_at:
                latest[change.project] = change
        return {project: change.settings for project, change in latest.items()}

    def write(self, project: str, overrides: Mapping[str, Any]) -> None:
        """Save a project's settings as the newest change. Earlier ones stay as history."""
        row = (project, self._clock(), dict(overrides))

        def append() -> None:
            with self._sessions.use() as session:
                self._ensure(session)
                frame = session.create_dataframe([row], schema=frames.struct(SETTINGS_COLUMNS))
                frame.write.save_as_table(self.name, mode="append")

        # Not repeated on failure: a write that did arrive would be saved twice.
        append()

    def history(self, project: str, limit: int = 50) -> list[SettingsChange]:
        """A project's changes, newest first."""
        changes = [change for change in self._changes() if change.project == project]
        changes.sort(key=lambda change: change.changed_at, reverse=True)
        return changes[:limit]

    def _changes(self) -> list[SettingsChange]:
        def collect() -> list[Any]:
            with self._sessions.use() as session:
                self._ensure(session)
                table = session.table(self.name)
                return table.select(
                    F.col("PROJECT"), F.col("CHANGED_AT"), F.col("SETTINGS")
                ).collect()

        changes = []
        for project, changed_at, settings in self._sessions.retrying(collect):
            at = frames.as_utc(changed_at)
            assert at is not None
            changes.append(SettingsChange(project, at, _parsed(settings)))
        return changes

    def _ensure(self, session: Any) -> None:
        if not self._ready:
            frames.empty(session, SETTINGS_COLUMNS).write.save_as_table(self.name, mode="ignore")
            self._ready = True


def _parsed(value: Any) -> dict[str, Any]:
    """A VARIANT comes back as JSON text."""
    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, Mapping) else {}
