import pytest

from langfuse_to_snowflake.entities import ENTITIES
from langfuse_to_snowflake.selection import Selection, is_sampled

V4_OBSERVATIONS = ENTITIES["observations"].endpoints["v4"]
V3_OBSERVATIONS = ENTITIES["observations"].endpoints["v3"]
V4_SCORES = ENTITIES["scores"].endpoints["v4"]


def _selection(*filters: str, rate: float = 1.0) -> Selection:
    return Selection.parse(rate, filters)


def test_no_filters_and_full_rate_keeps_everything():
    selection = _selection().for_entity("observations")

    assert selection.keeps_everything
    assert selection.keeps({"id": "o1"})


def test_filters_apply_to_their_entity_only():
    selection = _selection("environment=production", "observations:level=ERROR", "scores:name=acc")

    observations = selection.for_entity("observations")
    scores = selection.for_entity("scores")

    assert [str(item) for item in observations.filters] == [
        "environment=production",
        "observations:level=ERROR",
    ]
    assert [str(item) for item in scores.filters] == ["environment=production", "scores:name=acc"]


def test_every_filter_must_match():
    selection = _selection("environment=production", "observations:level=ERROR").for_entity(
        "observations"
    )

    assert selection.keeps({"environment": "production", "level": "ERROR"})
    assert not selection.keeps({"environment": "production", "level": "DEFAULT"})
    assert not selection.keeps({"environment": "dev", "level": "ERROR"})


def test_a_trace_is_kept_or_dropped_as_a_whole():
    selection = _selection(rate=0.5)
    observations = selection.for_entity("observations")
    scores = selection.for_entity("scores")

    for number in range(200):
        trace = f"trace-{number}"
        kept = is_sampled(trace, 0.5)
        assert observations.keeps({"id": "a", "traceId": trace}) is kept
        assert observations.keeps({"id": "b", "traceId": trace}) is kept
        assert scores.keeps({"id": "s", "subject": {"kind": "trace", "id": trace}}) is kept
        observation_score = {"kind": "observation", "id": "a", "traceId": trace}
        assert scores.keeps({"id": "s", "subject": observation_score}) is kept


def test_records_outside_any_trace_are_never_sampled_out():
    selection = _selection(rate=0.0001)

    scores = selection.for_entity("scores")
    assert scores.keeps({"id": "s", "subject": {"kind": "session", "id": "sess-1"}})
    assert selection.for_entity("sessions").keeps({"id": "sess-1"})


def test_filters_still_apply_to_records_that_are_exempt_from_sampling():
    selection = _selection("scores:name=accuracy", rate=0.0001).for_entity("scores")

    session_score = {"subject": {"kind": "session", "id": "sess-1"}}
    assert selection.keeps({**session_score, "name": "accuracy"})
    assert not selection.keeps({**session_score, "name": "toxicity"})


def test_whether_a_partial_record_is_enough_to_apply_the_filters():
    light = frozenset({"id", "traceId", "environment", "level", "tags"})

    def decidable(*filters: str) -> bool:
        return _selection(*filters).for_entity("observations").can_decide_from(light)

    assert decidable()
    assert decidable("environment=production", "observations:level!=DEBUG", "tags=vip")
    assert not decidable("environment=production", "observations:totalCost>0")
    # A nested path is judged by the field it starts from.
    assert not decidable("observations:metadata.tier=gold")
    # Filters meant for another entity do not count.
    assert decidable("scores:name=accuracy")


@pytest.mark.parametrize("rate", [0, -0.1, 1.01])
def test_sample_rate_must_be_a_share(rate):
    with pytest.raises(ValueError, match="sample rate"):
        Selection.parse(rate, [])


def test_v4_rejects_filters_on_derived_entities():
    selection = _selection("traces:userId=alice")

    with pytest.raises(ValueError, match="derived from observations.*observations:userId"):
        selection.check("v4")
    selection.check("v3")  # traces are extracted on v3


def test_pushdown_sends_equality_filters_the_endpoint_supports():
    selection = _selection(
        "environment=production,staging", "observations:level=ERROR", "observations:name=chat"
    ).for_entity("observations")

    endpoint = selection.pushdown(V4_OBSERVATIONS)

    assert endpoint.params == {
        "environment": ["production", "staging"],
        "level": "ERROR",
        "name": "chat",
    }
    assert endpoint.path == V4_OBSERVATIONS.path
    # The registry's endpoint is not modified.
    assert V4_OBSERVATIONS.params == {}


def test_pushdown_joins_values_where_the_endpoint_takes_a_list():
    selection = _selection("scores:name=accuracy,toxicity", "environment=production").for_entity(
        "scores"
    )

    endpoint = selection.pushdown(V4_SCORES)

    assert endpoint.params == {
        "fields": "details,subject,annotation",
        "name": "accuracy,toxicity",
        "environment": "production",
    }


@pytest.mark.parametrize(
    "text",
    [
        "observations:level!=DEBUG",  # not an equality
        "observations:level=ERROR,WARNING",  # the parameter takes one value
        "observations:totalCost>=0.01",  # no such parameter
        "observations:metadata.tier=gold",  # nested field
        "observations:userId=null",  # null cannot be sent
        "observations:name=",  # nor can an empty value
        "observations:input~refund",
    ],
)
def test_filters_the_endpoint_cannot_apply_stay_local(text):
    selection = _selection(text).for_entity("observations")

    assert selection.pushdown(V4_OBSERVATIONS).params == {}


def test_pushdown_depends_on_the_api_version():
    selection = _selection("observations:userId=alice", "observations:type=GENERATION").for_entity(
        "observations"
    )

    assert selection.pushdown(V4_OBSERVATIONS).params == {"userId": "alice", "type": "GENERATION"}
    # v3 observation records carry no user, so there is nothing to push or to check against.
    assert selection.pushdown(V3_OBSERVATIONS).params == {"type": "GENERATION"}


def test_two_filters_on_one_field_do_not_overwrite_each_other():
    selection = _selection("observations:name=a", "observations:name=b").for_entity("observations")

    assert selection.pushdown(V4_OBSERVATIONS).params == {"name": "a"}
    # Both still apply locally, so nothing can match.
    assert not selection.keeps({"name": "a"})
