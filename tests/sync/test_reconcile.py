"""Reconciling a range that was already synced."""

from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)

OBSERVATIONS = "/api/public/v2/observations"
SCORES = "/api/public/v3/scores"
LISTING_FIELDS = "core,basic,time,trace_context"


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def stamp(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-03-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def observation(name: str, start: str, updated: str, trace: str | None = None, **extra) -> dict:
    return {
        "id": name,
        "traceId": trace or f"trace-of-{name}",
        "startTime": start,
        "updatedAt": updated,
        "environment": "production",
        **extra,
    }


def score(score_id: str, timestamp: str, value: float, **extra) -> dict:
    subject = {"kind": "trace", "id": "t1"}
    return {"id": score_id, "timestamp": timestamp, "value": value, "subject": subject, **extra}


def make_service(langfuse_settings, source, warehouse, **sync):
    settings = SyncSettings(_env_file=None, **sync)
    return SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)


def test_an_edited_score_is_updated(langfuse_settings):
    """The case reconciliation exists for: a reviewer changes a score days after it was loaded."""
    loaded = score("s1", stamp(5, 10), 0.2, comment="first pass")
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["scores"], [loaded])
    edited = {**loaded, "value": 0.9, "comment": "corrected on review"}
    service = make_service(
        langfuse_settings, FakeSource({SCORES: [[edited]]}), warehouse, entities="scores"
    )

    result = service.reconcile(start=at(5), end=at(6))

    assert warehouse.tables["scores"][("s1",)] == edited
    outcome = result.entities[0].reconcile
    # Scores are always re-read in full, so an edit is found by its content.
    assert outcome.mode == "full"
    assert (outcome.rows_compared, outcome.rows_inserted, outcome.rows_updated) == (1, 0, 1)
    assert outcome.rows_deleted_upstream == 0


def test_an_edit_that_leaves_updated_at_alone_is_still_found(langfuse_settings):
    loaded = score("s1", stamp(5, 10), 0.2, updatedAt=stamp(5, 10))
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["scores"], [loaded])
    edited = {**loaded, "value": 0.9}  # same updatedAt
    service = make_service(
        langfuse_settings, FakeSource({SCORES: [[edited]]}), warehouse, entities="scores"
    )

    service.reconcile(start=at(5), end=at(6))

    assert warehouse.tables["scores"][("s1",)]["value"] == 0.9


def test_missing_scores_are_loaded_and_deleted_ones_reported(langfuse_settings, caplog):
    kept = score("s1", stamp(5, 10), 0.5)
    gone = score("s2", stamp(5, 11), 0.5)
    late = score("s3", stamp(5, 12), 0.5)
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["scores"], [kept, gone])
    service = make_service(
        langfuse_settings, FakeSource({SCORES: [[kept, late]]}), warehouse, entities="scores"
    )

    with caplog.at_level("WARNING"):
        result = service.reconcile(start=at(5), end=at(6))

    outcome = result.entities[0].reconcile
    assert (outcome.rows_inserted, outcome.rows_updated, outcome.rows_deleted_upstream) == (1, 0, 1)
    # A deleted record is reported, never removed.
    assert set(warehouse.tables["scores"]) == {("s1",), ("s2",), ("s3",)}
    assert "scores: 1 row(s) in Snowflake no longer exist in Langfuse" in caplog.text
    assert "e.g. s2" in caplog.text


