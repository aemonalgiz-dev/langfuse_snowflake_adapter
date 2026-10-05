"""Keeping runs beyond the life of the process."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from .schemas import ReconcileRequest, Run, SyncRequest

if TYPE_CHECKING:
    from ..snowflake import RunsTable

logger = logging.getLogger(__name__)


class RunLog(Protocol):
    @property
    def location(self) -> str:
        """Where the runs are kept, in words."""

    def started(self, run: Run) -> None: ...

    def finished(self, run: Run) -> None: ...

    def recent(self, limit: int) -> list[Run]: ...


class TableRunLog:
    """Runs kept in the Snowflake table beside the data."""

    def __init__(self, table: RunsTable) -> None:
        self._table = table

    @property
    def location(self) -> str:
        return self._table.location

    def started(self, run: Run) -> None:
        self._table.started(_record(run))

    def finished(self, run: Run) -> None:
        self._table.finished(_record(run))

    def recent(self, limit: int) -> list[Run]:
        runs = []
        for record in self._table.recent(limit):
            run = _run(record)
            if run is not None:
                runs.append(run)
        return runs


def _record(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "project": run.project,
        "kind": run.kind,
        "triggered_by": run.trigger,
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "request": run.request.model_dump(mode="json", by_alias=True),
        "result": run.result.model_dump(mode="json") if run.result is not None else None,
        "error": run.error,
    }


def _run(record: Mapping[str, Any]) -> Run | None:
    try:
        request_type = ReconcileRequest if record["kind"] == "reconcile" else SyncRequest
        status = record["status"]
        return Run(
            id=record["id"],
            project=record["project"],
            kind=record["kind"],
            trigger=record["triggered_by"],
            # Still marked as under way, but found in the table rather than in
            # this process: whatever was running it is gone.
            status="interrupted" if status in ("queued", "running") else status,
            request=request_type.model_validate(record.get("request") or {}),
            created_at=record["created_at"],
            started_at=record.get("started_at"),
            finished_at=record.get("finished_at"),
            result=record.get("result"),
            error=record.get("error"),
        )
    except (KeyError, ValidationError) as exc:
        # Written by another version, most likely. History is not worth failing over.
        logger.debug("Skipping a stored run that cannot be read: %s", exc)
        return None
