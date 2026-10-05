import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from langfuse_to_snowflake.cli import app, support
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.langfuse import LangfuseApiError
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

ENV = {
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
    "SNOWFLAKE_ACCOUNT": "acct",
    "SNOWFLAKE_USER": "loader",
    "SNOWFLAKE_PRIVATE_KEY_PATH": "rsa_key.p8",
    "SNOWFLAKE_WAREHOUSE": "WH",
    "SNOWFLAKE_DATABASE": "DB",
    "SNOWFLAKE_SCHEMA": "LANGFUSE",
    # Settings in Snowflake are covered by tests of their own.
    "SYNC_STORE": "file",
    # One window for the whole 30-day backfill, so the fake source is read once.
    "SYNC_WINDOW_HOURS": "720",
}
NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture
def backends(monkeypatch):
    """Run CLI commands against fakes instead of live connections."""
    source = FakeSource(
        {
            "/api/public/v3/scores": [
                [{"id": "s1", "name": "accuracy"}, {"id": "s2", "name": "toxicity"}]
            ]
        }
    )
    warehouse = FakeWarehouse()

    @contextmanager
    def fake_open_service(settings, sessions=None):
        yield SyncService(settings.langfuse, settings.sync, source, warehouse, clock=lambda: NOW)

    monkeypatch.setattr(support, "open_service", fake_open_service)
    return source, warehouse


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("check", "init", "sync", "reconcile", "status", "serve"):
        assert command in result.output


def test_sync_help_shows_the_filter_syntax():
    result = runner.invoke(app, ["sync", "--help"])

    assert "[entity:]field<op>value" in " ".join(result.output.split())


def test_missing_configuration_is_explained():
    result = runner.invoke(app, ["sync"], env={})

    assert result.exit_code == 2
    assert "LANGFUSE_PUBLIC_KEY: Field required" in result.output
    assert "Traceback" not in result.output


def test_sync_prints_a_summary(backends):
    _, warehouse = backends

    result = runner.invoke(app, ["sync", "-e", "scores"], env=ENV)

    assert result.exit_code == 0, result.output
    assert "Project demo (proj-1), Langfuse API v4" in result.output
    assert (
        "LANGFUSE_SCORES: fetched 2, skipped 0, inserted 2, updated 0, rejected 0" in result.output
    )
    assert "Sampling" not in result.output and "Filters" not in result.output
    assert warehouse.watermarks == {"scores": NOW}