def test_observations_are_compared_by_listing_and_only_changed_hours_are_reread(
    langfuse_settings,
):
    current = observation("o1", stamp(5, 1, 10), stamp(5, 1, 11))
    changed = observation("o2", stamp(5, 5, 20), stamp(5, 9, 0), output="final")
    missing = observation("o3", stamp(5, 5, 40), stamp(5, 5, 41))
    late = observation("o5", stamp(5, 14, 0), stamp(5, 14, 1))
    deleted = observation("o4", stamp(5, 3, 0), stamp(5, 3, 1))
    source = FakeSource({OBSERVATIONS: [[current, changed, missing, late]]})
    warehouse = FakeWarehouse()
    stale = {**changed, "updatedAt": stamp(5, 5, 21), "output": "partial"}
    warehouse.seed(ENTITIES["observations"], [current, stale, deleted])
    service = make_service(langfuse_settings, source, warehouse, entities="observations")

    result = service.reconcile(start=at(5), end=at(6))

    listing, *rereads = source.endpoints
    assert listing.params == {"fields": LISTING_FIELDS}
    assert all("io" in endpoint.params["fields"] for endpoint in rereads)
    # One listing of the day, then only the two hours that hold a difference.
    assert source.calls == [
        (OBSERVATIONS, at(5), at(6)),
        (OBSERVATIONS, at(5, 5), at(5, 6)),
        (OBSERVATIONS, at(5, 14), at(5, 15)),
    ]

    table = warehouse.tables["observations"]
    assert table[("trace-of-o2", "o2")]["output"] == "final"
    assert ("trace-of-o3", "o3") in table and ("trace-of-o5", "o5") in table
    assert ("trace-of-o4", "o4") in table  # reported, not removed

    outcome = result.entities[0].reconcile
    assert outcome.mode == "listing"
    assert (outcome.rows_compared, outcome.rows_inserted, outcome.rows_updated) == (4, 2, 1)
    assert outcome.rows_deleted_upstream == 1


def test_nothing_is_reread_when_the_listing_matches(langfuse_settings):
    held = [
        observation("o1", stamp(5, 1), stamp(5, 2)),
        observation("o2", stamp(5, 7), stamp(5, 8)),
    ]
    source = FakeSource({OBSERVATIONS: [held]})
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["observations"], held)
    service = make_service(langfuse_settings, source, warehouse, entities="observations")

    result = service.reconcile(start=at(5), end=at(6))

    assert len(source.calls) == 1
    assert warehouse.loaded == {}
    outcome = result.entities[0].reconcile
    assert (outcome.rows_compared, outcome.rows_inserted, outcome.rows_updated) == (2, 0, 0)


def test_neighbouring_hours_are_reread_as_one_range(langfuse_settings):
    missing = [
        observation(f"o{hour}", stamp(5, hour, 10), stamp(5, hour, 11)) for hour in (5, 6, 7)
    ]
    missing.append(observation("o20", stamp(5, 20, 59), stamp(5, 21, 0)))
    source = FakeSource({OBSERVATIONS: [missing]})
    service = make_service(langfuse_settings, source, FakeWarehouse(), entities="observations")

    service.reconcile(start=at(5), end=at(6))

    assert source.calls[1:] == [
        (OBSERVATIONS, at(5, 5), at(5, 8)),
        (OBSERVATIONS, at(5, 20), at(5, 21)),
    ]


def test_each_window_is_compared_on_its_own(langfuse_settings):
    records = [observation(f"o{day}", stamp(day, 9), stamp(day, 10)) for day in (5, 6, 7)]
    source = FakeSource({OBSERVATIONS: [records]})
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["observations"], [records[0], records[2]])
    snapshots = []
    service = make_service(langfuse_settings, source, warehouse, entities="observations")

    service.reconcile(
        start=at(5),
        end=at(8),
        progress=lambda result: snapshots.append(result.entities[0].reconcile.rows_compared),
    )

    # Three daily listings; only the second day needs a re-read.
    assert source.calls == [
        (OBSERVATIONS, at(5), at(6)),
        (OBSERVATIONS, at(6), at(7)),
        (OBSERVATIONS, at(6, 9), at(6, 10)),
        (OBSERVATIONS, at(7), at(8)),
    ]
    assert snapshots == [1, 2, 3]


def test_sampling_decides_what_counts_as_missing_but_not_what_counts_as_deleted(
    langfuse_settings,
):
    # At a rate of 0.5, trace-b is in the sample and trace-a is not.
    unsampled = observation("oa", stamp(5, 3), stamp(5, 4), trace="trace-a")
    sampled = observation("ob", stamp(5, 6), stamp(5, 7), trace="trace-b")
    held_before_sampling = observation("oz", stamp(5, 9), stamp(5, 10), trace="trace-a")
    source = FakeSource({OBSERVATIONS: [[unsampled, sampled, held_before_sampling]]})
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["observations"], [held_before_sampling])
    service = make_service(
        langfuse_settings, source, warehouse, entities="observations", sample_rate=0.5
    )

    result = service.reconcile(start=at(5), end=at(6))

    assert [row["id"] for row in warehouse.loaded["observations"]] == ["ob"]
    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_inserted) == ("listing", 1)
    # Still in Langfuse, just outside today's sample: not a deletion.
    assert outcome.rows_deleted_upstream == 0


