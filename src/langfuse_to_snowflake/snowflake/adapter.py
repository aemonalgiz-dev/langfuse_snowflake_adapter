"""Snowflake adapter: schema management, staged loads and sync state."""

from __future__ import annotations

import gzip
import json
import logging
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from ..config import SnowflakeSettings
from ..entities import (
    ApiVersion,
    EntitySpec,
    SyncPlan,
    load_table_name,
    state_table_name,
    table_name,
    validate_prefix,
)
from . import sql
from .auth import load_private_key
from .errors import SchemaConflictError
from .models import EntityState, Key, LoadResult
from .retry import build_retrying

logger = logging.getLogger(__name__)

Row = dict[str, Any]


class SnowflakeAdapter:
    """Loads Langfuse records into Snowflake and keeps the sync watermarks there.

    Records are written as gzipped NDJSON, uploaded to a temporary load table's
    stage, copied in, and merged into the target table.
    """

    def __init__(
        self,
        settings: SnowflakeSettings,
        *,
        table_prefix: str = "LANGFUSE_",
        batch_max_rows: int = 20_000,
        batch_max_bytes: int = 100 * 1024 * 1024,
        connection: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._prefix = validate_prefix(table_prefix)
        self._batch_max_rows = batch_max_rows
        self._batch_max_bytes = batch_max_bytes
        self._conn = connection
        self._state_table_ready = False
        self._retrying = build_retrying(settings.max_retries, sleep)

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._conn is None:
            self._conn = self._retrying(self._open)

    def _open(self) -> Any:
        import snowflake.connector

        settings = self._settings
        return snowflake.connector.connect(
            account=settings.account,
            user=settings.user,
            private_key=load_private_key(settings),
            role=settings.role,
            warehouse=settings.warehouse,
            database=settings.database,
            schema=settings.schema_name,
            application="langfuse_to_snowflake",
            session_parameters={"QUERY_TAG": "langfuse_to_snowflake", "TIMEZONE": "UTC"},
            # Fail at connect time if the database, schema or warehouse is missing.
            validate_default_parameters=True,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._state_table_ready = False

    def table_name(self, entity: str) -> str:
        return table_name(self._prefix, entity)

    @property
    def state_table(self) -> str:
        return state_table_name(self._prefix)

    def ping(self) -> Row:
        """The session context, as a connectivity check."""
        return self._run(
            'SELECT CURRENT_ACCOUNT() AS "account", CURRENT_USER() AS "user", '
            'CURRENT_ROLE() AS "role", CURRENT_WAREHOUSE() AS "warehouse", '
            'CURRENT_DATABASE() AS "database", CURRENT_SCHEMA() AS "schema"'
        )[0]

    def ensure_schema(self, plan: SyncPlan) -> None:
        """Create the state table, entity tables and derived views if missing."""
        self._ensure_state_table()
        for extract in plan.extracts:
            self._run(sql.create_table(self.table_name(extract.spec.name), extract.spec))
        for view in plan.views:
            name = self.table_name(view.name)
            existing = self._run(sql.select_object_type(), {"name": name})
            if existing and existing[0]["table_type"] != "VIEW":
                raise SchemaConflictError(
                    f"{name} already exists as a table, most likely from syncing with "
                    f"LANGFUSE_API_VERSION=v3. On v4 it is a view over "
                    f"{self.table_name(view.source)}. Rename the table to keep its history "
                    f"(ALTER TABLE {name} RENAME TO {name}_V3) and run again."
                )
            self._run(sql.create_view(view.name, name, self.table_name(view.source)))

    def _ensure_state_table(self) -> None:
        if not self._state_table_ready:
            self._run(sql.create_state_table(self.state_table))
            self._run(sql.add_reconciled_column(self.state_table))
            self._state_table_ready = True

    def get_state(self, project_id: str) -> list[EntityState]:
        # Reading state must also work before the first sync has created the table.
        self._ensure_state_table()
        rows = self._run(sql.select_state(self.state_table), {"project_id": project_id})
        return [
            EntityState(
                entity=row["entity"],
                watermark=_as_utc(row["watermark"]),
                api_version=row["api_version"],
                updated_at=_as_utc(row["updated_at"]),
                reconciled_at=_as_utc(row["reconciled_at"]) if row["reconciled_at"] else None,
            )
            for row in rows
        ]

    def set_reconciled(self, project_id: str, entity: str, reconciled_at: datetime) -> None:
        """Record a completed reconciliation. Only entities that have been synced have a row."""
        self._run(
            sql.update_reconciled(self.state_table),
            {
                "project_id": project_id,
                "entity": entity,
                "reconciled_at": _as_utc(reconciled_at).isoformat(),
            },
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
        params: dict[str, Any] = {"project_id": project_id}
        ranged = start is not None and end is not None
        if start is not None and end is not None:
            params["start"] = _as_utc(start).isoformat()
            params["end"] = _as_utc(end).isoformat()
        rows = self._run(sql.select_keys(self.table_name(spec.name), spec, ranged=ranged), params)
        return {
            tuple(row[name.lower()] for name in spec.key): (
                _as_utc(row["updated_at"]) if row["updated_at"] else None
            )
            for row in rows
        }

    def earliest(self, spec: EntitySpec, project_id: str) -> datetime | None:
        """The oldest timestamp held for an entity, or None if nothing is held."""
        rows = self._run(
            sql.select_earliest(self.table_name(spec.name), spec), {"project_id": project_id}
        )
        value = rows[0]["earliest"] if rows else None
        return _as_utc(value) if value else None

    def get_watermarks(self, project_id: str) -> dict[str, datetime]:
        return {state.entity: state.watermark for state in self.get_state(project_id)}

    def set_watermark(
        self, project_id: str, entity: str, watermark: datetime, api_version: ApiVersion
    ) -> None:
        self._run(
            sql.upsert_state(self.state_table),
            {
                "project_id": project_id,
                "entity": entity,
                "watermark": _as_utc(watermark).isoformat(),
                "api_version": api_version,
            },
        )

    def load(
        self, spec: EntitySpec, project_id: str, records: Iterable[Mapping[str, Any]]
    ) -> LoadResult:
        """Upsert records into the entity's table, in bounded batches."""
        table = self.table_name(spec.name)
        load_table = load_table_name(self._prefix, spec.name)
        total = LoadResult()
        with tempfile.TemporaryDirectory(prefix="langfuse_to_snowflake_") as directory:
            created = False
            for path in self._write_batches(Path(directory), spec.name, records):
                if not created:
                    self._run(sql.create_load_table(load_table))
                    created = True
                try:
                    total += self._retrying(
                        self._load_file, spec, project_id, table, load_table, path
                    )
                finally:
                    path.unlink(missing_ok=True)
        return total

    def _write_batches(
        self, directory: Path, name: str, records: Iterable[Mapping[str, Any]]
    ) -> Iterator[Path]:
        """Spool records to gzipped NDJSON files, yielding each one as it fills up."""
        remaining = iter(records)
        while True:
            path = directory / f"{name}_{uuid4().hex}.ndjson.gz"
            rows = size = 0
            with gzip.open(path, "wt", encoding="ascii", newline="\n") as writer:
                for record in remaining:
                    line = json.dumps(record, separators=(",", ":"))
                    writer.write(line)
                    writer.write("\n")
                    rows += 1
                    size += len(line) + 1
                    if rows >= self._batch_max_rows or size >= self._batch_max_bytes:
                        break
            if rows == 0:
                path.unlink()
                return
            yield path

    def _load_file(
        self, spec: EntitySpec, project_id: str, table: str, load_table: str, path: Path
    ) -> LoadResult:
        """Load one file. Starts by emptying the load table, so the whole unit can be repeated."""
        self._execute(sql.truncate(load_table))
        self._execute(sql.put(path, load_table))
        result = LoadResult()
        for row in self._execute(sql.copy(load_table, path.name)):
            if "rows_parsed" not in row:
                raise RuntimeError(f"COPY did not load {path.name}: {row.get('status')}")
            result.rows_rejected += int(row.get("errors_seen") or 0)
            result.first_error = result.first_error or row.get("first_error")
        if result.rows_rejected:
            logger.warning(
                "%s: Snowflake rejected %d record(s) from %s; first error: %s",
                table,
                result.rows_rejected,
                path.name,
                result.first_error,
            )
        merged = self._execute(sql.merge(table, load_table, spec), {"project_id": project_id})
        if merged:
            result.rows_inserted = int(merged[0].get("number of rows inserted") or 0)
            result.rows_updated = int(merged[0].get("number of rows updated") or 0)
        return result

    def _run(self, statement: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        """Execute an idempotent statement, retrying transient failures."""
        return self._retrying(self._execute, statement, params)

    def _execute(self, statement: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        if self._conn is None:
            raise RuntimeError("Not connected to Snowflake; call connect() first")
        cursor = self._conn.cursor()
        try:
            # Only pass params when there are some: with none, the connector
            # leaves "%" alone, which the @%table stage references rely on.
            if params:
                cursor.execute(statement, params)
            else:
                cursor.execute(statement)
            if cursor.description is None:
                return []
            names = [column[0].lower() for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        finally:
            cursor.close()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
