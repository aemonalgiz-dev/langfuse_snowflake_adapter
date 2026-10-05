"""Starts a sync at a fixed interval, so the service needs no outside scheduler."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from ..sync import utcnow
from .runs import RunInProgress
from .schemas import ScheduleResponse

logger = logging.getLogger(__name__)


class Scheduler:
    """Calls ``submit`` every ``interval()`` minutes, for as long as that is above zero.

    The interval is asked for on every tick, so a change made in the web app
    takes effect without a restart. The first sync is due as soon as the
    schedule is on; sync is incremental, so starting one early costs little.
    """

    def __init__(
        self,
        interval: Callable[[], int],
        submit: Callable[[], object],
        *,
        clock: Callable[[], datetime] = utcnow,
        tick_seconds: float = 5.0,
    ) -> None:
        self._interval = interval
        self._submit = submit
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._last_run_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_seconds + 1)
            self._thread = None

    def status(self) -> ScheduleResponse:
        minutes = self._interval()
        return ScheduleResponse(
            enabled=minutes > 0,
            every_minutes=minutes,
            last_run_at=self._last_run_at,
            next_run_at=self._next_run_at(minutes),
        )

    def tick(self) -> bool:
        """Start a sync if one is due. Returns whether it did."""
        due = self._next_run_at(self._interval())
        now = self._clock()
        if due is None or due > now:
            return False
        try:
            self._submit()
        except RunInProgress:
            # Whatever is running comes first; this one is tried again next tick.
            return False
        self._last_run_at = now
        return True

    def _next_run_at(self, minutes: int) -> datetime | None:
        if minutes <= 0:
            return None
        if self._last_run_at is None:
            return self._clock()
        return self._last_run_at + timedelta(minutes=minutes)

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception("The scheduler could not start a sync")