def test_sync_json_output_and_explicit_range(backends):
    source, warehouse = backends

    result = runner.invoke(
        app,
        ["sync", "-e", "scores", "--from", "2026-03-01", "--to", "2026-03-02T00:00:00Z", "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["entities"][0]["rows_fetched"] == 2
    assert source.calls == [
        (
            "/api/public/v3/scores",
            datetime(2026, 3, 1, tzinfo=UTC),
            datetime(2026, 3, 2, tzinfo=UTC),
        )
    ]
    assert warehouse.watermarks == {}


def test_sync_with_filters_and_sampling(backends):
    _, warehouse = backends

    result = runner.invoke(
        app,
        ["sync", "-e", "scores", "-f", "scores:name=accuracy", "--sample-rate", "0.5"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1"]
    assert "Sampling 50.00% of traces" in result.output
    assert "Filters: scores:name=accuracy" in result.output
    assert "fetched 2, skipped 1, inserted 1" in result.output


def test_sync_uses_the_configured_filters_unless_overridden(backends):
    _, warehouse = backends
    env = {**ENV, "SYNC_FILTERS": "scores:name=toxicity"}

    runner.invoke(app, ["sync", "-e", "scores"], env=env)
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s2"]

    warehouse.loaded.clear()
    runner.invoke(app, ["sync", "-e", "scores", "--filter", "scores:name=accuracy"], env=env)
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--from", "yesterday"], "not an ISO 8601 timestamp"),
        (["--sample-rate", "0"], "above 0 and at most 1"),
        (["--sample-rate", "2"], "above 0 and at most 1"),
        (["--filter", "just some words"], "cannot parse filter"),
        (["--filter", "prompts:name=x"], "unknown entity"),
    ],
)
def test_sync_rejects_malformed_options(backends, arguments, message):
    source, _ = backends

    result = runner.invoke(app, ["sync", *arguments], env=ENV)

    assert result.exit_code == 2
    assert message in " ".join(result.output.split())
    assert source.calls == []


def test_sync_exits_3_when_records_were_rejected(backends):
    _, warehouse = backends
    warehouse.reject = 1

    result = runner.invoke(app, ["sync", "-e", "scores"], env=ENV)

    assert result.exit_code == 3
    assert "rejected 1" in result.output


def test_failures_exit_1_with_a_single_line(backends):
    source, _ = backends

    def unavailable():
        raise RuntimeError("langfuse went away")

    source.get_project = unavailable

    result = runner.invoke(app, ["sync"], env=ENV)

    assert result.exit_code == 1
    assert "RuntimeError: langfuse went away" in result.output
    assert "Traceback" not in result.output


def test_check_init_and_status(backends):
    _, warehouse = backends

    check = runner.invoke(app, ["check"], env=ENV)
    assert check.exit_code == 0, check.output
    assert "Langfuse   ok  project demo (proj-1)" in check.output
    assert "Snowflake  ok  loader@acct role LOADER, DB.LANGFUSE on WH" in check.output

    init = runner.invoke(app, ["init"], env=ENV)
    assert init.exit_code == 0, init.output
    assert "table  LANGFUSE_OBSERVATIONS" in init.output
    assert "view   LANGFUSE_TRACES  (over LANGFUSE_OBSERVATIONS)" in init.output

    assert "Nothing has been synced yet." in runner.invoke(app, ["status"], env=ENV).output
    warehouse.watermarks["scores"] = NOW
    status = runner.invoke(app, ["status"], env=ENV)
    assert "scores: watermark 2026-03-10T12:00:00+00:00" in status.output
    assert "reconciled never" in status.output
    warehouse.reconciled["scores"] = NOW
    status = runner.invoke(app, ["status"], env=ENV)
    assert "reconciled 2026-03-10T12:00:00+00:00" in status.output


def test_sync_reports_snapshots_and_entities_the_project_cannot_serve(backends):
    source, warehouse = backends
    held = {"id": "gone", "objectType": "TRACE", "objectId": "t1", "content": "deleted since"}
    warehouse.seed(ENTITIES["comments"], [held])
    source.pages["/api/public/comments"] = [
        [{"id": "c1", "objectType": "TRACE", "objectId": "t1", "content": "new"}]
    ]
    source.errors["/api/public/annotation-queues"] = LangfuseApiError(
        "GET /api/public/annotation-queues failed: HTTP 403: not on this plan", status_code=403
    )

    result = runner.invoke(
        app,
        ["sync", "-e", "comments", "-e", "annotation_queues", "-e", "annotation_queue_items"],
        env=ENV,
    )

    # A feature the plan lacks is reported, but is not a failure.
    assert result.exit_code == 0, result.output
    assert (
        "LANGFUSE_COMMENTS: read in full, fetched 1, skipped 0, inserted 1, updated 0, "
        "rejected 0, deleted in Langfuse 1"
    ) in result.output
    assert "LANGFUSE_ANNOTATION_QUEUES: not available, skipped (GET" in result.output
    assert (
        "LANGFUSE_ANNOTATION_QUEUE_ITEMS: not available, skipped "
        "(annotation_queues could not be read)"
    ) in result.output


def _edited_score(backends) -> None:
    """A score held with 0.2 that Langfuse now reports as 0.9."""
    source, warehouse = backends
    held = {"id": "s1", "name": "accuracy", "timestamp": "2026-03-05T10:00:00.000Z", "value": 0.2}
    warehouse.seed(ENTITIES["scores"], [held])
    source.pages["/api/public/v3/scores"] = [[{**held, "value": 0.9}]]


def test_reconcile_prints_what_it_repaired(backends):
    _, warehouse = backends
    _edited_score(backends)

    result = runner.invoke(app, ["reconcile", "-e", "scores"], env=ENV)

    assert result.exit_code == 0, result.output
    assert warehouse.tables["scores"][("s1",)]["value"] == 0.9
    assert (
        "LANGFUSE_SCORES: reconciled 2026-03-05T10:00:00+00:00 to 2026-03-10T12:00:00+00:00 "
        "(full), compared 1, inserted 0, updated 1, rejected 0, deleted in Langfuse 0"
    ) in result.output
    # No incremental read happened, so no line for one.
    assert "fetched" not in result.output


def test_reconcile_with_a_range_and_json_output(backends):
    source, _ = backends
    _edited_score(backends)

    result = runner.invoke(
        app,
        ["reconcile", "-e", "scores", "--from", "2026-03-05", "--to", "2026-03-06", "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output)["entities"][0]["reconcile"]
    assert (outcome["mode"], outcome["rows_updated"]) == ("full", 1)
    assert source.calls == [
        (
            "/api/public/v3/scores",
            datetime(2026, 3, 5, tzinfo=UTC),
            datetime(2026, 3, 6, tzinfo=UTC),
        )
    ]


def test_reconcile_full_flag_and_setting(backends):
    source, warehouse = backends
    record = {
        "id": "o1",
        "traceId": "t1",
        "startTime": "2026-03-05T10:00:00.000Z",
        "updatedAt": "2026-03-05T10:00:01.000Z",
    }
    warehouse.seed(ENTITIES["observations"], [record])
    source.pages["/api/public/v2/observations"] = [[record]]
    arguments = ["reconcile", "-e", "observations"]

    assert "(listing)" in runner.invoke(app, arguments, env=ENV).output
    assert "(full)" in runner.invoke(app, [*arguments, "--full"], env=ENV).output
    assert "(full)" in runner.invoke(app, arguments, env={**ENV, "SYNC_RECONCILE_FULL": "1"}).output


def test_reconcile_with_nothing_held(backends):
    result = runner.invoke(app, ["reconcile", "-e", "scores"], env=ENV)

    assert result.exit_code == 0, result.output
    assert "LANGFUSE_SCORES: nothing to reconcile" in result.output


def test_reconcile_exits_3_when_records_were_rejected(backends):
    _, warehouse = backends
    _edited_score(backends)
    warehouse.reject = 1

    result = runner.invoke(app, ["reconcile", "-e", "scores"], env=ENV)

    assert result.exit_code == 3
    assert "rejected 1" in result.output


def test_sync_reports_the_reconcile_it_ran(backends):
    _, warehouse = backends
    _edited_score(backends)
    warehouse.watermarks["scores"] = datetime(2026, 3, 10, 9, tzinfo=UTC)

    result = runner.invoke(app, ["sync", "-e", "scores"], env=ENV)

    assert result.exit_code == 0, result.output
    assert "LANGFUSE_SCORES: fetched 0, skipped 0, inserted 0, updated 0" in result.output
    assert "(full), compared 1, inserted 0, updated 1" in result.output


def test_commands_use_what_was_saved_in_the_web_app(backends, tmp_path):
    _, warehouse = backends
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"version": 1, "overrides": {"filters": ["scores:name=toxicity"]}})
    )
    env = {**ENV, "SYNC_CONFIG_FILE": str(config_file)}

    result = runner.invoke(app, ["sync", "-e", "scores"], env=env)

    assert result.exit_code == 0, result.output
    assert "Filters: scores:name=toxicity" in result.output
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s2"]


def test_a_broken_config_file_is_explained(backends, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"version": 1, "overrides": {"sample_rate": 12}}))

    result = runner.invoke(app, ["sync"], env={**ENV, "SYNC_CONFIG_FILE": str(config_file)})

    assert result.exit_code == 2
    assert "config.json holds settings for default that are not valid" in result.output
    assert "sample_rate" in result.output and "Traceback" not in result.output


def test_serve_runs_the_app_on_the_saved_configuration(monkeypatch, tmp_path):
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: served.append(app))
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"version": 1, "overrides": {"schedule_minutes": 45}}))

    result = runner.invoke(app, ["serve"], env={**ENV, "SYNC_CONFIG_FILE": str(config_file)})

    assert result.exit_code == 0, result.output
    services = served[0].state.services
    assert services.deployment.store().path == config_file
    assert services.schedulers["default"].status().every_minutes == 45


def test_serve_refuses_a_public_interface_without_an_api_key(monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("server started"))

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env=ENV)

    assert result.exit_code == 2
    assert "Refusing to listen on 0.0.0.0 without an access key" in result.output


def test_serve_starts_on_loopback_or_with_an_api_key(monkeypatch):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: started.append((host, port)))

    assert runner.invoke(app, ["serve"], env=ENV).exit_code == 0
    secured = {**ENV, "SYNC_API_KEY": "s3cret"}
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9000"], env=secured)
    assert result.exit_code == 0

    assert started == [("127.0.0.1", 8000), ("0.0.0.0", 9000)]
