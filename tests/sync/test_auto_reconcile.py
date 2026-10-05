"""An incremental sync reconciles older data by itself once the interval has passed."""

from datetime import UTC, datetime, timedelta

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)
WATERMARK = datetime(2026, 3, 10, 9, tzinfo=UTC)
# The incremental read starts the default 180 minutes behind the watermark.
INCREMENTAL_START = WATERMARK - timedelta(minutes=180)

SCORES = "/api/public/v3/scores"
OLD = "2026-03-05T10:00:00.000Z"
OLD_AT = datetime(2026, 3, 5, 10, tzinfo=UTC)


def _score(value: float) -> dict:
    return {"id": "s1", "timestamp": OLD, "value": value, "subject": {"kind": "trace", "id": "t1"}}


def _setup(langfuse_settings, reconciled=None, **sync):
    """A score loaded five days ago with 0.2 that a reviewer has since changed to 0.9."""
    warehouse = FakeWarehouse({"scores": WATERMARK}, reconciled)
    warehouse.seed(ENTITIES["scores"], [_score(0.2)])
    source = FakeSource({SCORES: [[_score(0.9)]]})
    settings = SyncSettings(_env_file=None, entities="scores", window_hours=24 * 40, **sync)
    service = SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)
    return service, source, warehouse


def test_a_sync_that_has_never_reconciled_does_so(langfuse_settings):
    service, source, warehouse = _setup(langfuse_settings)

    result = service.run()

    # The incremental read, then everything older, back to the oldest record held.
    assert source.calls == [
        (SCORES, INCREMENTAL_START, NOW),
        (SCORES, OLD_AT, INCREMENTAL_START),
    ]
    assert warehouse.tables["scores"][("s1",)]["value"] == 0.9
    assert warehouse.reconciled == {"scores": NOW}

    entity = result.entities[0]
    assert (entity.rows_fetched, entity.rows_updated) == (0, 0)
    outcome = entity.reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_updated) == ("full", 1, 1)
    assert (outcome.window_start, outcome.window_end) == (OLD_AT, INCREMENTAL_START)
    # Reconciling leaves the watermark to the incremental part.
    assert entity.watermark == NOW


def test_a_recent_reconcile_is_not_repeated(langfuse_settings):
    service, source, warehouse = _setup(
        langfuse_settings, reconciled={"scores": NOW - timedelta(hours=23)}
    )

    result = service.run()

    assert source.calls == [(SCORES, INCREMENTAL_START, NOW)]
    assert warehouse.tables["scores"][("s1",)]["value"] == 0.2
    assert result.entities[0].reconcile is None
    assert warehouse.reconciled == {"scores": NOW - timedelta(hours=23)}


def test_it_is_repeated_once_the_interval_has_passed(langfuse_settings):
    service, source, warehouse = _setup(
        langfuse_settings, reconciled={"scores": NOW - timedelta(hours=24)}
    )

    service.run()

    assert len(source.calls) == 2
    assert warehouse.tables["scores"][("s1",)]["value"] == 0.9
    assert warehouse.reconciled == {"scores": NOW}


def test_the_interval_is_configurable(langfuse_settings):
    service, source, _ = _setup(
        langfuse_settings, reconciled={"scores": NOW - timedelta(hours=2)}, reconcile_every_hours=1
    )

    service.run()

    assert len(source.calls) == 2


def test_it_can_be_turned_off(langfuse_settings):
    service, source, warehouse = _setup(langfuse_settings, reconcile_every_hours=0)

    service.run()

    assert source.calls == [(SCORES, INCREMENTAL_START, NOW)]
    assert warehouse.reconciled == {}


def test_one_off_runs_do_not_reconcile(langfuse_settings):
    service, source, warehouse = _setup(langfuse_settings)

    service.run(start=datetime(2026, 3, 9, tzinfo=UTC), end=datetime(2026, 3, 10, tzinfo=UTC))
    service.run(filters=[])
    service.run(sample_rate=0.5)

    assert all(call[1] != OLD_AT for call in source.calls)
    assert warehouse.reconciled == {}


def test_the_reconcile_window_respects_the_configured_days(langfuse_settings):
    service, source, _ = _setup(langfuse_settings, reconcile_days=2)

    service.run()

    # The five-day-old score is outside a two-day window.
    assert source.calls[1] == (SCORES, NOW - timedelta(days=2), INCREMENTAL_START)


def test_the_first_sync_has_nothing_older_to_reconcile(langfuse_settings):
    warehouse = FakeWarehouse()
    recent = {"id": "s1", "timestamp": "2026-03-09T10:00:00.000Z", "value": 0.5}
    source = FakeSource({SCORES: [[recent]]})
    settings = SyncSettings(_env_file=None, entities="scores", window_hours=24 * 40)
    service = SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)

    result = service.run()

    # Only the backfill itself; but the clock for the next reconcile starts now.
    assert source.calls == [(SCORES, NOW - timedelta(days=30), NOW)]
    assert result.entities[0].reconcile is None
    assert warehouse.reconciled == {"scores": NOW}


def test_full_mode_setting_applies(langfuse_settings):
    observations = "/api/public/v2/observations"
    record = {
        "id": "o1",
        "traceId": "t1",
        "startTime": OLD,
        "updatedAt": OLD,
        "environment": "production",
    }
    warehouse = FakeWarehouse({"observations": WATERMARK})
    warehouse.seed(ENTITIES["observations"], [record])
    source = FakeSource({observations: [[record]]})
    settings = SyncSettings(
        _env_file=None, entities="observations", window_hours=24 * 40, reconcile_full=True
    )
    service = SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)

    result = service.run()

    assert result.entities[0].reconcile.mode == "full"
