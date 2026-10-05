from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.sync import SyncService, iter_windows
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, 0, 0, 250_000, tzinfo=UTC)
NOW_SECONDS = NOW.replace(microsecond=0)

OBSERVATIONS = "/api/public/v2/observations"
SCORES = "/api/public/v3/scores"


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def make_service(langfuse_settings, source, warehouse, **sync):
    settings = SyncSettings(_env_file=None, **sync)
    return SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)


def test_iter_windows_covers_the_range_without_gaps_or_overlap():
    windows = list(iter_windows(at(1), at(3, 6), timedelta(hours=24)))

    assert windows == [(at(1), at(2)), (at(2), at(3)), (at(3), at(3, 6))]
    assert list(iter_windows(at(2), at(2), timedelta(hours=1))) == []


def test_first_run_backfills_and_sets_the_watermark(langfuse_settings):
    source = FakeSource(
        {
            OBSERVATIONS: [[{"id": "o1"}, {"id": "o2"}], [{"id": "o3"}]],
            SCORES: [[{"id": "s1"}]],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations,scores",
        initial_backfill_days=2,
        window_hours=24 * 30,
    )

    result = service.run()

    backfill_start = NOW_SECONDS - timedelta(days=2)
    assert source.calls == [
        (OBSERVATIONS, backfill_start, NOW_SECONDS),
        (SCORES, backfill_start, NOW_SECONDS),
    ]
    assert [row["id"] for row in warehouse.loaded["observations"]] == ["o1", "o2", "o3"]
    assert warehouse.watermarks == {"observations": NOW_SECONDS, "scores": NOW_SECONDS}

    observations, scores = result.entities
    assert (observations.table, observations.rows_fetched, observations.rows_inserted) == (
        "LANGFUSE_OBSERVATIONS",
        3,
        3,
    )
    assert observations.rows_skipped == 0
    assert (scores.rows_fetched, scores.watermark) == (1, NOW_SECONDS)
    assert (result.project_id, result.project_name, result.api_version) == ("proj-1", "demo", "v4")
    assert (result.sample_rate, result.filters) == (1.0, [])
    assert result.finished_at is not None


def test_incremental_run_rereads_the_lookback_behind_the_watermark(langfuse_settings):
    source = FakeSource()
    warehouse = FakeWarehouse({"scores": at(10, 9)})
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", lookback_minutes=90
    )

    service.run()

    assert source.calls == [(SCORES, at(10, 9) - timedelta(minutes=90), NOW_SECONDS)]
    assert warehouse.watermarks["scores"] == NOW_SECONDS


def test_watermark_is_checkpointed_per_window_and_never_moves_back(langfuse_settings):
    source = FakeSource()
    warehouse = FakeWarehouse({"scores": at(10, 10)})
    checkpoints = []
    warehouse.set_watermark = lambda project, entity, mark, version: checkpoints.append(mark)
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="scores",
        lookback_minutes=180,
        window_hours=1,
    )

    service.run()

    # Windows start at 07:00; those ending at or before the 10:00 watermark do not checkpoint.
    assert [call[1] for call in source.calls] == [at(10, hour) for hour in range(7, 12)]
    assert checkpoints == [at(10, 11), NOW_SECONDS]


def test_explicit_range_does_not_touch_the_watermark(langfuse_settings):
    source = FakeSource({SCORES: [[{"id": "s1"}]]})
    warehouse = FakeWarehouse({"scores": at(9)})
    service = make_service(langfuse_settings, source, warehouse, entities="scores")

    result = service.run(start=at(1), end=at(2))

    assert source.calls == [(SCORES, at(1), at(2))]
    assert warehouse.watermarks == {"scores": at(9)}
    assert result.entities[0].watermark == at(9)
    assert result.entities[0].rows_fetched == 1


def test_end_is_capped_at_now(langfuse_settings):
    source = FakeSource()
    warehouse = FakeWarehouse({"scores": at(10, 11)})
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", lookback_minutes=0
    )

    service.run(end=at(20))

    assert source.calls == [(SCORES, at(10, 11), NOW_SECONDS)]
    assert warehouse.watermarks["scores"] == NOW_SECONDS


