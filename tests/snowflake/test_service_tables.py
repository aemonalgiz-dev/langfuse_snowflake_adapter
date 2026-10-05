"""The service's own tables: saved settings and the history of runs."""

from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.snowflake import RunsTable, SettingsTable

START = datetime(2026, 4, 1, 9, tzinfo=UTC)


class Clock:
    """Each reading is a minute after the last."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def settings_table(warehouse) -> SettingsTable:
    return SettingsTable(warehouse.sessions, table_prefix=warehouse.prefix, clock=Clock())


@pytest.fixture
def runs_table(warehouse) -> RunsTable:
    return RunsTable(warehouse.sessions, table_prefix=warehouse.prefix)


def test_nothing_is_saved_to_begin_with(settings_table, warehouse):
    assert settings_table.read() == {}
    assert settings_table.history("support") == []
    assert settings_table.name == warehouse.name("sync_settings")


def test_saved_settings_come_back_as_they_went_in(settings_table):
    saved = {
        "entities": ["observations", "scores"],
        "sample_rate": 0.25,
        "filters": ["environment=production", "observations:name~naïve ✓"],
        "exclude_fields": ["observations:input"],
        "reconcile_full": True,
        "schedule_minutes": 15,
    }

    settings_table.write("support", saved)

    assert settings_table.read() == {"support": saved}


def test_the_newest_change_of_each_project_is_in_effect(settings_table):
    settings_table.write("support", {"sample_rate": 0.5})
    settings_table.write("search", {"schedule_minutes": 30})
    settings_table.write("support", {"sample_rate": 0.1, "filters": ["level=ERROR"]})

    assert settings_table.read() == {
        "support": {"sample_rate": 0.1, "filters": ["level=ERROR"]},
        "search": {"schedule_minutes": 30},
    }


def test_resetting_is_a_change_like_any_other(settings_table):
    settings_table.write("support", {"sample_rate": 0.5})
    settings_table.write("support", {})

    assert settings_table.read() == {"support": {}}
    assert len(settings_table.history("support")) == 2


def test_every_change_is_kept_as_history_newest_first(settings_table):
    settings_table.write("support", {"sample_rate": 0.5})
    settings_table.write("search", {"schedule_minutes": 30})
    settings_table.write("support", {"sample_rate": 0.1})

    history = settings_table.history("support")

    assert [change.settings for change in history] == [{"sample_rate": 0.1}, {"sample_rate": 0.5}]
    assert [change.changed_at for change in history] == [
        START + timedelta(minutes=3),
        START + timedelta(minutes=1),
    ]
    assert len(settings_table.history("support", limit=1)) == 1


def test_another_instance_sees_what_this_one_saved(settings_table, warehouse):
    settings_table.write("support", {"sample_rate": 0.5})

    other = SettingsTable(warehouse.sessions, table_prefix=warehouse.prefix)

    assert other.read() == {"support": {"sample_rate": 0.5}}


def test_the_location_names_the_table_for_people(warehouse):
    table = SettingsTable(warehouse.sessions, table_prefix="lf_", schema="ANALYTICS.LANGFUSE")

    assert table.location == "Snowflake table ANALYTICS.LANGFUSE.LF_SYNC_SETTINGS"


def _run(identifier: str, minutes: int = 0, **fields) -> dict:
    return {
        "id": identifier,
        "project": "support",
        "kind": "sync",
        "triggered_by": "schedule",
        "status": "running",
        "created_at": START + timedelta(minutes=minutes),
        "started_at": START + timedelta(minutes=minutes, seconds=1),
        "finished_at": None,
        "request": {"project": "support", "entities": None},
        "result": None,
        "error": None,
        **fields,
    }


def test_no_runs_to_begin_with(runs_table, warehouse):
    assert runs_table.recent() == []
    assert runs_table.name == warehouse.name("sync_runs")


def test_a_run_is_recorded_when_it_starts_and_completed_when_it_ends(runs_table):
    run = _run("r1")
    runs_table.started(run)

    assert runs_table.recent() == [run]

    result = {"project_id": "p", "entities": [{"entity": "scores", "rows_inserted": 3}]}
    done = {
        **run,
        "status": "succeeded",
        "finished_at": START + timedelta(minutes=2),
        "result": result,
    }
    runs_table.finished(done)

    assert runs_table.recent() == [done]


def test_a_failed_run_keeps_its_error(runs_table):
    run = _run("r1")
    runs_table.started(run)

    failed = {
        **run,
        "status": "failed",
        "finished_at": START + timedelta(minutes=2),
        "error": 'OperationalError: it\'s "down"',
    }
    runs_table.finished(failed)

    assert runs_table.recent() == [failed]


def test_a_run_whose_start_was_never_recorded_is_still_recorded_at_its_end(runs_table):
    done = _run("r1", status="succeeded", finished_at=START + timedelta(minutes=2), result={})

    runs_table.finished(done)

    assert runs_table.recent() == [done]


def test_recent_runs_come_newest_first_and_can_be_limited(runs_table):
    for index in range(4):
        runs_table.started(_run(f"r{index}", minutes=index))
    runs_table.finished({**_run("r1", minutes=1), "status": "succeeded"})

    recent = runs_table.recent(limit=3)

    assert [run["id"] for run in recent] == ["r3", "r2", "r1"]
    assert [run["status"] for run in recent] == ["running", "running", "succeeded"]
