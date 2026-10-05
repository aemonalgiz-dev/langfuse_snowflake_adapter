"""Snowflake adapter: schema management, merged loads and sync state.

Written against Snowpark DataFrames rather than SQL text, so that the same
code runs on Snowflake and on Snowpark's local emulator, which the tests use.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime
from typing import Any, Self

from snowflake.snowpark import Session
from snowflake.snowpark import functions as F

from ..config import SnowflakeSettings
from ..entities import (
    ApiVersion,
    EntitySpec,
    SyncPlan,
    state_table_name,
    table_name,
    validate_prefix,
)
from . import frames, rows, views
from .errors import SchemaConflictError
from .layout import (
    LOADED_AT,
    PROJECT_ID,
    RAW,
    RAW_HASH,
    STATE_COLUMNS,
    ColumnDef,
    entity_columns,
)
from .models import EntityState, Key, LoadResult
from .session import ObjectKind, Sessions

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


class SnowflakeAdapter:
    """Loads Langfuse records into Snowflake and keeps the sync watermarks there.

    Records arrive as typed rows, in bounded batches, and each batch is merged
    into the entity's table on its key.
    """

    def __init__(
        self,
        settings: SnowflakeSettings | None = None,
        *,
        table_prefix: str = "LANGFUSE_",
        batch_max_rows: int = 20_000,
        batch_max_bytes: int = 32 * _MIB,
        record_max_bytes: int = 16 * _MIB,
        sessions: Sessions | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_uploaded_values: int = frames.MIN_UPLOADED_VALUES,
    ) -> None:
        self._sessions = sessions or Sessions(settings, sleep=sleep)
        self._prefix = validate_prefix(table_prefix)
        self._batch_max_rows = batch_max_rows
        self._batch_max_bytes = batch_max_bytes
        self._record_max_bytes = record_max_bytes
        self._min_uploaded_values = min_uploaded_values
        self._retrying = self._sessions.retrying
        self._session: Session | None = None
        self._scope = ExitStack()
        self._state_table_ready = False

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._session is None:
            self._session = self._scope.enter_context(self._sessions.use())

    def close(self) -> None:
        if self._session is not None:
            self._session = None
            self._state_table_ready = False
            self._scope.close()

    def table_name(self, entity: str) -> str:
        return table_name(self._prefix, entity)

    @property
    def state_table(self) -> str:
        return state_table_name(self._prefix)

    def ping(self) -> dict[str, Any]:
        """The session context, as a connectivity check."""
        session = self._connected()
        context = {
            "account": session.get_current_account,
            "user": session.get_current_user,
            "role": session.get_current_role,
            "warehouse": session.get_current_warehouse,
            "database": session.get_current_database,
            "schema": session.get_current_schema,
        }
        return {name: _unquoted(self._retrying(read)) for name, read in context.items()}

    def ensure_schema(self, plan: SyncPlan) -> None:
        """Create the state table, entity tables and derived views if missing.

        A table that exists but lacks a column this version loads gets it added.
        """
        session = self._connected()
        tables = {self.state_table: STATE_COLUMNS}
        for extract in plan.extracts:
            tables[self.table_name(extract.spec.name)] = entity_columns(extract.spec)
        derived = {self.table_name(view.name): view for view in plan.views}
        catalog = self._sessions.catalog(session)
        kinds: Mapping[str, ObjectKind] = self._retrying(catalog.kinds, [*tables, *derived])

        for name, columns in tables.items():
            if name not in kinds:
                self._retrying(self._create_table, name, columns)
                continue
            held = {_unquoted(field.name) for field in session.table(name).schema.fields}
            missing = [(column, kind) for column, kind in columns if column not in held]
            if missing:
                logger.info("%s: adding %s", name, ", ".join(column for column, _ in missing))
                self._retrying(catalog.add_columns, name, missing)
        self._state_table_ready = True

        for name, view in derived.items():
            source = self.table_name(view.source)
            if kinds.get(name) == "TABLE":
                raise SchemaConflictError(
                    f"{name} already exists as a table, most likely from syncing with "
                    f"LANGFUSE_API_VERSION=v3. On v4 it is a view over {source}. Rename the "
                    f"table to keep its history (ALTER TABLE {name} RENAME TO {name}_V3) and "
                    "run again."
                )
            statement = views.create_view(view.name, name, source)
            self._retrying(catalog.create_view, name, statement)

    def _create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        frames.empty(self._connected(), columns).write.save_as_table(name, mode="ignore")

    def _ensure_state_table(self) -> None:
        if not self._state_table_ready:
            self._retrying(self._create_table, self.state_table, STATE_COLUMNS)
            self._state_table_ready = True

    def get_state(self, project_id: str) -> list[EntityState]:
        # Reading state must also work before the first sync has created the table.
        self._ensure_state_table()
        frame = (
            self._connected()
            .table(self.state_table)
            .filter(F.col(PROJECT_ID) == project_id)
            .select("ENTITY", "WATERMARK", "API_VERSION", "UPDATED_AT", "RECONCILED_AT")
        )
        states = [
            EntityState(
                entity=row["ENTITY"],
                watermark=_required(row["WATERMARK"]),
                api_version=row["API_VERSION"],
                updated_at=_required(row["UPDATED_AT"]),
                reconciled_at=frames.as_utc(row["RECONCILED_AT"]),
            )
            for row in self._retrying(frame.collect)
        ]
        return sorted(states, key=lambda state: state.entity)

    def get_watermarks(self, project_id: str) -> dict[str, datetime]:
        return {state.entity: state.watermark for state in self.get_state(project_id)}

    def set_watermark(
        self, project_id: str, entity: str, watermark: datetime, api_version: ApiVersion
    ) -> None:
        self._ensure_state_table()
        self._retrying(self._merge_watermark, project_id, entity, watermark, api_version)

    def _merge_watermark(
        self, project_id: str, entity: str, watermark: datetime, api_version: ApiVersion
    ) -> None:
        session = self._connected()
        target = session.table(self.state_table)
        source = session.create_dataframe(
            [(project_id, entity, api_version)], schema=["PROJECT_ID", "ENTITY", "API_VERSION"]
        )
        values = {
            "WATERMARK": frames.timestamp(watermark),
            "API_VERSION": source["API_VERSION"],
            "UPDATED_AT": F.current_timestamp(),
        }
        target.merge(
            source,
            (target[PROJECT_ID] == source[PROJECT_ID]) & (target["ENTITY"] == source["ENTITY"]),
            [
                F.when_matched().update(values),
                F.when_not_matched().insert(
                    {
                        PROJECT_ID: source[PROJECT_ID],
                        "ENTITY": source["ENTITY"],
                        **values,
                        "RECONCILED_AT": F.lit(None),
                    }
                ),
            ],
        )

    def set_reconciled(self, project_id: str, entity: str, reconciled_at: datetime) -> None:
        """Record a completed reconciliation. Only entities that have been synced have a row."""
        self._ensure_state_table()
        target = self._connected().table(self.state_table)
        self._retrying(
            target.update,
            {"RECONCILED_AT": frames.timestamp(reconciled_at)},
            (target[PROJECT_ID] == project_id) & (target["ENTITY"] == entity),
        )

    def get_keys(
        self,
        spec: EntitySpec,
        project_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[Key, datetime | None]:
        """The rows held, by key, with their updatedAt.

        With a range, only those whose timestamp is in ``[start, end)``.
        """
        frame = self._table(spec).filter(F.col(PROJECT_ID) == project_id)
        if start is not None and end is not None:
            at = F.col(spec.time_column)
            frame = frame.filter((at >= frames.timestamp(start)) & (at < frames.timestamp(end)))
        has_updated_at = any(column.name == "UPDATED_AT" for column in spec.columns)
        columns = [*spec.key, *(["UPDATED_AT"] if has_updated_at else [])]
        width = len(spec.key)
        return {
            tuple(row[:width]): frames.as_utc(row[width]) if has_updated_at else None
            for row in self._retrying(frame.select(columns).collect)
        }

    def earliest(self, spec: EntitySpec, project_id: str) -> datetime | None:
        """The oldest timestamp held for an entity, or None if nothing is held."""
        frame = (
            self._table(spec)
            .filter(F.col(PROJECT_ID) == project_id)
            .agg(F.min(F.col(spec.time_column)).alias("EARLIEST"))
        )
        found = self._retrying(frame.collect)
        return frames.as_utc(found[0][0]) if found else None

    def load(
        self, spec: EntitySpec, project_id: str, records: Iterable[Mapping[str, Any]]
    ) -> LoadResult:
        """Upsert records into the entity's table, in bounded batches.

        A record that cannot be loaded, because it has no ID or is larger than
        a row may be, is counted as rejected and the rest carries on.
        """
        table = self.table_name(spec.name)
        total = LoadResult()
        for batch in rows.batches(
            spec,
            project_id,
            records,
            max_rows=self._batch_max_rows,
            max_bytes=self._batch_max_bytes,
            max_record_bytes=self._record_max_bytes,
        ):
            if batch.rows_rejected:
                logger.warning(
                    "%s: %d record(s) could not be loaded; first reason: %s",
                    table,
                    batch.rows_rejected,
                    batch.first_error,
                )
                total += LoadResult(
                    rows_rejected=batch.rows_rejected, first_error=batch.first_error
                )
            if batch.rows:
                # Merging the same batch again changes nothing, so it can be repeated.
                total += self._retrying(self._merge, spec, batch.rows)
        return total

    def _merge(self, spec: EntitySpec, batch: Sequence[rows.Row]) -> LoadResult:
        """Upsert one batch, keyed on PROJECT_ID plus the entity key.

        Rows whose record is unchanged are left alone, so _LOADED_AT only moves
        when a record actually changed and re-reading the lookback window is cheap.
        """
        session = self._connected()
        target = session.table(self.table_name(spec.name))
        source = frames.batch(session, spec, batch, self._min_uploaded_values)

        matches = target[PROJECT_ID] == source[PROJECT_ID]
        for name in spec.key:
            # Only ID is guaranteed non-null (legacy observations may lack a trace).
            matches = matches & (
                target[name] == source[name]
                if name == "ID"
                else target[name].equal_null(source[name])
            )
        data = [column.name for column in spec.columns] + [RAW, RAW_HASH]
        changed = target[RAW_HASH].is_null() | (target[RAW_HASH] != source[RAW_HASH])
        update = {name: source[name] for name in data if name not in spec.key}
        insert = {name: source[name] for name in [PROJECT_ID, *data]}
        outcome = target.merge(
            source,
            matches,
            [
                F.when_matched(changed).update({**update, LOADED_AT: F.current_timestamp()}),
                F.when_not_matched().insert({**insert, LOADED_AT: F.current_timestamp()}),
            ],
        )
        return LoadResult(rows_inserted=outcome.rows_inserted, rows_updated=outcome.rows_updated)

    def _table(self, spec: EntitySpec) -> Any:
        return self._connected().table(self.table_name(spec.name))

    def _connected(self) -> Session:
        if self._session is None:
            raise RuntimeError("Not connected to Snowflake; call connect() first")
        return self._session


def _unquoted(name: str | None) -> str | None:
    """Snowpark quotes identifiers it reports; the names themselves are plain."""
    return name.strip('"') if name else name


def _required(value: Any) -> datetime:
    found = frames.as_utc(value)
    assert found is not None
    return found
