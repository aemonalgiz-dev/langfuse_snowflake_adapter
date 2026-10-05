"""Entities Langfuse cannot filter by time: comments, annotation queues and their items."""

from datetime import UTC, datetime

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.langfuse import LangfuseApiError
from langfuse_to_snowflake.sync import SyncService
from tests.fakes import FakeSource, FakeWarehouse

NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)

COMMENTS = "/api/public/comments"
QUEUES = "/api/public/annotation-queues"
SCORES = "/api/public/v3/scores"


def items_of(queue: str) -> str:
    return f"/api/public/annotation-queues/{queue}/items"


def comment(name: str, content: str, on: str = "TRACE", target: str = "t1") -> dict:
    return {"id": name, "objectType": on, "objectId": target, "content": content}


def item(name: str, queue: str, status: str = "PENDING", target: str = "t1") -> dict:
    return {
        "id": name,
        "queueId": queue,
        "objectType": "TRACE",
        "objectId": target,
        "status": status,
    }


def make_service(langfuse_settings, source, warehouse, **sync):
    settings = SyncSettings(_env_file=None, **sync)
    return SyncService(langfuse_settings, settings, source, warehouse, clock=lambda: NOW)


def test_comments_are_read_in_full_on_every_run(langfuse_settings):
    source = FakeSource({COMMENTS: [[comment("c1", "first"), comment("c2", "second")]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="comments")

    first = service.run().entities[0]
    second = service.run().entities[0]

    # No time range and no watermark to resume from: the same full read each time.
    assert source.calls == [(COMMENTS, None, None), (COMMENTS, None, None)]
    assert (first.snapshot, first.rows_fetched, first.rows_inserted) == (True, 2, 2)
    assert (second.rows_fetched, second.rows_inserted, second.rows_updated) == (2, 0, 0)
    assert first.window_start is None and first.reconcile is None
    # The watermark of a snapshot is simply when it was last taken.
    assert first.watermark == NOW and warehouse.watermarks == {"comments": NOW}


def test_an_edited_comment_is_updated_on_the_next_run(langfuse_settings):
    source = FakeSource({COMMENTS: [[comment("c1", "looks fine")]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="comments")
    service.run()

    source.pages[COMMENTS] = [[comment("c1", "on second thought, wrong")]]
    result = service.run().entities[0]

    assert warehouse.tables["comments"][("c1",)]["content"] == "on second thought, wrong"
    assert (result.rows_inserted, result.rows_updated) == (0, 1)


def test_a_deleted_comment_is_reported_and_left_in_place(langfuse_settings, caplog):
    source = FakeSource({COMMENTS: [[comment("c1", "kept"), comment("c2", "to be deleted")]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="comments")
    service.run()

    source.pages[COMMENTS] = [[comment("c1", "kept")]]
    with caplog.at_level("WARNING"):
        result = service.run().entities[0]

    assert result.rows_deleted_upstream == 1
    assert set(warehouse.tables["comments"]) == {("c1",), ("c2",)}
    assert "comments: 1 row(s) in Snowflake no longer exist in Langfuse" in caplog.text


def test_queue_items_are_listed_per_queue(langfuse_settings):
    source = FakeSource(
        {
            QUEUES: [[{"id": "q1", "name": "weekly"}, {"id": "q 2", "name": "ad hoc"}]],
            items_of("q1"): [[item("i1", "q1"), item("i2", "q1", status="COMPLETED")]],
            items_of("q%202"): [[item("i3", "q 2")]],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings, source, warehouse, entities="annotation_queues,annotation_queue_items"
    )

    queues, items = service.run().entities

    # The queues first, then one listing for each of them, the ID escaped for the URL.
    assert [call[0] for call in source.calls] == [QUEUES, items_of("q%202"), items_of("q1")]
    assert (queues.rows_fetched, items.rows_fetched, items.rows_inserted) == (2, 3, 3)
    assert set(warehouse.tables["annotation_queue_items"]) == {("i1",), ("i2",), ("i3",)}
    assert warehouse.watermarks == {"annotation_queues": NOW, "annotation_queue_items": NOW}


def test_asking_for_items_reads_the_queues_too(langfuse_settings):
    source = FakeSource({QUEUES: [[{"id": "q1"}]], items_of("q1"): [[item("i1", "q1")]]})
    service = make_service(
        langfuse_settings, source, FakeWarehouse(), entities="annotation_queue_items"
    )

    result = service.run()

    assert [entity.entity for entity in result.entities] == [
        "annotation_queues",
        "annotation_queue_items",
    ]


def test_a_completed_item_is_updated_and_a_removed_queue_takes_its_items_with_it(
    langfuse_settings,
):
    source = FakeSource(
        {
            QUEUES: [[{"id": "q1"}, {"id": "q2"}]],
            items_of("q1"): [[item("i1", "q1")]],
            items_of("q2"): [[item("i2", "q2")]],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings, source, warehouse, entities="annotation_queues,annotation_queue_items"
    )
    service.run()

    # A reviewer completes i1, and q2 is deleted.
    source.pages[QUEUES] = [[{"id": "q1"}]]
    source.pages[items_of("q1")] = [[item("i1", "q1", status="COMPLETED")]]
    queues, items = service.run().entities

    assert warehouse.tables["annotation_queue_items"][("i1",)]["status"] == "COMPLETED"
    assert (items.rows_updated, items.rows_deleted_upstream) == (1, 1)
    assert queues.rows_deleted_upstream == 1
    assert items_of("q2") not in [call[0] for call in source.calls[3:]]


def test_sampling_follows_the_trace_a_comment_or_item_is_on(langfuse_settings):
    # At a rate of 0.5, trace-b is in the sample and trace-a is not.
    source = FakeSource(
        {
            COMMENTS: [
                [
                    comment("c1", "on an unsampled trace", target="trace-a"),
                    comment("c2", "on a sampled trace", target="trace-b"),
                    comment("c3", "on a session", on="SESSION", target="trace-a"),
                ]
            ],
            QUEUES: [[{"id": "q1"}]],
            items_of("q1"): [
                [item("i1", "q1", target="trace-a"), item("i2", "q1", target="trace-b")]
            ],
        }
    )
    warehouse = FakeWarehouse()
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="comments,annotation_queues,annotation_queue_items",
        sample_rate=0.5,
    )

    comments, queues, items = service.run().entities

    assert set(warehouse.tables["comments"]) == {("c2",), ("c3",)}
    assert set(warehouse.tables["annotation_queue_items"]) == {("i2",)}
    assert (comments.rows_skipped, queues.rows_skipped, items.rows_skipped) == (1, 0, 1)
    # Sampled out is not deleted.
    assert (comments.rows_deleted_upstream, items.rows_deleted_upstream) == (0, 0)


def test_filters_apply_and_do_not_hide_deletions(langfuse_settings):
    source = FakeSource(
        {COMMENTS: [[comment("c1", "on a trace"), comment("c2", "on a session", on="SESSION")]]}
    )
    warehouse = FakeWarehouse()
    held = [comment("c2", "on a session", on="SESSION"), comment("c3", "since deleted")]
    warehouse.seed(ENTITIES["comments"], held)
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="comments",
        filters="comments:objectType=TRACE",
    )

    result = service.run().entities[0]

    assert [row["id"] for row in warehouse.loaded["comments"]] == ["c1"]
    assert (result.rows_fetched, result.rows_skipped) == (2, 1)
    # c2 is merely filtered out now; only c3 is gone from Langfuse.
    assert result.rows_deleted_upstream == 1


def test_the_deletion_check_can_be_turned_off(langfuse_settings):
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["comments"], [comment("gone", "deleted upstream")])
    service = make_service(
        langfuse_settings, FakeSource(), warehouse, entities="comments", check_deletions=False
    )

    assert service.run().entities[0].rows_deleted_upstream is None


@pytest.mark.parametrize("status", [402, 403, 404])
def test_an_entity_the_project_cannot_serve_is_skipped_not_fatal(langfuse_settings, status, caplog):
    source = FakeSource({SCORES: [[{"id": "s1"}]], COMMENTS: [[comment("c1", "fine")]]})
    source.errors[QUEUES] = LangfuseApiError(
        f"GET {QUEUES} failed: HTTP {status}", status_code=status
    )
    warehouse = FakeWarehouse()
    warehouse.seed(ENTITIES["annotation_queues"], [{"id": "held-before"}])
    service = make_service(
        langfuse_settings,
        source,
        warehouse,
        entities="scores,comments,annotation_queues,annotation_queue_items",
        window_hours=24 * 40,
    )

    with caplog.at_level("WARNING"):
        scores, comments, queues, items = service.run().entities

    # Everything else still syncs.
    assert (scores.rows_inserted, comments.rows_inserted) == (1, 1)
    assert f"HTTP {status}" in queues.unavailable
    assert items.unavailable == "annotation_queues could not be read"
    # Nothing was read, so nothing is judged deleted and no snapshot is recorded.
    assert queues.rows_deleted_upstream is None and items.rows_deleted_upstream is None
    assert "annotation_queues" not in warehouse.watermarks
    assert "annotation_queues is not available from this Langfuse project" in caplog.text


@pytest.mark.parametrize("status", [401, 500, None])
def test_other_failures_still_stop_the_run(langfuse_settings, status):
    source = FakeSource()
    source.errors[COMMENTS] = LangfuseApiError(
        "GET /api/public/comments failed", status_code=status
    )
    service = make_service(langfuse_settings, source, FakeWarehouse(), entities="comments")

    with pytest.raises(LangfuseApiError):
        service.run()


def test_core_entities_are_never_skipped(langfuse_settings):
    source = FakeSource()
    source.errors[SCORES] = LangfuseApiError("GET /api/public/v3/scores failed", status_code=404)
    service = make_service(langfuse_settings, source, FakeWarehouse(), entities="scores")

    with pytest.raises(LangfuseApiError):
        service.run()


def test_snapshots_are_taken_whatever_range_or_command_is_used(langfuse_settings):
    source = FakeSource({COMMENTS: [[comment("c1", "hello")]]})
    warehouse = FakeWarehouse()
    service = make_service(langfuse_settings, source, warehouse, entities="comments")
    day = datetime(2026, 3, 5, tzinfo=UTC)

    ranged = service.run(start=day, end=datetime(2026, 3, 6, tzinfo=UTC)).entities[0]
    reconciled = service.reconcile().entities[0]

    assert source.calls == [(COMMENTS, None, None), (COMMENTS, None, None)]
    assert ranged.snapshot and reconciled.snapshot
    assert (ranged.rows_inserted, reconciled.rows_inserted) == (1, 0)
    # A snapshot is already a full comparison; there is nothing further to reconcile.
    assert reconciled.reconcile is None and warehouse.reconciled == {}


def test_progress_is_reported_for_snapshots(langfuse_settings):
    snapshots = []
    service = make_service(
        langfuse_settings,
        FakeSource({COMMENTS: [[comment("c1", "hello")]]}),
        FakeWarehouse(),
        entities="comments",
    )

    service.run(progress=lambda result: snapshots.append(result.entities[-1].rows_fetched))

    assert snapshots == [1]
