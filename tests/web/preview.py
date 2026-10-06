"""The web app on made-up data, to look at it with no Langfuse and no Snowflake.

    python -m tests.web.preview

then open http://127.0.0.1:8011. The service is the real one: syncs run through
the real client, sync service and adapter. Only the two ends are stand-ins.
Langfuse is answered here, for two projects that log a trace every few minutes
up to now, and Snowflake is Snowpark's local emulator, so everything is gone
when the process ends.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import uvicorn
from snowflake.snowpark import Session

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.api.runlog import TableRunLog
from langfuse_to_snowflake.api.schemas import ReconcileRequest, Run, SyncRequest
from langfuse_to_snowflake.config import (
    ApiSettings,
    Deployment,
    LangfuseSettings,
    Settings,
    SnowflakeSettings,
    SyncSettings,
)
from langfuse_to_snowflake.langfuse import LangfuseClient
from langfuse_to_snowflake.snowflake import RunsTable, Sessions, SettingsTable, SnowflakeAdapter
from langfuse_to_snowflake.sync import Runtime, SyncService, utcnow
from tests.fakes import LocalCatalog

PORT = 8011
SCHEMA = "ANALYTICS.LANGFUSE"
# How long the stand-in Langfuse takes over a page, so that a run can be watched.
PAGE_SECONDS = 0.7
PAGE_SIZE = 60
# Review data does not move while the preview runs.
STARTED = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

_QUESTIONS = (
    "How do I rotate an API key?",
    "Why was my invoice higher this month?",
    "Can I export traces to a warehouse?",
    "What is the rate limit on the ingestion API?",
)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SampleLangfuse:
    """Answers like Langfuse's API for a project that logs a trace every few minutes."""

    def __init__(self, name: str, every_minutes: int, *, reviewed: bool = True) -> None:
        self._name = name
        self._step = timedelta(minutes=every_minutes)
        self._reviewed = reviewed
        self.delay = 0.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/api/public/projects":
            return httpx.Response(
                200, json={"data": [{"id": f"proj-{self._name}", "name": self._name}]}
            )
        if path == "/api/public/v2/observations":
            rows = self._between(params["fromStartTime"], params["toStartTime"], self._observations)
            return self._cursor_page(rows, params)
        if path == "/api/public/v3/scores":
            rows = self._between(params["fromTimestamp"], params["toTimestamp"], self._scores)
            return self._cursor_page(rows, params)
        if not self._reviewed:
            # As a plan without human review answers.
            return httpx.Response(403, json={"message": "Annotation queues are not in this plan"})
        done = {"page": 1, "totalPages": 1}
        if path == "/api/public/comments":
            return httpx.Response(200, json={"data": self._comments(), "meta": done})
        if path == "/api/public/annotation-queues":
            return httpx.Response(200, json={"data": self._queues(), "meta": done})
        if path.endswith("/items"):
            queue = path.split("/")[-2]
            return httpx.Response(200, json={"data": self._items(queue), "meta": done})
        return httpx.Response(404, json={"message": f"no route for {path}"})

    def _cursor_page(self, rows: list[dict], params: httpx.QueryParams) -> httpx.Response:
        time.sleep(self.delay)
        start = int(params.get("cursor") or 0)
        page = rows[start : start + PAGE_SIZE]
        more = start + PAGE_SIZE < len(rows)
        meta = {"cursor": str(start + PAGE_SIZE)} if more else {}
        return httpx.Response(200, json={"data": page, "meta": meta})

    def _traces(self, start: datetime, end: datetime) -> Iterator[tuple[int, datetime]]:
        """The number and time of every trace in the range. None are from the future."""
        step = self._step.total_seconds()
        end = min(end, utcnow())
        number = int(-(-start.timestamp() // step))
        while number * step < end.timestamp():
            yield number, datetime.fromtimestamp(number * step, UTC)
            number += 1

    def _between(self, start: str, end: str, make) -> list[dict]:
        window = datetime.fromisoformat(start), datetime.fromisoformat(end)
        return [row for number, at in self._traces(*window) for row in make(number, at)]

    def _observations(self, number: int, at: datetime) -> list[dict]:
        trace = f"trace-{number}"
        failed = number % 23 == 0
        shared = {
            "traceId": trace,
            "traceName": "support-chat",
            "userId": f"user-{number % 17}",
            "sessionId": f"session-{number // 4}",
            "environment": "staging" if number % 5 == 0 else "production",
            "release": "2026.10.1",
            "tags": ["chat", "web"],
            "level": "DEFAULT",
            "createdAt": _stamp(at),
            "updatedAt": _stamp(at + timedelta(seconds=4)),
        }
        tokens = {"input": 820 + number % 300, "output": 140 + number % 90}
        tokens["total"] = tokens["input"] + tokens["output"]
        cost = round(tokens["input"] * 3e-6 + tokens["output"] * 15e-6, 6)
        question = _QUESTIONS[number % len(_QUESTIONS)]
        return [
            {
                **shared,
                "id": f"{trace}-request",
                "parentObservationId": None,
                "type": "SPAN",
                "name": "handle-request",
                "startTime": _stamp(at),
                "endTime": _stamp(at + timedelta(seconds=3.1)),
                "latency": 3.1,
                "level": "ERROR" if failed else "DEFAULT",
                "statusMessage": "Upstream timed out" if failed else None,
                "input": {"question": question},
                "output": None if failed else {"answer": "Here is how…", "sources": 3},
                "metadata": {
                    "tier": "pro" if number % 3 else "free",
                    "customer": f"customer-{number % 9}",
                    "email": f"user{number % 17}@example.com",
                    "channel": "web",
                },
            },
            {
                **shared,
                "id": f"{trace}-retrieve",
                "parentObservationId": f"{trace}-request",
                "type": "SPAN",
                "name": "retrieve-context",
                "startTime": _stamp(at + timedelta(seconds=0.1)),
                "endTime": _stamp(at + timedelta(seconds=0.5)),
                "latency": 0.4,
                "input": {"query": question, "top_k": 5},
                "output": {"documents": 5},
                "metadata": {"index": "help-centre"},
            },
            {
                **shared,
                "id": f"{trace}-answer",
                "parentObservationId": f"{trace}-request",
                "type": "GENERATION",
                "name": "answer",
                "startTime": _stamp(at + timedelta(seconds=0.6)),
                "endTime": _stamp(at + timedelta(seconds=3.0)),
                "completionStartTime": _stamp(at + timedelta(seconds=1.1)),
                "latency": 2.4,
                "timeToFirstToken": 0.5,
                "model": "claude-sonnet-5-5",
                "modelParameters": {"temperature": 0.2, "max_tokens": 1024},
                "promptName": "support-answer",
                "promptVersion": 7,
                "usageDetails": tokens,
                "costDetails": {"total": cost},
                "totalCost": cost,
                "input": [{"role": "user", "content": question}],
                "output": {"role": "assistant", "content": "Here is how…"},
                "metadata": {"prompt_variant": "b"},
            },
        ]

    def _scores(self, number: int, at: datetime) -> list[dict]:
        if number % 3:
            return []
        when = _stamp(at)
        return [
            {
                "id": f"score-{number}",
                "name": "helpfulness",
                "dataType": "NUMERIC",
                "source": "EVAL",
                "value": round(0.55 + (number % 9) / 20, 2),
                "subject": {"kind": "trace", "id": f"trace-{number}"},
                "environment": "production",
                "comment": None,
                "timestamp": when,
                "createdAt": when,
                "updatedAt": when,
            }
        ]

    def _comments(self) -> list[dict]:
        day = STARTED - timedelta(days=1)
        return [
            {
                "id": f"comment-{index}",
                "objectType": "TRACE",
                "objectId": f"trace-{index * 11}",
                "content": text,
                "authorUserId": "reviewer-1",
                "createdAt": _stamp(day + timedelta(minutes=index * 14)),
                "updatedAt": _stamp(day + timedelta(minutes=index * 14)),
            }
            for index, text in enumerate(
                ("Wrong plan quoted.", "Good answer, slow.", "Should have escalated."), start=1
            )
        ]

    def _queues(self) -> list[dict]:
        created = _stamp(STARTED - timedelta(days=40))
        return [
            {"id": "weekly", "name": "Weekly review", "createdAt": created, "updatedAt": created},
            {
                "id": "escalations",
                "name": "Escalations",
                "createdAt": created,
                "updatedAt": created,
            },
        ]

    def _items(self, queue: str) -> list[dict]:
        created = _stamp(STARTED - timedelta(days=2))
        return [
            {
                "id": f"{queue}-item-{index}",
                "queueId": queue,
                "objectType": "TRACE",
                "objectId": f"trace-{index * 7}",
                "status": "COMPLETED" if index % 2 else "PENDING",
                "createdAt": created,
                "updatedAt": created,
            }
            for index in range(1, 4)
        ]


class PreviewBackend:
    """What ``LiveBackend`` does, with the stand-ins in place of the two services."""

    def __init__(self, deployment: Deployment, sessions: Sessions, langfuse: dict) -> None:
        self._deployment = deployment
        self._sessions = sessions
        self._langfuse = langfuse

    @contextmanager
    def _service(self, project: str | None, clock=utcnow) -> Iterator[SyncService]:
        settings = self._deployment.store(project).current()
        adapter = SnowflakeAdapter(sessions=self._sessions, table_prefix=settings.sync.table_prefix)
        transport = httpx.MockTransport(self._langfuse[settings.langfuse.name])
        with LangfuseClient(settings.langfuse, transport=transport) as client, adapter:
            yield SyncService(settings.langfuse, settings.sync, client, adapter, clock=clock)

    def run_sync(self, request, progress, clock=utcnow):
        self._deployment.refresh()
        with self._service(request.project, clock) as service:
            return service.run(
                request.entities,
                request.start,
                request.end,
                progress,
                sample_rate=request.sample_rate,
                filters=request.filters,
            )

    def run_reconcile(self, request, progress, clock=utcnow):
        self._deployment.refresh()
        with self._service(request.project, clock) as service:
            return service.reconcile(
                request.entities, request.start, request.end, progress, full=request.full
            )

    def read_state(self, project):
        with self._service(project) as service:
            return service.state()

    def discover(self, project, entities, sample):
        with self._service(project) as service:
            return service.discover(entities, sample)


def _project(name: str, snowflake: SnowflakeSettings) -> Settings:
    return Settings(
        langfuse=LangfuseSettings(
            _env_file=None,
            name=name,
            host="https://cloud.langfuse.com",
            public_key="pk-preview",
            secret_key="sk-preview",
        ),
        snowflake=snowflake,
        sync=SyncSettings(_env_file=None, initial_backfill_days=3),
        api=ApiSettings(_env_file=None),
    )


def _earlier_runs(backend: PreviewBackend, log: TableRunLog, project: str) -> None:
    """A past for the first project: real runs, recorded as if they happened earlier today."""
    now = utcnow()

    def record(kind: str, ago: timedelta, trigger: str = "schedule", fails: str | None = None):
        at = now - ago
        request = (
            ReconcileRequest(project=project)
            if kind == "reconcile"
            else SyncRequest(project=project)
        )
        run = Run(
            id=f"earlier-{int(ago.total_seconds())}",
            project=project,
            kind=kind,
            trigger=trigger,
            status="running",
            request=request,
            created_at=at,
            started_at=at,
        )
        log.started(run)
        if fails:
            ended = run.model_copy(
                update={
                    "status": "failed",
                    "error": fails,
                    "finished_at": at + timedelta(seconds=31),
                }
            )
        else:
            execute = backend.run_reconcile if kind == "reconcile" else backend.run_sync
            result = execute(request, lambda _: None, lambda: at)
            took = timedelta(
                seconds=8 + sum(entity.rows_fetched for entity in result.entities) / 40
            )
            ended = run.model_copy(
                update={"status": "succeeded", "result": result, "finished_at": at + took}
            )
        log.finished(ended)

    record("sync", timedelta(hours=7), trigger="manual")
    record("sync", timedelta(hours=5))
    record(
        "sync",
        timedelta(hours=4),
        fails="LangfuseApiError: GET /api/public/v2/observations failed: HTTP 503: upstream unavailable",
    )
    record("sync", timedelta(hours=3))
    record("reconcile", timedelta(hours=2, minutes=30))
    record("sync", timedelta(minutes=50))
    # Left as it was when the service "stopped".
    at = now - timedelta(hours=9)
    log.started(
        Run(
            id="earlier-interrupted",
            project=project,
            kind="sync",
            trigger="schedule",
            status="running",
            request=SyncRequest(project=project),
            created_at=at,
            started_at=at,
        )
    )


def build():
    session = Session.builder.config("local_testing", True).create()
    catalog = LocalCatalog(session)
    sessions = Sessions(session=session, catalog=lambda _: catalog)
    snowflake = SnowflakeSettings(
        _env_file=None,
        account="acme-eu1",
        user="LANGFUSE_LOADER",
        role="LOADER",
        private_key="unused",
        warehouse="LOAD_WH",
        database="ANALYTICS",
        schema_name="LANGFUSE",
    )
    storage = SettingsTable(sessions, schema=SCHEMA)
    runs = RunsTable(sessions, schema=SCHEMA)
    langfuse = {
        "support-bot": SampleLangfuse("support-bot", every_minutes=12),
        "search": SampleLangfuse("search", every_minutes=45, reviewed=False),
    }
    storage.write("support-bot", {"sample_rate": 0.5})
    storage.write("support-bot", {"exclude_fields": ["observations:metadata.email"]})
    storage.write(
        "support-bot",
        {
            "exclude_fields": ["observations:metadata.email"],
            "filters": ["observations:environment=production"],
            "schedule_minutes": 30,
        },
    )
    deployment = Deployment.load({name: _project(name, snowflake) for name in langfuse}, storage)
    backend = PreviewBackend(deployment, sessions, langfuse)
    log = TableRunLog(runs)
    _earlier_runs(backend, log, "support-bot")
    for source in langfuse.values():
        source.delay = PAGE_SECONDS
    return create_app(Runtime(deployment, sessions, runs), backend)


if __name__ == "__main__":
    print(f"Preview on http://127.0.0.1:{PORT}  (made-up data; nothing leaves this machine)")
    uvicorn.run(build(), host="127.0.0.1", port=PORT, log_level="warning")
