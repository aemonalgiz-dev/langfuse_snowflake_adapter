import pytest

from langfuse_to_snowflake.selection import (
    FieldRef,
    Selection,
    is_excluded,
    parse_field,
    required_fields,
    without,
)


@pytest.mark.parametrize(
    ("text", "entity", "path"),
    [
        ("observations:input", "observations", "input"),
        ("observations:metadata.email", "observations", "metadata.email"),
        ("metadata.internal_notes", None, "metadata.internal_notes"),
        ("  scores:comment ", "scores", "comment"),
        ("annotation_queue_items:objectId", "annotation_queue_items", "objectId"),
        # Keys inside nested fields are whatever the project logs.
        ("observations:metadata.customer-tier", "observations", "metadata.customer-tier"),
        ("observations:metadata.user@id.value", "observations", "metadata.user@id.value"),
    ],
)
def test_parse(text, entity, path):
    parsed = parse_field(text)

    assert parsed == FieldRef(path=path, entity=entity)
    assert parse_field(str(parsed)) == parsed


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "cannot parse field"),
        ("two words", "cannot parse field"),
        ("observations:", "cannot parse field"),
        ("prompts:name", "unknown entity 'prompts'"),
        # Without these a row could not be identified or placed in time.
        ("observations:id", "id is needed to load observations"),
        ("observations:traceId", "traceId is needed to load observations"),
        ("observations:startTime", "startTime is needed to load observations"),
        ("scores:timestamp", "timestamp is needed to load scores"),
        ("id", "id is needed to load"),
    ],
)
def test_fields_that_cannot_be_left_out(text, message):
    with pytest.raises(ValueError, match=message):
        parse_field(text)


def test_required_fields_are_the_key_and_the_time():
    assert required_fields("observations") == {"traceId", "id", "startTime"}
    assert required_fields("scores") == {"id", "timestamp"}
    assert required_fields("comments") == {"id", "createdAt"}


def test_without_removes_fields_and_nested_keys():
    record = {
        "id": "o1",
        "input": "secret prompt",
        "metadata": {"email": "a@b.c", "tier": "gold", "nested": {"keep": 1, "drop": 2}},
        "tags": ["x"],
    }

    result = without(record, ["input", "metadata.email", "metadata.nested.drop", "missing.key"])

    assert result == {
        "id": "o1",
        "metadata": {"tier": "gold", "nested": {"keep": 1}},
        "tags": ["x"],
    }
    # The original record, and anything not on the path, is left as it was.
    assert record["input"] == "secret prompt" and record["metadata"]["email"] == "a@b.c"
    assert result["tags"] is record["tags"]


@pytest.mark.parametrize(
    ("record", "paths"),
    [
        ({"id": "o1"}, ["input"]),
        ({"id": "o1", "metadata": "not an object"}, ["metadata.email"]),
        ({"id": "o1", "metadata": None}, ["metadata.email"]),
        ({"id": "o1", "metadata": {"tier": "gold"}}, ["metadata.email"]),
    ],
)
def test_without_returns_the_same_record_when_there_is_nothing_to_remove(record, paths):
    assert without(record, paths) is record


def test_a_whole_field_can_go_and_take_its_keys_with_it():
    assert without({"id": "o1", "metadata": {"a": 1}}, ["metadata"]) == {"id": "o1"}


def test_is_excluded_covers_the_field_and_everything_inside_it():
    excluded = ["metadata", "input"]

    assert is_excluded("metadata", excluded)
    assert is_excluded("metadata.email", excluded)
    assert is_excluded("input", excluded)
    assert not is_excluded("metadataExtra", excluded)
    assert not is_excluded("output", excluded)


def test_exclusions_apply_to_their_entity_and_filters_still_see_the_field():
    selection = Selection.parse(
        1.0,
        ["observations:metadata.tier=gold"],
        ["observations:input", "metadata.tier", "scores:comment"],
    )
    observations = selection.for_entity("observations")
    scores = selection.for_entity("scores")

    assert observations.excluded == ("input", "metadata.tier")
    assert scores.excluded == ("metadata.tier", "comment")

    record = {"id": "o1", "input": "prompt", "output": "answer", "metadata": {"tier": "gold"}}
    # The filter is judged on the full record, then the field is dropped.
    assert observations.keeps(record)
    assert observations.shape(record) == {"id": "o1", "output": "answer", "metadata": {}}


def test_shape_returns_the_record_itself_when_nothing_is_excluded():
    record = {"id": "o1"}

    assert Selection.parse(1.0, []).for_entity("observations").shape(record) is record


def test_v4_rejects_exclusions_on_derived_entities():
    selection = Selection.parse(1.0, [], ["traces:userId"])

    with pytest.raises(ValueError, match="field 'traces:userId'.*Use observations instead"):
        selection.check("v4")
    selection.check("v3")