def test_filters_the_listing_can_answer_are_applied_to_it(langfuse_settings):
    production = observation("o1", stamp(5, 3), stamp(5, 4))
    development = observation("o2", stamp(5, 6), stamp(5, 7), environment="dev")
    source = FakeSource({OBSERVATIONS: [[production, development]]})
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations",
        filters="environment=production",
    )

    # Held from before the filter was set: one still in Langfuse, one deleted there.
    held_dev = observation("o3", stamp(5, 9), stamp(5, 10), environment="dev")
    deleted = observation("o4", stamp(5, 12), stamp(5, 13), environment="dev")
    source.pages[OBSERVATIONS][0].append(held_dev)
    warehouse.seed(ENTITIES["observations"], [held_dev, deleted])

    result = service.reconcile(start=at(5), end=at(6))

    # The listing is taken unfiltered and filtered here, so that it also shows
    # which rows are really gone rather than merely filtered out.
    listing = source.endpoints[0]
    assert listing.params == {"fields": LISTING_FIELDS}
    assert [row["id"] for row in warehouse.loaded["observations"]] == ["o1"]
    assert source.calls[1:] == [(OBSERVATIONS, at(5, 3), at(5, 4))]
    # The re-read of the full records is filtered at the source as usual.
    assert source.endpoints[1].params["environment"] == ["production"]
    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_inserted) == ("listing", 3, 1)
    assert outcome.rows_deleted_upstream == 1


def test_deletions_are_audited_separately_when_a_filtered_read_is_needed(langfuse_settings):
    kept = score("s1", stamp(5, 10), 0.5, name="accuracy")
    filtered_out = score("s2", stamp(5, 11), 0.5, name="toxicity")
    deleted = score("s3", stamp(5, 12), 0.5, name="accuracy")
    source = FakeSource({SCORES: [[kept, filtered_out]]})
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["scores"], [kept, filtered_out, deleted])
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", filters="scores:name=accuracy"
    )

    result = service.reconcile(start=at(5), end=at(6))

    # The read of the full records is filtered at the source; a second, bare
    # request then lists every key so that only s3 counts as deleted.
    read, audit = source.endpoints
    assert read.params == {"fields": "details,subject,annotation", "name": "accuracy"}
    assert audit.params == {}
    assert source.calls == [(SCORES, at(5), at(6)), (SCORES, at(5), at(6))]
    outcome = result.entities[0].reconcile
    assert (outcome.mode, outcome.rows_compared, outcome.rows_deleted_upstream) == ("full", 2, 1)


def test_the_audit_of_observations_asks_for_the_core_fields_only(langfuse_settings):
    record = observation("o1", stamp(5, 3), stamp(5, 4), totalCost=0.5)
    source = FakeSource({OBSERVATIONS: [[record]]})
    service = make_service(
        langfuse_settings,
        source,
        FakeWarehouse(),
        entities="observations",
        filters="observations:totalCost>0",
    )

    service.reconcile(start=at(5), end=at(6))

    assert source.endpoints[1].params == {"fields": "core"}


def test_the_deletion_check_can_be_turned_off(langfuse_settings):
    record = observation("o1", stamp(5, 3), stamp(5, 4))
    source = FakeSource(
        {OBSERVATIONS: [[record]], SCORES: [[score("s1", stamp(5, 10), 0.5, name="accuracy")]]}
    )
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["observations"], [observation("gone", stamp(5, 8), stamp(5, 9))])
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations,scores",
        filters="environment=production;scores:name=accuracy",
        check_deletions=False,
    )

    result = service.reconcile(start=at(5), end=at(6))

    observations, scores = (item.reconcile for item in result.entities)
    assert observations.rows_deleted_upstream is None and scores.rows_deleted_upstream is None
    # Without the audit the listing is filtered at the source, and there is no second pass.
    assert source.endpoints[0].params == {"fields": LISTING_FIELDS, "environment": ["production"]}
    assert [call[0] for call in source.calls] == [OBSERVATIONS, OBSERVATIONS, SCORES]


