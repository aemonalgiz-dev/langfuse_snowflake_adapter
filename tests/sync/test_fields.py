"""Leaving fields out of what is loaded, and seeing which fields there are."""

from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.langfuse import LangfuseApiError
from langfuse_to_snowflake.sync import SyncService, describe
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)

OBSERVATIONS = "/api/public/v2/observations"
SCORES = "/api/public/v3/scores"
COMMENTS = "/api/public/comments"
QUEUES = "/api/public/annotation-queues"


def make_service(langfuse_settings, source, warehouse=None, **sync):
    settings = SyncSettings(_env_file=None, window_hours=24 * 40, **sync)
    return SyncService(
        langfuse_settings, settings, source, warehouse or FakeWarehouse(), clock=lambda: NOW
    )


def observation(name: str, **extra) -> dict:
    return {
        "id": name,
        "traceId": "t1",
        "startTime": "2026-03-09T10:00:00.000Z",
        "environment": "production",
        **extra,
    }


def test_excluded_fields_never_reach_the_warehouse(langfuse_settings):
    source = FakeSource(
        {
            OBSERVATIONS: [
                [
                    observation(
                        "o1",
                        input="the prompt",
                        output="the answer",
                        metadata={"email": "a@b.c", "tier": "gold"},
                    )
                ]
            ],
            SCORES: [[{"id": "s1", "comment": "private note", "metadata": {"tier": "gold"}}]],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations,scores",
        exclude_fields="observations:input,observations:metadata.email,scores:comment",
    )

    result = service.run()

    (loaded,) = warehouse.loaded["observations"]
    assert loaded == observation("o1", output="the answer", metadata={"tier": "gold"})
    assert warehouse.loaded["scores"] == [{"id": "s1", "metadata": {"tier": "gold"}}]
    assert result.excluded_fields == [
        "observations:input",
        "observations:metadata.email",
        "scores:comment",
    ]
    # The source's own records are untouched.
    assert source.pages[OBSERVATIONS][0][0]["input"] == "the prompt"


def test_a_filter_can_use_a_field_that_is_then_left_out(langfuse_settings):
    source = FakeSource(
        {
            OBSERVATIONS: [
                [
                    observation("o1", metadata={"tier": "gold", "note": "x"}),
                    observation("o2", metadata={"tier": "free", "note": "y"}),
                ]
            ]
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations",
        filters="observations:metadata.tier=gold",
        exclude_fields="observations:metadata",
    )

    service.run()

    assert warehouse.loaded["observations"] == [observation("o1")]


def test_exclusions_apply_when_reconciling_too(langfuse_settings):
    edited = {"id": "s1", "timestamp": "2026-03-05T10:00:00.000Z", "value": 0.9, "comment": "why"}
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        FakeSource({SCORES: [[edited]]}),
        warehouse,
        entities="scores",
        exclude_fields="scores:comment",
    )

    service.reconcile(start=datetime(2026, 3, 5, tzinfo=UTC), end=datetime(2026, 3, 6, tzinfo=UTC))

    assert "comment" not in warehouse.tables["scores"][("s1",)]


def test_describe_lists_fields_with_their_types_and_how_often_they_have_a_value():
    total, fields = describe(
        [
            {"id": "a", "cost": 0.5, "metadata": {"tier": "gold", "seats": 3}, "tags": ["x"]},
            {"id": "b", "cost": None, "metadata": {"tier": 7}, "public": False},
            {"id": "c", "metadata": None},
            {"id": "d", "metadata": {"deep": {"er": {"and": {"deeper": 1}}}}},
        ]
    )

    assert total == 4
    assert fields["id"] == (["text"], 1.0)
    assert fields["cost"] == (["number"], 0.25)
    assert fields["metadata"] == (["object"], 0.75)
    # Nested keys get dotted paths; a key can hold different types across records.
    assert fields["metadata.tier"] == (["number", "text"], 0.5)
    assert fields["metadata.seats"] == (["number"], 0.25)
    assert fields["tags"] == (["list"], 0.25)
    assert fields["public"] == (["boolean"], 0.25)
    # Nesting is followed a few levels, not without end.
    assert "metadata.deep.er.and" in fields and "metadata.deep.er.and.deeper" not in fields


def test_describe_of_nothing():
    assert describe([]) == (0, {})


def test_discover_shows_what_a_project_logs_whatever_the_settings_keep(langfuse_settings):
    source = FakeSource(
        {
            OBSERVATIONS: [
                [
                    observation("o1", input="prompt", metadata={"email": "a@b.c", "tier": "gold"}),
                    observation("o2", environment="dev", metadata={"tier": "free"}),
                ]
            ],
            SCORES: [[{"id": "s1", "timestamp": "2026-03-09T10:00:00.000Z", "value": 1}]],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="observations,scores,traces",
        # Neither the filter nor the sampling hides anything from the view.
        filters="environment=production",
        sample_rate=0.01,
        exclude_fields="observations:metadata.email,observations:output",
    )

    observations, scores = service.discover()

    assert (observations.entity, observations.table, observations.sampled) == (
        "observations",
        "LANGFUSE_OBSERVATIONS",
        2,
    )
    fields = {field.path: field for field in observations.fields}
    assert list(fields) == sorted(fields)
    assert fields["id"].required and fields["traceId"].required and fields["startTime"].required
    assert not fields["environment"].required
    assert (fields["environment"].column, fields["environment"].share) == ("ENVIRONMENT", 1.0)
    assert (fields["metadata.tier"].types, fields["metadata.tier"].column) == (["text"], None)
    assert (fields["metadata.email"].excluded, fields["metadata.email"].share) == (True, 0.5)
    assert not fields["metadata.tier"].excluded and not fields["input"].excluded
    # Left out by the settings but not in this sample: still listed, so it can be brought back.
    assert (fields["output"].excluded, fields["output"].types, fields["output"].share) == (
        True,
        [],
        0.0,
    )
    assert [field.path for field in scores.fields] == ["id", "timestamp", "value"]
    # Nothing was written anywhere.
    assert warehouse.loaded == {} and warehouse.plan is None


def test_discover_reads_only_a_sample_of_the_newest_records(langfuse_settings):
    pages = [[observation(f"o{page}-{row}") for row in range(50)] for page in range(10)]
    source = FakeSource({OBSERVATIONS: pages})
    service = make_service(langfuse_settings, source, entities="observations")

    (schema,) = service.discover(sample=120, days=3)

    assert schema.sampled == 120
    path, start, end = source.calls[0]
    assert (path, start, end) == (OBSERVATIONS, NOW - timedelta(days=3), NOW)
    # No more is asked for per request than the sample needs, with every field group.
    assert source.endpoints[0].max_limit == 120
    assert "io" in source.endpoints[0].params["fields"]


def test_discover_follows_queues_to_their_items(langfuse_settings):
    source = FakeSource(
        {
            QUEUES: [[{"id": "q1", "name": "weekly"}]],
            "/api/public/annotation-queues/q1/items": [
                [{"id": "i1", "queueId": "q1", "status": "PENDING"}]
            ],
        }
    )
    service = make_service(langfuse_settings, source, entities="annotation_queue_items")

    queues, items = service.discover()

    assert [field.path for field in queues.fields] == ["id", "name"]
    assert (items.sampled, [field.path for field in items.fields]) == (
        1,
        ["id", "queueId", "status"],
    )


def test_discover_reports_entities_the_project_cannot_serve(langfuse_settings):
    source = FakeSource({SCORES: [[{"id": "s1"}]]})
    source.errors[COMMENTS] = LangfuseApiError("GET comments failed: HTTP 403", status_code=403)
    service = make_service(langfuse_settings, source, entities="scores,comments")

    scores, comments = service.discover()

    assert scores.sampled == 1
    assert "HTTP 403" in comments.unavailable and comments.fields == []


def test_discover_fails_when_langfuse_itself_cannot_be_read(langfuse_settings):
    source = FakeSource()
    source.errors[SCORES] = LangfuseApiError("GET scores failed: HTTP 401", status_code=401)
    service = make_service(langfuse_settings, source, entities="scores")

    with pytest.raises(LangfuseApiError):
        service.discover()


def test_discover_of_an_empty_project(langfuse_settings):
    service = make_service(langfuse_settings, FakeSource(), entities="observations")

    (schema,) = service.discover()

    assert (schema.sampled, schema.fields) == (0, [])
