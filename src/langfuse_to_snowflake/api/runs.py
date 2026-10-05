"""Background execution and bookkeeping of sync and reconcile runs."""

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from typing import Any
from uuid import uuid4

from ..sync import SyncResult, utcnow
from .backend import Backend
from .runlog import RunLog
from .schemas import ReconcileRequest, Run, RunTrigger, SyncRequest

logger = logging.getLogger(__name__)


class RunInProgress(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


class RunManager:
    """Runs one job at a time on a worker thread and keeps the recent ones at hand.

    With a ``log``, runs are also written to it as they start and end, and the
    ones it already holds are shown as history. ``scope`` wraps each run, for
    what the run and its bookkeeping should share, such as one Snowflake login.
    """

    def __init__(
        self,
        backend: Backend,
        history: int = 50,
        *,
        log: RunLog | None = None,
        scope: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self._backend = backend
        self._history = history
        self._log = log
        self._scope = scope
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._earlier: list[Run] | None = None
        self._futures: dict[str, Future[None]] = {}
        self._active: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync")

    @property
    def kept_in(self) -> str | None:
        """Where runs are kept beyond this process, in words. None if nowhere."""
        return self._log.location if self._log is not None else None

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
            if run is not None:
                return run.model_copy(deep=True)
        return next((run for run in self._earlier_runs() if run.id == run_id), None)

    def recent(self) -> list[Run]:
        """This process's runs, then the ones recorded before it started, newest first."""
        with self._lock:
            own = [run.model_copy(deep=True) for run in reversed(self._runs.values())]
        seen = {run.id for run in own}
        earlier = [run for run in self._earlier_runs() if run.id not in seen]
        return (own + earlier)[: self._history]

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        """Block until the run has finished."""
        self._futures[run_id].result(timeout)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _earlier_runs(self) -> list[Run]:
        """What the log held when first asked. Read once; later runs are this process's own."""
        if self._log is None:
            return []
        with self._lock:
            if self._earlier is not None:
                return self._earlier
        try:
            earlier = self._log.recent(self._history)
        except Exception as exc:  # noqa: BLE001 - history is never worth failing over
            # Tried again next time: the history is worth having once it can be read.
            logger.warning("The history of runs could not be read: %s", exc)
            return []
        with self._lock:
            self._earlier = earlier
            return earlier

    def _update(self, run_id: str, *, release: bool = False, **changes: Any) -> Run | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run = self._runs[run_id] = run.model_copy(update=changes)
            if release:
                self._active = None
            return run

    def _record(self, write: Callable[[Run], None], run: Run | None) -> None:
        """Bookkeeping never decides how a run ends."""
        if run is None:
            return
        try:
            write(run)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Run %s could not be recorded: %s", run.id, exc)

    def _execute(self, run_id: str, request: SyncRequest | ReconcileRequest) -> None:
        started = self._update(run_id, status="running", started_at=utcnow())

        def progress(result: SyncResult) -> None:
            self._update(run_id, result=result.model_copy(deep=True))

        try:
            with self._scope():
                if self._log is not None:
                    self._record(self._log.started, started)
                try:
                    if isinstance(request, ReconcileRequest):
                        result = self._backend.run_reconcile(request, progress)
                    else:
                        result = self._backend.run_sync(request, progress)
                except Exception as exc:
                    logger.exception("Run %s failed", run_id)
                    ended = self._update(
                        run_id,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                        finished_at=utcnow(),
                    )
                else:
                    ended = self._update(
                        run_id, status="succeeded", result=result, finished_at=utcnow()
                    )
                if self._log is not None:
                    self._record(self._log.finished, ended)
        except Exception as exc:
            # The scope itself could not be entered or left, a login most likely.
            logger.exception("Run %s failed", run_id)
            run = self._runs.get(run_id)
            if run is not None and run.status == "running":
                self._update(
                    run_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=utcnow(),
                )
        finally:
            self._update(run_id, release=True)
