import json

import pytest

from langfuse_to_snowflake.config import ConfigError, ConfigStore, Settings

REQUIRED = {
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
    "SNOWFLAKE_ACCOUNT": "acct",
    "SNOWFLAKE_USER": "loader",
    "SNOWFLAKE_PRIVATE_KEY_PATH": "rsa_key.p8",
    "SNOWFLAKE_WAREHOUSE": "WH",
    "SNOWFLAKE_DATABASE": "DB",
    "SNOWFLAKE_SCHEMA": "LANGFUSE",
}


@pytest.fixture
def store(settings, tmp_path) -> ConfigStore:
    return ConfigStore(settings, tmp_path / "state" / "config.json")


def test_without_changes_the_environment_applies(store, settings):
    assert store.current() is settings
    assert store.overridden() == []
    assert store.values() == store.defaults()
    assert store.values()["entities"] == list(settings.sync.entities)
    assert not store.path.exists()


def test_a_change_is_validated_applied_and_kept(store, settings):
    updated = store.update(
        {
            "sample_rate": 0.25,
            "filters": ["environment = production", "observations:level!=DEBUG"],
            "entities": ["observations", "scores"],
            "observation_fields": ["usage", "basic", "time", "trace_context"],
        }
    )

    assert updated.sync.sample_rate == 0.25
    # Filters come back in canonical form, field groups in the API's order with core.
    assert updated.sync.filters == ("environment=production", "observations:level!=DEBUG")
    assert updated.langfuse.observation_fields == (
        "core",
        "basic",
        "time",
        "usage",
        "trace_context",
    )
    assert store.current() is updated
    assert store.overridden() == ["entities", "sample_rate", "filters", "observation_fields"]
    # Everything that was not touched, and everything not editable, is as deployed.
    assert updated.sync.lookback_minutes == settings.sync.lookback_minutes
    assert updated.snowflake is settings.snowflake
    assert updated.langfuse.secret_key.get_secret_value() == "sk-test"

    saved = json.loads(store.path.read_text())
    assert saved == {
        "version": 2,
        "projects": {
            "default": {
                "entities": ["observations", "scores"],
                "sample_rate": 0.25,
                "filters": ["environment=production", "observations:level!=DEBUG"],
                "observation_fields": ["core", "basic", "time", "usage", "trace_context"],
            },
        },
    }
    assert list(store.path.parent.iterdir()) == [store.path]


def test_saved_changes_survive_a_restart(store, settings):
    store.update({"schedule_minutes": 30, "reconcile_days": 60})

    reopened = ConfigStore(settings, store.path)

    assert reopened.current().sync.schedule_minutes == 30
    assert reopened.current().sync.reconcile_days == 60
    assert reopened.overridden() == ["schedule_minutes", "reconcile_days"]


def test_changes_build_on_each_other(store):
    store.update({"sample_rate": 0.5})
    store.update({"schedule_minutes": 15})

    assert store.values()["sample_rate"] == 0.5
    assert store.values()["schedule_minutes"] == 15


def test_setting_a_value_back_to_its_default_clears_the_override(store):
    store.update({"sample_rate": 0.5, "schedule_minutes": 15})
    store.update({"sample_rate": 1.0})

    assert store.overridden() == ["schedule_minutes"]
    assert json.loads(store.path.read_text())["projects"]["default"] == {"schedule_minutes": 15}


def test_only_overrides_are_kept_so_untouched_settings_follow_the_environment(
    store, settings, monkeypatch
):
    store.update({"sample_rate": 0.5})

    # Engineering redeploys with a longer lookback.
    redeployed = settings.model_copy(
        update={"sync": settings.sync.model_copy(update={"lookback_minutes": 600})}
    )
    reopened = ConfigStore(redeployed, store.path)

    assert reopened.current().sync.lookback_minutes == 600
    assert reopened.current().sync.sample_rate == 0.5


def test_reset_goes_back_to_the_environment(store, settings):
    store.update({"sample_rate": 0.5})

    assert store.reset() is settings
    assert store.overridden() == []
    assert json.loads(store.path.read_text())["projects"] == {"default": {}}


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"sample_rate": 0}, "sample_rate", "greater than 0"),
        ({"sample_rate": 1.5}, "sample_rate", "less than or equal to 1"),
        ({"entities": []}, "entities", "at least one entity"),
        ({"entities": ["prompts"]}, "entities", "unknown entities prompts"),
        ({"filters": ["just some words"]}, "filters", "cannot parse filter"),
        ({"schedule_minutes": -5}, "schedule_minutes", "greater than or equal to 0"),
        (
            {"observation_fields": ["core", "everything"]},
            "observation_fields",
            "unknown field group",
        ),
        # Problems that only show once the settings are taken together.
        ({"observation_fields": ["core", "basic"]}, "", "trace_context"),
        ({"filters": ["traces:userId=alice"]}, "", "traces are derived from observations"),
    ],
)
def test_invalid_changes_are_rejected_and_nothing_is_changed(
    store, settings, changes, field, message
):
    with pytest.raises(ConfigError) as error:
        store.update(changes)

    assert message in error.value.problems[field]
    assert store.current() is settings
    assert not store.path.exists()


@pytest.mark.parametrize("name", ["api_version", "table_prefix", "page_size", "secret_key", "nope"])
def test_deployment_settings_cannot_be_changed(store, name):
    with pytest.raises(ConfigError, match="cannot be changed here") as error:
        store.update({name: "x"})

    assert error.value.problems == {name: "not an editable setting"}


def test_without_a_file_changes_apply_but_are_not_kept(settings):
    store = ConfigStore(settings)

    store.update({"sample_rate": 0.5})

    assert store.path is None
    assert store.current().sync.sample_rate == 0.5
    assert ConfigStore(settings).current().sync.sample_rate == 1.0


def test_a_broken_file_is_an_error_rather_than_silently_ignored(settings, tmp_path):
    path = tmp_path / "config.json"

    path.write_text("{not json")
    with pytest.raises(ConfigError, match="config.json cannot be read"):
        ConfigStore(settings, path)

    path.write_text(json.dumps({"version": 1, "overrides": {"sample_rate": 7}}))
    with pytest.raises(ConfigError, match="sample_rate"):
        ConfigStore(settings, path)


def test_settings_that_are_no_longer_editable_are_ignored_on_load(settings, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "overrides": {"retired": 1, "sample_rate": 0.5}}))

    assert ConfigStore(settings, path).overridden() == ["sample_rate"]


def test_open_reads_the_environment_and_the_file_it_names(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "overrides": {"sample_rate": 0.1}}))
    for name, value in {
        **REQUIRED,
        "SYNC_STORE": "file",
        "SYNC_CONFIG_FILE": str(path),
        "SYNC_SAMPLE_RATE": "0.9",
    }.items():
        monkeypatch.setenv(name, value)

    store = ConfigStore.open(env_file=None)

    assert store.path == path
    # The saved change wins over the environment; the environment is the default.
    assert store.current().sync.sample_rate == 0.1
    assert store.defaults()["sample_rate"] == 0.9
    assert Settings.load(env_file=None).sync.sample_rate == 0.9
