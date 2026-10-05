"""What the API runs syncs against."""

from collections.abc import Callable, Sequence
from typing import Protocol

from ..config import Settings
from ..snowflake import EntityState
from ..sync import EntitySchema, SyncResult, open_service, open_source
from .schemas import ReconcileRequest, SyncRequest

Progress = Callable[[SyncResult], None]


class Backend(Protocol):
    """Each call is for one project; requests arrive with their ``project`` filled in."""

    def run_sync(self, request: SyncRequest, progress: Progress) -> SyncResult: ...

    def run_reconcile(self, request: ReconcileRequest, progress: Progress) -> SyncResult: ...

    def read_state(self, project: str) -> list[EntityState]: ...

    def discover(
        self, project: str, entities: Sequence[str] | None, sample: int
    ) -> list[EntitySchema]: ...


class LiveBackend:
    """Runs against a project's Langfuse keys and the shared Snowflake account.

    The settings are asked for at the start of every run, so that a change made
    in the web app applies from the next run on.
    """

    def __init__(self, settings: Callable[[str], Settings]) -> None:
        self._settings = settings

    def run_sync(self, request: SyncRequest, progress: Progress) -> SyncResult:
        with open_service(self._settings(request.project or "")) as service:
            return service.run(
                request.entities,
                request.start,
                request.end,
                progress,
                sample_rate=request.sample_rate,
                filters=request.filters,
            )

    def run_reconcile(self, request: ReconcileRequest, progress: Progress) -> SyncResult:
        with open_service(self._settings(request.project or "")) as service:
            return service.reconcile(
                request.entities, request.start, request.end, progress, full=request.full
            )

    def read_state(self, project: str) -> list[EntityState]:
        with open_service(self._settings(project)) as service:
            return service.state()

    def discover(
        self, project: str, entities: Sequence[str] | None, sample: int
    ) -> list[EntitySchema]:
        # Reads Langfuse only, so Snowflake need not be reachable to look at the data.
        with open_source(self._settings(project)) as service:
            return service.discover(entities, sample)
