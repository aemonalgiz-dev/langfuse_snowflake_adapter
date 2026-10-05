"""What the sync service needs from the systems on either side of it."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any, Protocol

from ..entities import ApiVersion, Endpoint, EntitySpec, SyncPlan
from ..snowflake import EntityState, Key, LoadResult


class Source(Protocol):
    def get_project(self) -> dict[str, Any]: ...

    def iter_pages(
        self, endpoint: Endpoint, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[list[dict[str, Any]]]: ...


class Warehouse(Protocol):
    def ping(self) -> dict[str, Any]: ...

    def ensure_schema(self, plan: SyncPlan) -> None: ...

    def get_state(self, project_id: str) -> list[EntityState]: ...

    def set_watermark(
        self, project_id: str, entity: str, watermark: datetime, api_version: ApiVersion
    ) -> None: ...

    def set_reconciled(self, project_id: str, entity: str, reconciled_at: datetime) -> None: ...

    def load(
        self, spec: EntitySpec, project_id: str, records: Iterable[Mapping[str, Any]]
    ) -> LoadResult: ...

    def get_keys(
        self,
        spec: EntitySpec,
        project_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[Key, datetime | None]: ...

    def earliest(self, spec: EntitySpec, project_id: str) -> datetime | None: ...