def test_start_must_precede_end(langfuse_settings):
    service = make_service(langfuse_settings, FakeSource(), FakeWarehouse())

    with pytest.raises(ValueError, match="earlier"):
        service.run(start=at(2), end=at(1))


def test_requested_entities_override_the_configured_ones(langfuse_settings):
    source = FakeSource()
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="observations,scores")

    result = service.run(entities=["scores"])

    assert {call[0] for call in source.calls} == {SCORES}
    assert [item.entity for item in result.entities] == ["scores"]


def test_v4_traces_sync_observations_and_report_the_view(langfuse_settings):
    source = FakeSource()
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="traces,sessions")

    result = service.run()

    assert {call[0] for call in source.calls} == {OBSERVATIONS}
    assert [view.name for view in warehouse.plan.views] == ["traces", "sessions"]
    assert result.views == ["LANGFUSE_TRACES", "LANGFUSE_SESSIONS"]
    assert [item.entity for item in result.entities] == ["observations"]


def test_v3_syncs_all_four_entities_from_legacy_endpoints(langfuse_settings):
    source = FakeSource({"/api/public/traces": [[{"id": "t1"}]]})
    warehouse = FakeWarehouse()
    legacy = langfuse_settings.model_copy(update={"api_version": "v3"})
    service = make_service(
        legacy,
        source,
        warehouse,
        entities="observations,scores,traces,sessions",
        window_hours=24 * 30,
    )

    result = service.run()

    assert [call[0] for call in source.calls] == [
        "/api/public/observations",
        "/api/public/v2/scores",
        "/api/public/traces",
        "/api/public/sessions",
    ]
    assert warehouse.loaded["traces"] == [{"id": "t1"}]
    assert result.views == []
    assert result.api_version == "v3"


def test_a_failed_window_keeps_earlier_checkpoints(langfuse_settings):
    class FlakySource(FakeSource):
        def iter_pages(self, endpoint, start, end):
            if start >= at(8, 12):
                raise RuntimeError("langfuse went away")
            yield [{"id": start.isoformat()}]

    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        FlakySource(),
        warehouse,
        entities="scores",
        initial_backfill_days=3,
        window_hours=24,
    )

    with pytest.raises(RuntimeError, match="langfuse went away"):
        service.run()

    # Backfill started 03-07 12:00; the first window completed, the second failed.
    assert warehouse.watermarks == {"scores": at(8, 12)}
    assert len(warehouse.loaded["scores"]) == 1


def test_progress_is_reported_after_each_window(langfuse_settings):
    snapshots = []
    service = make_service(
        langfuse_settings,
        FakeSource({SCORES: [[{"id": "s1"}]]}),
        FakeWarehouse(),
        entities="scores",
        initial_backfill_days=2,
        window_hours=24,
    )

    service.run(progress=lambda result: snapshots.append(result.entities[0].rows_fetched))

    assert snapshots == [1, 2]


def test_rejected_rows_are_carried_into_the_result(langfuse_settings):
    warehouse = FakeWarehouse()
    warehouse.reject = 1
    service = make_service(
        langfuse_settings,
        FakeSource({SCORES: [[{"id": "s1"}, {"id": "s2"}]]}),
        warehouse,
        entities="scores",
        window_hours=24 * 60,
    )

    (scores,) = service.run().entities

    assert (scores.rows_fetched, scores.rows_inserted, scores.rows_rejected) == (2, 1, 1)


def test_init_and_state_and_check(langfuse_settings):
    warehouse = FakeWarehouse({"scores": at(5)})
    service = make_service(langfuse_settings, FakeSource(), warehouse)

    plan = service.init()
    assert warehouse.plan is plan

    (entry,) = service.state()
    assert (entry.entity, entry.watermark) == ("scores", at(5))

    project, session = service.check()
    assert project["id"] == "proj-1" and session["database"] == "DB"
