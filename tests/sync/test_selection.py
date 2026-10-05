"""Sampling and filters as applied by the sync service."""

from datetime import UTC, datetime

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.selection import is_sampled
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)

OBSERVATIONS = "/api/public/v2/observations"
SCORES = "/api/public/v3/scores"

TRACES = [f"trace-{number}" for number in range(100)]


def make_service(langfuse_settings, source, warehouse, **sync):
    # One window, so the fake source is read exactly once per entity.
    settings = SyncSettings(_env_file=None, window_hours=24 * 30, **sync)
    return SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)


def _source() -> FakeSource:
    observations = [
        {"id": f"{trace}-{part}", "traceId": trace, "environment": "production"}
        for trace in TRACES
        for part in ("root", "llm")
    ]
    scores = [
        {"id": f"score-{trace}", "name": "accuracy", "subject": {"kind": "trace", "id": trace}}
        for trace in TRACES
    ]
    scores.append({"id": "session-score", "subject": {"kind": "session", "id": "sess-1"}})
    return FakeSource({OBSERVATIONS: [observations], SCORES: [scores]})


def test_sampling_keeps_whole_traces_across_observations_and_scores(langfuse_settings):
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings, _source(), warehouse, entities="observations,scores", sample_rate=0.3
    )

    result = service.run()

    expected = {trace for trace in TRACES if is_sampled(trace, 0.3)}
    assert 15 < len(expected) < 45

    loaded = warehouse.loaded["observations"]
    assert {row["traceId"] for row in loaded} == expected
    # Both observations of every sampled trace made it.
    assert len(loaded) == 2 * len(expected)

    trace_scores = [row for row in warehouse.loaded["scores"] if row["id"] != "session-score"]
    assert {row["subject"]["id"] for row in trace_scores} == expected
    # A score that belongs to no trace is always kept.
    assert any(row["id"] == "session-score" for row in warehouse.loaded["scores"])

    observations, scores = result.entities
    assert (observations.rows_fetched, observations.rows_skipped) == (200, 200 - len(loaded))
    assert (scores.rows_fetched, scores.rows_skipped) == (101, 100 - len(expected))
    assert result.sample_rate == 0.3


def test_sampling_gives_the_same_traces_on_every_run(langfuse_settings):
    first, second = FakeWarehouse(), FakeWarehouse()

    for warehouse in (first, second):
        make_service(
            langfuse_settings, _source(), warehouse, entities="observations", sample_rate=0.3
        ).run()

    assert first.loaded == second.loaded


def test_filters_drop_records_and_are_sent_to_the_endpoint(langfuse_settings):
    source = FakeSource(
        {
            OBSERVATIONS: [
                [
                    {"id": "o1", "environment": "production", "level": "ERROR"},
                    {"id": "o2", "environment": "production", "level": "DEFAULT"},
                    # The fake ignores query parameters, as a misbehaving server would.
                    {"id": "o3", "environment": "dev", "level": "ERROR"},
                ]
            ],
            SCORES: [
                [
                    {"id": "s1", "environment": "production", "name": "accuracy"},
                    {"id": "s2", "environment": "dev", "name": "accuracy"},
                ]
            ],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations,scores",
        filters="environment=production;observations:level=ERROR",
    )

    result = service.run()

    assert [row["id"] for row in warehouse.loaded["observations"]] == ["o1"]
    # The observation-only filter does not touch scores.
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1"]

    observations_endpoint, scores_endpoint = source.endpoints
    assert observations_endpoint.params["environment"] == ["production"]
    assert observations_endpoint.params["level"] == "ERROR"
    assert scores_endpoint.params["environment"] == "production"
    assert "level" not in scores_endpoint.params

    assert [(item.rows_fetched, item.rows_skipped) for item in result.entities] == [(3, 2), (2, 1)]
    assert result.filters == ["environment=production", "observations:level=ERROR"]


def test_filters_and_sampling_combine(langfuse_settings):
    source = _source()
    source.pages[OBSERVATIONS][0][0]["environment"] = "dev"
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations",
        sample_rate=0.5,
        filters="environment=production",
    )

    service.run()

    loaded = warehouse.loaded["observations"]
    assert all(row["environment"] == "production" for row in loaded)
    assert all(is_sampled(row["traceId"], 0.5) for row in loaded)


def test_a_run_can_override_the_configured_selection(langfuse_settings):
    source = FakeSource(
        {SCORES: [[{"id": "s1", "name": "accuracy"}, {"id": "s2", "name": "toxicity"}]]}
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", filters="scores:name=accuracy"
    )

    result = service.run(filters=["scores:name=toxicity"], sample_rate=1.0)
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s2"]
    assert result.filters == ["scores:name=toxicity"]

    # An empty list switches the configured filters off for the run.
    warehouse.loaded.clear()
    service.run(filters=[])
    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1", "s2"]


def test_filters_on_entities_outside_the_run_are_ignored(langfuse_settings):
    source = FakeSource({SCORES: [[{"id": "s1"}]]})
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", filters="observations:level=ERROR"
    )

    service.run()

    assert [row["id"] for row in warehouse.loaded["scores"]] == ["s1"]


def test_a_filter_that_removes_everything_is_pointed_out(langfuse_settings, caplog):
    # "type" exists on observations but not on scores, and the filter is not scoped.
    source = FakeSource({SCORES: [[{"id": "s1"}, {"id": "s2"}]]})
    service = make_service(
        langfuse_settings, source, FakeWarehouse(), entities="scores", filters="type=GENERATION"
    )

    with caplog.at_level("WARNING"):
        service.run()

    assert "scores: all 2 fetched record(s) were filtered out by type=GENERATION" in caplog.text


def test_v4_rejects_a_filter_on_traces_before_doing_any_work(langfuse_settings):
    source, warehouse = FakeSource(), FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, filters="traces:userId=alice")

    with pytest.raises(ValueError, match="traces are derived from observations"):
        service.run()

    assert source.calls == [] and warehouse.plan is None


def test_v3_filters_traces_directly(langfuse_settings):
    source = FakeSource(
        {"/api/public/traces": [[{"id": "t1", "userId": "alice"}, {"id": "t2", "userId": "bob"}]]}
    )
    warehouse = FakeWarehouse()
    legacy = langfuse_settings.model_copy(update={"api_version": "v3"})
    service = make_service(
        legacy, source, warehouse, entities="traces", filters="traces:userId=alice"
    )

    service.run()

    assert [row["id"] for row in warehouse.loaded["traces"]] == ["t1"]
    assert source.endpoints[0].params == {"userId": "alice"}


def test_an_invalid_override_is_rejected(langfuse_settings):
    service = make_service(langfuse_settings, FakeSource(), FakeWarehouse())

    with pytest.raises(ValueError, match="sample rate"):
        service.run(sample_rate=0)
    with pytest.raises(ValueError, match="cannot parse filter"):
        service.run(filters=["nonsense"])