@pytest.mark.parametrize(
    ("sync", "fields"),
    [
        # The listing has no cost or metadata to filter on.
        ({"filters": "observations:totalCost>0"}, None),
        ({"filters": "observations:metadata.tier=gold"}, None),
        ({"reconcile_full": True}, None),
        # Without the time group the stored rows have no updatedAt to compare.
        ({}, ("core", "basic", "trace_context")),
    ],
)
def test_observations_fall_back_to_a_full_reread(langfuse_settings, sync, fields):
    if fields:
        langfuse_settings = langfuse_settings.model_copy(update={"observation_fields": fields})
    record = observation("o1", stamp(5, 3), stamp(5, 4), totalCost=0.5, metadata={"tier": "gold"})
    source = FakeSource({OBSERVATIONS: [[record]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="observations", **sync)

    result = service.reconcile(start=at(5), end=at(6))

    assert result.entities[0].reconcile.mode == "full"
    # The whole day is read with the fields that are synced, not the lighter listing.
    assert source.calls[0] == (OBSERVATIONS, at(5), at(6))
    assert source.endpoints[0].params["fields"] != LISTING_FIELDS
    assert [row["id"] for row in warehouse.loaded["observations"]] == ["o1"]


def test_full_can_be_requested_for_one_run(langfuse_settings):
    source = FakeSource({OBSERVATIONS: [[observation("o1", stamp(5, 3), stamp(5, 4))]]})
    service = make_service(langfuse_settings, source, FakeWarehouse(), entities="observations")

    assert service.reconcile(start=at(5), end=at(6), full=True).entities[0].reconcile.mode == "full"
    assert service.reconcile(start=at(5), end=at(6)).entities[0].reconcile.mode == "listing"


def test_legacy_endpoints_are_always_reread_in_full(langfuse_settings):
    legacy = langfuse_settings.model_copy(update={"api_version": "v3"})
    source = FakeSource()
    service = make_service(
        legacy, source, FakeWarehouse(), entities="observations,scores,traces,sessions"
    )

    result = service.reconcile(start=at(5), end=at(6))

    assert [item.reconcile.mode for item in result.entities] == ["full"] * 4
    assert [call[0] for call in source.calls] == [
        "/api/public/observations",
        "/api/public/v2/scores",
        "/api/public/traces",
        "/api/public/sessions",
    ]


def test_the_default_window_starts_at_the_oldest_record_held(langfuse_settings):
    warehouse = FakeWarehouse({"scores": at(10, 9)})
    warehouse.seed(ENTITIES["scores"], [score("s1", stamp(3, 8), 0.5)])
    source = FakeSource()
    service = make_service(
        langfuse_settings, source, warehouse, entities="scores", window_hours=24 * 40
    )

    result = service.reconcile()

    # 30 days back would be February; nothing that old was ever synced.
    assert source.calls == [(SCORES, at(3, 8), NOW)]
    assert result.entities[0].window_start is None
    # A whole-window reconcile is recorded; the watermark is not its business.
    assert warehouse.reconciled == {"scores": NOW}
    assert warehouse.watermarks == {"scores": at(10, 9)}


def test_the_default_window_is_limited_to_the_configured_days(langfuse_settings):
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["scores"], [score("s1", stamp(1, 0), 0.5)])
    source = FakeSource()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="scores",
        reconcile_days=3,
        window_hours=24 * 40,
    )

    service.reconcile()

    assert source.calls == [(SCORES, NOW - timedelta(days=3), NOW)]


def test_nothing_held_means_nothing_to_reconcile(langfuse_settings):
    source = FakeSource({SCORES: [[score("s1", stamp(5, 10), 0.5)]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="scores")

    result = service.reconcile()

    # Reconciling must not turn into a backfill of data that was never synced.
    assert source.calls == []
    assert result.entities[0].reconcile is None


def test_an_explicit_range_is_used_as_given_and_not_recorded(langfuse_settings):
    warehouse = FakeWarehouse({"scores": at(10, 9)})
    source = FakeSource({SCORES: [[score("s1", stamp(5, 10), 0.5)]]})
    service = make_service(langfuse_settings, source, warehouse, entities="scores")

    result = service.reconcile(start=at(5), end=at(6))

    assert source.calls == [(SCORES, at(5), at(6))]
    assert result.entities[0].reconcile.rows_inserted == 1
    assert warehouse.reconciled == {}


def test_the_range_must_be_ordered(langfuse_settings):
    service = make_service(langfuse_settings, FakeSource(), FakeWarehouse())

    with pytest.raises(ValueError, match="earlier"):
        service.reconcile(start=at(6), end=at(5))
