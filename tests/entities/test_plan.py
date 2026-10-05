import pytest

from langfuse_to_snowflake.entities import ENTITY_NAMES, build_plan


def _names(plan):
    return [extract.spec.name for extract in plan.extracts], [view.name for view in plan.views]


TRACING = ("observations", "scores", "traces", "sessions")
REVIEW = ("comments", "annotation_queues", "annotation_queue_items")


def test_the_entities_are_tracing_data_and_human_review():
    assert ENTITY_NAMES == TRACING + REVIEW


def test_v4_extracts_observations_and_scores_and_derives_the_rest():
    plan = build_plan("v4", TRACING)

    assert _names(plan) == (["observations", "scores"], ["traces", "sessions"])
    paths = {extract.spec.name: extract.endpoint.path for extract in plan.extracts}
    assert paths == {
        "observations": "/api/public/v2/observations",
        "scores": "/api/public/v3/scores",
    }
    assert all(extract.endpoint.pagination == "cursor" for extract in plan.extracts)
    assert all(extract.endpoint.windowed for extract in plan.extracts)


def test_v3_extracts_all_four_from_the_legacy_endpoints():
    plan = build_plan("v3", TRACING)

    assert _names(plan) == (["observations", "scores", "traces", "sessions"], [])
    paths = {extract.spec.name: extract.endpoint.path for extract in plan.extracts}
    assert paths == {
        "observations": "/api/public/observations",
        "scores": "/api/public/v2/scores",
        "traces": "/api/public/traces",
        "sessions": "/api/public/sessions",
    }
    assert all(extract.endpoint.pagination == "page" for extract in plan.extracts)


@pytest.mark.parametrize("version", ["v4", "v3"])
def test_review_entities_are_snapshots_on_both_api_versions(version):
    plan = build_plan(version, REVIEW)

    assert _names(plan) == (list(REVIEW), [])
    paths = {extract.spec.name: extract.endpoint.path for extract in plan.extracts}
    assert paths == {
        "comments": "/api/public/comments",
        "annotation_queues": "/api/public/annotation-queues",
        "annotation_queue_items": "/api/public/annotation-queues/{parent_id}/items",
    }
    # No time filter to read them incrementally by.
    assert not any(extract.endpoint.windowed for extract in plan.extracts)


def test_selecting_a_child_also_extracts_its_parent_first():
    plan = build_plan("v4", ["annotation_queue_items"])

    assert _names(plan) == (["annotation_queues", "annotation_queue_items"], [])


def test_every_entity_can_list_its_keys_without_any_filter():
    for version in ("v4", "v3"):
        for extract in build_plan(version, ENTITY_NAMES).extracts:
            # Whatever the lightest request is, it must not narrow the result.
            assert set(extract.endpoint.key_listing) <= {"fields"}
    (observations,) = build_plan("v4", ["observations"]).extracts
    assert observations.endpoint.key_listing == {"fields": "core"}


def test_selecting_a_v4_view_also_extracts_its_source():
    plan = build_plan("v4", ["traces"])

    assert _names(plan) == (["observations"], ["traces"])


def test_only_selected_entities_are_planned():
    assert _names(build_plan("v4", ["scores"])) == (["scores"], [])
    assert _names(build_plan("v3", ["sessions"])) == (["sessions"], [])


def test_v4_observations_request_the_configured_field_groups():
    plan = build_plan(
        "v4",
        ["observations"],
        observation_fields=("core", "basic", "usage"),
        expand_metadata=("customer", "ticket"),
    )

    assert plan.extracts[0].endpoint.params == {
        "fields": "core,basic,usage",
        "expandMetadata": "customer,ticket",
    }


def test_v3_observations_send_no_field_groups():
    plan = build_plan("v3", ["observations"], expand_metadata=("customer",))

    assert plan.extracts[0].endpoint.params == {}


def test_views_need_the_field_groups_they_are_built_from():
    with pytest.raises(ValueError, match="trace_context"):
        build_plan("v4", ["traces"], observation_fields=("core", "basic"))

    # Without views the same field selection is fine.
    build_plan("v4", ["observations"], observation_fields=("core", "basic"))


def test_v4_observations_can_be_listed_cheaply():
    (extract,) = build_plan("v4", ["observations"]).extracts

    listing = extract.endpoint.listing
    assert listing.params == {"fields": "core,basic,time,trace_context"}
    assert {"id", "traceId", "startTime", "updatedAt", "environment", "tags"} <= listing.fields
    # The heavy groups are what the listing leaves out.
    assert not {"input", "output", "metadata", "totalCost"} & listing.fields


def test_the_listing_only_asks_for_field_groups_that_are_synced():
    (extract,) = build_plan(
        "v4", ["observations"], observation_fields=("core", "time", "usage")
    ).extracts

    listing = extract.endpoint.listing
    assert listing.params == {"fields": "core,time"}
    assert "environment" not in listing.fields


def test_there_is_no_listing_without_the_time_group():
    (extract,) = build_plan(
        "v4", ["observations"], observation_fields=("core", "basic", "io")
    ).extracts

    assert extract.endpoint.listing is None


def test_only_v4_observations_have_a_listing():
    others = [
        extract
        for version in ("v4", "v3")
        for extract in build_plan(version, ENTITY_NAMES).extracts
        if (version, extract.spec.name) != ("v4", "observations")
    ]

    assert len(others) == 11
    assert all(extract.endpoint.listing is None for extract in others)


def test_unknown_and_empty_selections_are_rejected():
    with pytest.raises(ValueError, match="Unknown entities: prompts"):
        build_plan("v4", ["scores", "prompts"])
    with pytest.raises(ValueError, match="No entities"):
        build_plan("v4", [])
