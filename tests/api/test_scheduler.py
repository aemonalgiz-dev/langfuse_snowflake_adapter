import threading
from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.api.runs import RunInProgress
from langfuse_to_snowflake.api.scheduler import Scheduler

START = datetime(2026, 3, 10, 12, tzinfo=UTC)


class Harness:
    """A scheduler with a clock, an interval and a submit function the test controls."""

    def __init__(self, minutes: int = 30) -> None:
        self.minutes = minutes
        self.now = START
        self.submitted: list[datetime] = []
        self.busy = False
        self.scheduler = Scheduler(lambda: self.minutes, self._submit, clock=lambda: self.now)

    def _submit(self) -> None:
        if self.busy:
            raise RunInProgress("abc")
        self.submitted.append(self.now)

    def advance(self, minutes: int) -> bool:
        self.now += timedelta(minutes=minutes)
        return self.scheduler.tick()


def test_the_first_sync_is_due_at_once_and_then_every_interval():
    harness = Harness(minutes=30)

    assert harness.scheduler.tick()
    assert not harness.advance(29)
    assert harness.advance(1)
    assert not harness.advance(10)

    assert harness.submitted == [START, START + timedelta(minutes=30)]


def test_a_schedule_of_zero_never_runs():
    harness = Harness(minutes=0)

    assert not harness.scheduler.tick()
    assert not harness.advance(600)
    status = harness.scheduler.status()
    assert (status.enabled, status.every_minutes, status.next_run_at) == (False, 0, None)


def test_a_change_of_interval_takes_effect_without_a_restart():
    harness = Harness(minutes=0)
    harness.scheduler.tick()

    harness.minutes = 60
    assert harness.scheduler.tick()
    harness.minutes = 10
    assert not harness.advance(9)
    assert harness.advance(1)
    harness.minutes = 0
    assert not harness.advance(600)

    assert len(harness.submitted) == 2


def test_a_sync_that_cannot_start_is_tried_again_on_the_next_tick():
    harness = Harness(minutes=30)
    harness.busy = True

    assert not harness.scheduler.tick()
    assert harness.scheduler.status().last_run_at is None

    harness.busy = False
    assert harness.advance(1)
    assert harness.submitted == [START + timedelta(minutes=1)]


def test_status_reports_when_the_next_sync_is_due():
    harness = Harness(minutes=45)

    before = harness.scheduler.status()
    assert (before.enabled, before.last_run_at, before.next_run_at) == (True, None, START)

    harness.scheduler.tick()
    after = harness.scheduler.status()
    assert after.last_run_at == START
    assert after.next_run_at == START + timedelta(minutes=45)


def test_the_background_thread_ticks_until_stopped():
    ticked = threading.Event()
    scheduler = Scheduler(lambda: 1, ticked.set, tick_seconds=0.01)

    scheduler.start()
    scheduler.start()  # starting twice is harmless
    assert ticked.wait(2)
    scheduler.stop()

    assert threading.active_count() >= 1
    assert not any(thread.name == "scheduler" for thread in threading.enumerate())


def test_a_failing_submit_does_not_kill_the_thread(caplog):
    calls = []
    second = threading.Event()

    def submit() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("snowflake is down")
        second.set()

    now = [START]

    def clock() -> datetime:
        now[0] += timedelta(hours=1)
        return now[0]

    scheduler = Scheduler(lambda: 1, submit, clock=clock, tick_seconds=0.01)
    with caplog.at_level("ERROR"):
        scheduler.start()
        assert second.wait(2)
        scheduler.stop()

    assert "The scheduler could not start a sync" in caplog.text


@pytest.mark.parametrize("minutes", [5, 1440])
def test_every_minutes_is_reported_as_configured(minutes):
    assert Harness(minutes).scheduler.status().every_minutes == minutes
