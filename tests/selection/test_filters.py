import pytest

from langfuse_to_snowflake.selection import Filter, parse_filter


def test_parse_an_unscoped_filter_with_several_values():
    parsed = parse_filter("environment=production,staging")

    assert parsed == Filter("environment", "=", ("production", "staging"), entity=None)
    assert parsed.applies_to("observations") and parsed.applies_to("scores")


def test_parse_a_scoped_filter():
    parsed = parse_filter("observations:level!=DEBUG")

    assert parsed == Filter("level", "!=", ("DEBUG",), entity="observations")
    assert parsed.applies_to("observations") and not parsed.applies_to("scores")


def test_parse_a_filter_on_an_entity_with_an_underscore_in_its_name():
    parsed = parse_filter("annotation_queue_items:status=PENDING")

    assert parsed == Filter("status", "=", ("PENDING",), entity="annotation_queue_items")
    assert str(parsed) == "annotation_queue_items:status=PENDING"


@pytest.mark.parametrize(
    ("text", "field", "op", "values"),
    [
        ("scores:value>=0.5", "value", ">=", ("0.5",)),
        ("scores:value<=0.5", "value", "<=", ("0.5",)),
        ("scores:value>0.5", "value", ">", ("0.5",)),
        ("scores:value<0.5", "value", "<", ("0.5",)),
        # Substring values are taken whole, commas included.
        ("observations:input~refund, please", "input", "~", ("refund, please",)),
        ("observations:input!~test", "input", "!~", ("test",)),
        ("  observations:metadata.customer-tier = gold ", "metadata.customer-tier", "=", ("gold",)),
        # Only the first operator splits; the value may contain more.
        ("metadata.url=https://x.test/a?b=c", "metadata.url", "=", ("https://x.test/a?b=c",)),
        ("userId=", "userId", "=", ("",)),
    ],
)
def test_parse_operators_and_values(text, field, op, values):
    parsed = parse_filter(text)

    assert (parsed.field, parsed.op, parsed.values) == (field, op, values)


@pytest.mark.parametrize(
    "text",
    ["environment=production,staging", "observations:level!=DEBUG", "scores:comment~refund"],
)
def test_filters_print_in_the_form_they_are_written(text):
    assert str(parse_filter(text)) == text
    assert parse_filter(str(parse_filter(text))) == parse_filter(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("just some words", "cannot parse filter"),
        ("=production", "cannot parse filter"),
        ("prompts:name=x", "unknown entity 'prompts'"),
        ("scores:value>", "> needs a value"),
        ("observations:input~", "~ needs a value"),
    ],
)
def test_malformed_filters_are_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        parse_filter(text)


@pytest.mark.parametrize(
    ("text", "record", "expected"),
    [
        # Equality, any of / none of.
        ("environment=production", {"environment": "production"}, True),
        ("environment=production,staging", {"environment": "staging"}, True),
        ("environment=production,staging", {"environment": "dev"}, False),
        ("environment=Production", {"environment": "production"}, False),
        ("environment!=dev", {"environment": "production"}, True),
        ("environment!=dev,test", {"environment": "test"}, False),
        # A missing field equals nothing, so it fails = and passes !=.
        ("environment=production", {}, False),
        ("environment!=dev", {}, True),
        # null matches missing and explicit nulls.
        ("userId=null", {"userId": None}, True),
        ("userId=null", {}, True),
        ("userId=null", {"userId": "alice"}, False),
        ("userId!=null", {"userId": "alice"}, True),
        ("userId!=null", {"userId": None}, False),
        ("userId=null,alice", {"userId": "alice"}, True),
        # Lists match if any element does.
        ("tags=vip", {"tags": ["beta", "vip"]}, True),
        ("tags=vip,internal", {"tags": ["beta"]}, False),
        ("tags!=vip", {"tags": ["beta"]}, True),
        ("tags!=vip", {"tags": []}, True),
        ("tags!=vip", {"tags": ["vip"]}, False),
        # Dotted paths.
        ("metadata.tier=gold", {"metadata": {"tier": "gold"}}, True),
        ("metadata.tier=gold", {"metadata": {"tier": "silver"}}, False),
        ("metadata.tier=gold", {"metadata": "not an object"}, False),
        ("subject.kind=trace", {"subject": {"kind": "trace", "id": "t1"}}, True),
        # Booleans and numbers compare by value, not by spelling.
        ("isRootObservation=true", {"isRootObservation": True}, True),
        ("isRootObservation=TRUE", {"isRootObservation": True}, True),
        ("public=false", {"public": False}, True),
        ("public=1", {"public": True}, False),
        ("promptVersion=3", {"promptVersion": 3}, True),
        ("promptVersion=3.0", {"promptVersion": 3}, True),
        ("value=0.9", {"value": 0.9}, True),
        ("value=high", {"value": 0.9}, False),
        ("value=0.9", {"value": "0.9"}, True),
        # Ordering: numeric where both sides are numbers.
        ("totalCost>=0.01", {"totalCost": 0.02}, True),
        ("totalCost>=0.01", {"totalCost": 0.01}, True),
        ("totalCost>0.01", {"totalCost": 0.01}, False),
        ("totalCost<0.01", {"totalCost": 0.001}, True),
        ("latency>9", {"latency": 10}, True),
        ("totalPrice<0.001", {"totalPrice": "0.000005"}, True),
        ("totalCost>0", {}, False),
        ("totalCost>0", {"totalCost": None}, False),
        ("public>0", {"public": True}, False),
        ("totalCost>cheap", {"totalCost": 5}, False),
        # Ordering: text otherwise, which suits ISO 8601 timestamps.
        ("startTime>=2026-03-01", {"startTime": "2026-03-10T08:00:00.000Z"}, True),
        ("startTime<2026-03-01", {"startTime": "2026-03-10T08:00:00.000Z"}, False),
        # Substring.
        ("input~refund", {"input": "please refund my order"}, True),
        ("input~Refund", {"input": "please refund my order"}, False),
        ("input~refund", {"input": {"question": "refund?"}}, True),
        ("input~refund", {}, False),
        ("input!~refund", {}, True),
        ("input!~refund", {"input": "hello"}, True),
        ("tags~vi", {"tags": ["beta", "vip"]}, True),
        ("promptVersion~1", {"promptVersion": 12}, True),
    ],
)
def test_matching(text, record, expected):
    assert parse_filter(text).matches(record) is expected
