"""The run manager with a log to write to and a scope to run in."""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from langfuse_to_snowflake.api.runs import RunManager
from langfuse_to_snowflake.api.schemas import ReconcileRequest, Run, SyncRequest
from langfuse_to_snowflake.sync import SyncResult

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


class Backend:
    def __init__(self, events: list) -> None:
        self.events = events
        self.error: Exception | None = None

    def run_sync(self, request, progress):
        self.events.append("run")
        if self.error:
            raise self.error
        return SyncResult(project_id="p", api_version="v4", started_at=NOW, entities=[])

    run_reconcile = run_sync


class Log:
    location = "the runs table"

    def __init__(self, events: list, earlier: list[Run] | None = None) -> None:
        self.events = events
        self.earlier = earlier or []
        self.reads = 0
        self.written: list[Run] = []

    def started(self, run: Run) -> None:
        self.events.append(f"started {run.status}")
        self.written.append(run)

    def finished(self, run: Run) -> None:
        self.events.append(f"finished {run.status}")
        self.written.append(run)

    def recent(self, limit: int) -> list[Run]:
        self.reads += 1
        return self.earlier[:limit]


def _earlier(identifier: str, status: str = "succeeded") -> Run:
    return Run(
        id=identifier,
        project="default",
        kind="sync",
        status=status,
        request=SyncRequest(),
        created_at=NOW,
    )


@pytest.fixture
def events() -> list:
    return []


def _finish(manager: RunManager, request=None) -> Run:
    run = manager.submit(request or SyncRequest(project="default"))
    manager.wait(run.id, timeout=10)
    return manager.get(run.id)


def test_the_run_and_its_record_happen_inside_one_scope(events):
    @contextmanager
    def scope():
        events.append("enter")
        yield
        events.append("leave")

    manager = RunManager(Backend(events), log=Log(events), scope=scope)

    run = _finish(manager)

    assert events == ["enter", "started running", "run", "finished succeeded", "leave"]
    assert run.status == "succeeded"


def test_what_is_written_is_the_run_as_it_stood(events):
    log = Log(events)
    manager = RunManager(Backend(events), log=log)

    run = _finish(manager, ReconcileRequest(project="default", full=True))

    begun, ended = log.written
    assert (begun.id, begun.kind, begun.status) == (run.id, "reconcile", "running")
    assert begun.started_at is not None and begun.finished_at is None
    assert (ended.status, ended.finished_at, ended.result) == (
        "succeeded",
        run.finished_at,
        run.result,
    )


def test_a_failed_run_is_written_as_failed(events):
    backend = Backend(events)
    backend.error = RuntimeError("no")
    manager = RunManager(backend, log=Log(events))

    run = _finish(manager)

    assert events == ["started running", "run", "finished failed"]
    assert (run.status, run.error) == ("failed", "RuntimeError: no")


def test_a_scope_that_cannot_be_entered_fails_the_run_and_frees_the_worker(events):
    @contextmanager
    def scope():
        if not events:
            events.append("refused")
            raise ConnectionError("no login")
        yield

    manager = RunManager(Backend(events), log=Log(events), scope=scope)

    run = _finish(manager)

    assert (run.status, run.error) == ("failed", "ConnectionError: no login")
    assert run.finished_at is not None
    assert "run" not in events
    # The next run is not held up by the failed one.
    assert _finish(manager).status == "succeeded"


def test_a_scope_that_fails_on_the_way_out_does_not_undo_the_run(events):
    @contextmanager
    def scope():
        yield
        raise ConnectionError("lost while closing")

    manager = RunManager(Backend(events), scope=scope)

    assert _finish(manager).status == "succeeded"


def test_earlier_runs_are_read_once_and_listed_after_this_processes_own(events):
    log = Log(events, [_earlier("old-2"), _earlier("old-1", "failed")])
    manager = RunManager(Backend(events), log=log)

    assert [run.id for run in manager.recent()] == ["old-2", "old-1"]
    run = _finish(manager)

    assert [item.id for item in manager.recent()] == [run.id, "old-2", "old-1"]
    assert manager.get("old-1").status == "failed"
    assert manager.get("nope") is None
    assert log.reads == 1


def test_a_run_of_this_process_is_not_listed_twice(events):
    manager = RunManager(Backend(events), log=Log(events))
    run = _finish(manager)
    # As a later read of the table would return it.
    manager._earlier = [_earlier(run.id), _earlier("old")]

    assert [item.id for item in manager.recent()] == [run.id, "old"]


def test_history_is_limited_to_what_is_asked_for(events):
    log = Log(events, [_earlier(f"old-{index}") for index in range(5)])
    manager = RunManager(Backend(events), history=3, log=log)

    _finish(manager)

    assert len(manager.recent()) == 3


def test_reading_earlier_runs_is_tried_again_after_it_failed(events):
    log = Log(events, [_earlier("old")])
    log.recent = lambda limit: (_ for _ in ()).throw(ConnectionError("down"))
    manager = RunManager(Backend(events), log=log)

    assert manager.recent() == []

    del log.recent
    assert [run.id for run in manager.recent()] == ["old"]


def test_where_runs_are_kept_is_passed_on(events):
    assert RunManager(Backend(events)).kept_in is None
    assert RunManager(Backend(events), log=Log(events)).kept_in == "the runs table"
