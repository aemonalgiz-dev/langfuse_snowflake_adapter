from datetime import UTC, datetime

import pytest
from snowflake.connector import errors

from langfuse_to_snowflake.entities import ENTITIES, build_plan
from langfuse_to_snowflake.snowflake import Sessions, SnowflakeAdapter, frames
from langfuse_to_snowflake.snowflake.retry import is_transient

SCORES = ENTITIES["scores"]
PLAN = build_plan("v4", ("scores",), observation_fields=(), expand_metadata=())


@pytest.fixture
def sleeps() -> list[float]:
    return []


def _adapter(session, catalog, settings, sleeps) -> SnowflakeAdapter:
    sessions = Sessions(settings, session=session, catalog=lambda _: catalog, sleep=sleeps.append)
    adapter = SnowflakeAdapter(sessions=sessions)
    adapter.connect()
    adapter.ensure_schema(PLAN)
    return adapter


@pytest.fixture
def adapter(local_session, local_catalog, snowflake_settings, sleeps) -> SnowflakeAdapter:
    return _adapter(local_session, local_catalog, snowflake_settings, sleeps)


def _outage() -> Exception:
    return errors.OperationalError(msg="Service temporarily unavailable")


def _failing(monkeypatch, target, name: str, failures: list[Exception]) -> list[int]:
    """Make ``target.name`` raise each of ``failures`` in turn before it works again."""
    original = getattr(target, name)
    calls: list[int] = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return calls


def test_only_network_and_service_errors_are_transient():
    assert is_transient(errors.OperationalError(msg="network"))
    assert is_transient(errors.ServiceUnavailableError(msg="503"))
    assert not is_transient(errors.ProgrammingError(msg="syntax error"))
    assert not is_transient(errors.NonRetryableTlsError(msg="bad certificate"))
    assert not is_transient(ValueError("not a Snowflake error"))


def test_a_failed_merge_is_repeated_as_a_whole(adapter, monkeypatch, sleeps, local_session):
    attempts = _failing(monkeypatch, frames, "batch", [_outage()])

    result = adapter.load(SCORES, "proj-1", [{"id": "s1"}, {"id": "s2"}])

    assert len(attempts) == 2
    assert result.rows_inserted == 2
    assert local_session.table("LANGFUSE_SCORES").count() == 2
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 2


def test_a_merge_that_failed_after_writing_does_no_harm_when_repeated(
    adapter, monkeypatch, local_session
):
    """The connection drops after the rows went in but before the answer came back."""
    merge = adapter._merge
    outcomes = []

    def once_lost(spec, batch):
        outcome = merge(spec, batch)
        outcomes.append(outcome)
        if len(outcomes) == 1:
            raise _outage()
        return outcome

    monkeypatch.setattr(adapter, "_merge", once_lost)

    result = adapter.load(SCORES, "proj-1", [{"id": "s1"}, {"id": "s2"}])

    assert [outcome.rows_inserted for outcome in outcomes] == [2, 0]
    assert local_session.table("LANGFUSE_SCORES").count() == 2
    # The repeat found the rows already there; what is reported is what it saw.
    assert (result.rows_inserted, result.rows_updated) == (0, 0)


def test_statement_errors_are_not_retried(adapter, monkeypatch, sleeps):
    attempts = _failing(monkeypatch, frames, "batch", [errors.ProgrammingError(msg="bad SQL")])

    with pytest.raises(errors.ProgrammingError):
        adapter.load(SCORES, "proj-1", [{"id": "s1"}])

    assert len(attempts) == 1
    assert sleeps == []


def test_retries_are_bounded(adapter, monkeypatch, sleeps, local_session):
    attempts = _failing(monkeypatch, frames, "batch", [_outage() for _ in range(5)])

    with pytest.raises(errors.OperationalError):
        adapter.load(SCORES, "proj-1", [{"id": "s1"}])

    # SNOWFLAKE_MAX_RETRIES defaults to 2: three attempts in total.
    assert len(attempts) == 3
    assert len(sleeps) == 2
    assert local_session.table("LANGFUSE_SCORES").count() == 0


def test_retries_can_be_disabled(
    local_session, local_catalog, snowflake_settings, sleeps, monkeypatch
):
    settings = snowflake_settings.model_copy(update={"max_retries": 0})
    adapter = _adapter(local_session, local_catalog, settings, sleeps)
    _failing(monkeypatch, frames, "batch", [_outage()])

    with pytest.raises(errors.OperationalError):
        adapter.load(SCORES, "proj-1", [{"id": "s1"}])
    assert sleeps == []


def test_state_changes_are_retried(adapter, monkeypatch, sleeps):
    _failing(monkeypatch, adapter, "_merge_watermark", [_outage()])

    adapter.set_watermark("proj-1", "scores", datetime(2026, 3, 1, tzinfo=UTC), "v4")

    assert adapter.get_watermarks("proj-1") == {"scores": datetime(2026, 3, 1, tzinfo=UTC)}
    assert len(sleeps) == 1


def test_schema_checks_are_retried(adapter, monkeypatch, sleeps, local_catalog):
    _failing(monkeypatch, local_catalog, "kinds", [_outage()])

    adapter.ensure_schema(PLAN)

    assert len(sleeps) == 1
