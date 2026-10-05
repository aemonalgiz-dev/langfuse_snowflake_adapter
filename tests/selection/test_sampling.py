import pytest

from langfuse_to_snowflake.selection import is_sampled, sample_key

KEYS = [f"trace-{number}" for number in range(10_000)]


@pytest.mark.parametrize(
    ("entity", "record", "key"),
    [
        ("observations", {"id": "o1", "traceId": "t1"}, "t1"),
        # A legacy observation without a trace is sampled on its own ID.
        ("observations", {"id": "o1", "traceId": None}, "o1"),
        ("traces", {"id": "t1"}, "t1"),
        # Scores v3: the scored object is nested under subject.
        ("scores", {"id": "s1", "subject": {"kind": "trace", "id": "t1"}}, "t1"),
        ("scores", {"id": "s1", "subject": {"kind": "TRACE", "id": "t1"}}, "t1"),
        (
            "scores",
            {"id": "s1", "subject": {"kind": "observation", "id": "o1", "traceId": "t1"}},
            "t1",
        ),
        ("scores", {"id": "s1", "subject": {"kind": "observation", "id": "o1"}}, None),
        ("scores", {"id": "s1", "subject": {"kind": "session", "id": "sess-1"}}, None),
        ("scores", {"id": "s1", "subject": {"kind": "experiment", "id": "exp-1"}}, None),
        # Scores v2: flat fields.
        ("scores", {"id": "s1", "traceId": "t1", "observationId": "o1"}, "t1"),
        ("scores", {"id": "s1", "traceId": None, "sessionId": "sess-1"}, None),
        ("sessions", {"id": "sess-1"}, None),
        # Comments and queue items follow the trace they are on, if they are on one.
        ("comments", {"id": "c1", "objectType": "TRACE", "objectId": "t1"}, "t1"),
        ("comments", {"id": "c1", "objectType": "trace", "objectId": "t1"}, "t1"),
        ("comments", {"id": "c1", "objectType": "OBSERVATION", "objectId": "o1"}, None),
        ("comments", {"id": "c1", "objectType": "PROMPT", "objectId": "p1"}, None),
        ("annotation_queue_items", {"id": "i1", "objectType": "TRACE", "objectId": "t1"}, "t1"),
        ("annotation_queue_items", {"id": "i1", "objectType": "SESSION", "objectId": "s1"}, None),
        ("annotation_queues", {"id": "q1"}, None),
    ],
)
def test_sample_key_is_the_trace_a_record_belongs_to(entity, record, key):
    assert sample_key(entity, record) == key


def test_sampling_is_deterministic():
    first = [is_sampled(key, 0.3) for key in KEYS]

    assert first == [is_sampled(key, 0.3) for key in KEYS]


def test_sampling_decisions_are_stable_across_versions():
    # Pinned: a change of hash would silently reshuffle every existing sample.
    keys = ("trace-a", "trace-b", "trace-c", "trace-d", "trace-e", "trace-f")

    assert [is_sampled(key, 0.5) for key in keys] == [False, True, False, False, True, False]


@pytest.mark.parametrize("rate", [0.01, 0.1, 0.5, 0.9])
def test_the_sampled_share_is_close_to_the_rate(rate):
    share = sum(is_sampled(key, rate) for key in KEYS) / len(KEYS)

    assert share == pytest.approx(rate, abs=0.02)


def test_a_rate_of_one_keeps_everything():
    assert all(is_sampled(key, 1.0) for key in KEYS)


def test_raising_the_rate_only_adds_to_the_sample():
    low = {key for key in KEYS if is_sampled(key, 0.1)}
    high = {key for key in KEYS if is_sampled(key, 0.4)}

    assert low < high
