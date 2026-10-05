"""Background execution and bookkeeping of sync and reconcile runs."""

import logging
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from ..sync import SyncResult, utcnow
from .backend import Backend
from .schemas import ReconcileRequest, Run, RunTrigger, SyncRequest

logger = logging.getLogger(__name__)


class RunInProgress(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


class RunManager:
    """Runs one job at a time on a worker thread and keeps recent runs in memory."""

    def __init__(self, backend: Backend, history: int = 50) -> None:
        self._backend = backend
        self._history = history
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._futures: dict[str, Future[None]] = {}
        self._active: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync")

    def submit(
        self, request: SyncRequest | ReconcileRequest, trigger: RunTrigger = "manual"
    ) -> Run:
        kind = "reconcile" if isinstance(request, ReconcileRequest) else "sync"
        with self._lock:
            if self._active is not None:
                raise RunInProgress(self._active)
            run = Run(
                id=uuid4().hex,
                project=request.project or "",
                kind=kind,
                trigger=trigger,
                status="queued",
                request=request,
                created_at=utcnow(),
            )
            self._runs[run.id] = run
            while len(self._runs) > self._history:
                evicted, _ = self._runs.popitem(last=False)
                self._futures.pop(evicted, None)
            self._active = run.id
            self._futures[run.id] = self._executor.submit(self._execute, run.id, request)
            return run.model_copy(deep=True)

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.model_copy(deep=True) if run else None

    def recent(self) -> list[Run]:
        with self._lock:
            return [run.model_copy(deep=True) for run in reversed(self._runs.values())]

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        """Block until the run has finished."""
        self._futures[run_id].result(timeout)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _update(self, run_id: str, *, release: bool = False, **changes: Any) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                self._runs[run_id] = run.model_copy(update=changes)
            if release:
                self._active = None

    def _execute(self, run_id: str, request: SyncRequest | ReconcileRequest) -> None:
        self._update(run_id, status="running", started_at=utcnow())

        def progress(result: SyncResult) -> None:
            self._update(run_id, result=result.model_copy(deep=True))

        try:
            if isinstance(request, ReconcileRequest):
                result = self._backend.run_reconcile(request, progress)
            else:
                result = self._backend.run_sync(request, progress)
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            self._update(
                run_id,
                release=True,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utcnow(),
            )
        else:
            self._update(
                run_id, release=True, status="succeeded", result=result, finished_at=utcnow()
            )
