"""The history of runs, kept in a Snowflake table so that it outlives the container."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from snowflake.snowpark import Column
from snowflake.snowpark import functions as F

from ..entities import runs_table_name, validate_prefix
from . import frames
from .layout import RUNS_COLUMNS
from .session import Sessions

# A run as it is stored: the column names in lower case.
Record = dict[str, Any]

_NAMES = [name for name, _ in RUNS_COLUMNS]
_KINDS = dict(RUNS_COLUMNS)


class RunsTable:
    def __init__(
        self, sessions: Sessions, *, table_prefix: str = "LANGFUSE_", schema: str | None = None
    ) -> None:
        self._sessions = sessions
        self.name = runs_table_name(validate_prefix(table_prefix))
        self._schema = schema
        self._ready = False

    @property
    def location(self) -> str:
        """Where the runs are, in words for the people who look for them."""
        qualified = f"{self._schema}.{self.name}" if self._schema else self.name
        return f"Snowflake table {qualified}"

    def started(self, run: Mapping[str, Any]) -> None:
        """Record a run as it begins."""
        with self._sessions.use() as session:
            self._ensure(session)
            self._insert(session, run)

    def finished(self, run: Mapping[str, Any]) -> None:
        """Record how a run ended. If its beginning was never recorded, the whole run is."""
        with self._sessions.use() as session:
            self._ensure(session)
            table = session.table(self.name)
            changes = {
                name: _literal(name, run.get(name.lower()))
                for name in ("STATUS", "STARTED_AT", "FINISHED_AT", "RESULT", "ERROR")
            }
            outcome = table.update(changes, table["ID"] == run["id"])
            if outcome.rows_updated == 0:
                self._insert(session, run)

    def recent(self, limit: int = 50) -> list[Record]:
        """The latest runs, newest first."""

        def collect() -> list[Any]:
            with self._sessions.use() as session:
                self._ensure(session)
                table = session.table(self.name)
                return table.sort(F.col("CREATED_AT").desc()).limit(limit).collect()

        return [_record(row.as_dict()) for row in self._sessions.retrying(collect)]

    def _insert(self, session: Any, run: Mapping[str, Any]) -> None:
        row = tuple(run.get(name.lower()) for name in _NAMES)
        frame = session.create_dataframe([row], schema=frames.struct(RUNS_COLUMNS))
        frame.write.save_as_table(self.name, mode="append")

    def _ensure(self, session: Any) -> None:
        if not self._ready:
            frames.empty(session, RUNS_COLUMNS).write.save_as_table(self.name, mode="ignore")
            self._ready = True


def _literal(name: str, value: Any) -> Column:
    if value is None:
        return F.lit(None)
    kind = _KINDS[name]
    if kind == "TIMESTAMP_TZ":
        return frames.timestamp(value)
    if kind == "VARIANT":
        return F.parse_json(F.lit(json.dumps(value)))
    return F.lit(value)


def _record(row: Mapping[str, Any]) -> Record:
    record: Record = {}
    for name, value in row.items():
        key = name.strip('"')
        kind = _KINDS.get(key)
        if kind == "TIMESTAMP_TZ":
            value = frames.as_utc(value)
        elif kind == "VARIANT" and isinstance(value, str):
            value = json.loads(value)
        elif value != value:  # noqa: PLR0124 - the emulator's NaN for a null
            value = None
        record[key.lower()] = value
    return record
