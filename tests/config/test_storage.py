"""Settings kept somewhere other than a file: what the store asks of its storage."""

import pytest
from pydantic import ValidationError

from langfuse_to_snowflake.config import (
    ConfigError,
    ConfigFile,
    ConfigStore,
    Deployment,
    LangfuseSettings,
    SettingsUnavailable,
    SyncSettings,
)


class Storage:
    """Holds saved settings like a shared table would, and can be made to fail."""

    location = "the settings table"

    def __init__(self, saved: dict | None = None) -> None:
        self.saved = saved or {}
        self.reads = 0
        self.down: Exception | None = None

    def read(self) -> dict:
        self.reads += 1
        if self.down:
            raise self.down
        return {project: dict(overrides) for project, overrides in self.saved.items()}

    def write(self, project: str, overrides) -> None:
        if self.down:
            raise self.down
        self.saved[project] = dict(overrides)


class StorageWithHistory(Storage):
    def history(self, project: str, limit: int):
        return [f"{project} change {index}" for index in range(limit)]


def _two_projects(settings):
    def project(name: str):
        langfuse = LangfuseSettings(
            name=name, host=settings.langfuse.host, public_key=f"pk-{name}", secret_key="sk"
        )
        return settings.model_copy(update={"langfuse": langfuse})

    return {"support": project("support"), "search": project("search")}


def test_what_storage_holds_is_applied_when_the_store_opens(settings):
    storage = Storage({"default": {"sample_rate": 0.25}, "other": {"sample_rate": 0.9}})

    store = ConfigStore(settings, storage=storage)

    assert store.current().sync.sample_rate == 0.25
    assert store.location == "the settings table"
    assert store.path is None


def test_changes_are_written_through_to_storage(settings):
    storage = Storage()
    store = ConfigStore(settings, storage=storage)

    store.update({"sample_rate": 0.5, "filters": ["level=ERROR"]})
    assert storage.saved == {"default": {"sample_rate": 0.5, "filters": ["level=ERROR"]}}

    store.reset()
    assert storage.saved == {"default": {}}


def test_refresh_takes_over_what_was_saved_elsewhere(settings):
    storage = Storage()
    store = ConfigStore(settings, storage=storage)
    storage.saved["default"] = {"exclude_fields": ["observations:input"]}
    assert store.current().sync.exclude_fields == ()

    store.refresh()

    assert store.current().sync.exclude_fields == ("observations:input",)

    # And follows it back when the change is undone elsewhere.
    storage.saved["default"] = {}
    store.refresh()
    assert store.current() is settings


def test_storage_that_cannot_be_read_is_not_mistaken_for_no_settings(settings):
    storage = Storage({"default": {"exclude_fields": ["observations:input"]}})
    storage.down = ConnectionError("no route to host")

    with pytest.raises(SettingsUnavailable) as error:
        ConfigStore(settings, storage=storage)

    assert "could not be read from the settings table" in str(error.value)
    assert "ConnectionError: no route to host" in str(error.value)
    # It is a configuration error like any other to whoever only catches those.
    assert isinstance(error.value, ConfigError)


def test_a_failed_refresh_leaves_the_settings_as_they_were_and_says_so(settings):
    storage = Storage({"default": {"exclude_fields": ["observations:input"]}})
    store = ConfigStore(settings, storage=storage)
    storage.down = ConnectionError("no route to host")

    with pytest.raises(SettingsUnavailable):
        store.refresh()

    assert store.current().sync.exclude_fields == ("observations:input",)


def test_a_change_that_cannot_be_saved_is_not_applied(settings):
    storage = Storage()
    store = ConfigStore(settings, storage=storage)
    storage.down = ConnectionError("no route to host")

    with pytest.raises(SettingsUnavailable, match="could not be saved to the settings table"):
        store.update({"sample_rate": 0.5})
    with pytest.raises(SettingsUnavailable):
        store.reset()

    assert store.current() is settings


def test_saved_settings_that_are_not_valid_are_refused_with_where_they_are(settings):
    storage = Storage({"default": {"sample_rate": 7}})

    with pytest.raises(ConfigError, match="the settings table holds settings for default"):
        ConfigStore(settings, storage=storage)


def test_history_is_passed_on_from_storage_that_keeps_it(settings):
    plain = ConfigStore(settings, storage=Storage())
    kept = ConfigStore(settings, storage=StorageWithHistory())

    assert (plain.keeps_history, plain.history()) == (False, None)
    assert kept.keeps_history
    assert kept.history(limit=2) == ["default change 0", "default change 1"]


def test_a_store_without_a_file_remembers_changes_for_as_long_as_it_lives(settings):
    store = ConfigStore(settings)

    store.update({"sample_rate": 0.5})
    store.refresh()

    assert store.current().sync.sample_rate == 0.5
    assert store.location is None


def test_a_file_shared_by_two_stores_without_a_path_keeps_them_apart(settings):
    file = ConfigFile(None)
    projects = _two_projects(settings)
    support = ConfigStore(projects["support"], storage=file)
    search = ConfigStore(projects["search"], storage=file)

    support.update({"sample_rate": 0.5})
    search.refresh()

    assert search.current().sync.sample_rate == 1.0
    assert file.read() == {"support": {"sample_rate": 0.5}}


def test_a_deployment_reads_storage_once_for_all_its_projects(settings):
    storage = Storage({"support": {"sample_rate": 0.5}, "search": {"schedule_minutes": 30}})

    deployment = Deployment.load(_two_projects(settings), storage)

    assert storage.reads == 1
    assert deployment.store("support").current().sync.sample_rate == 0.5
    assert deployment.store("search").current().sync.schedule_minutes == 30

    storage.saved["search"] = {"schedule_minutes": 5}
    deployment.refresh()

    assert storage.reads == 2
    assert deployment.store("search").current().sync.schedule_minutes == 5


def test_a_deployment_does_not_open_without_its_saved_settings(settings):
    storage = Storage()
    storage.down = ConnectionError("no route to host")

    with pytest.raises(SettingsUnavailable):
        Deployment.load(_two_projects(settings), storage)


def test_refreshing_a_deployment_of_one_store_reads_that_stores_storage(settings):
    storage = Storage()
    deployment = Deployment.of(ConfigStore(settings, storage=storage))
    storage.saved["default"] = {"sample_rate": 0.5}

    deployment.refresh()

    assert deployment.store().current().sync.sample_rate == 0.5


def test_settings_are_kept_in_snowflake_unless_a_file_is_asked_for():
    assert SyncSettings(_env_file=None).store == "snowflake"
    assert SyncSettings(_env_file=None, store="file", config_file="c.json").store == "file"


def test_naming_a_file_without_choosing_the_file_store_is_an_error():
    with pytest.raises(ValidationError, match="SYNC_CONFIG_FILE is only used with SYNC_STORE=file"):
        SyncSettings(_env_file=None, config_file="config.json")
