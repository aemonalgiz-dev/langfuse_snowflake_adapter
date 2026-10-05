"""Several Langfuse projects in one deployment, each with its own keys."""

import json

import pytest

from langfuse_to_snowflake.config import (
    ConfigError,
    ConfigStore,
    Deployment,
    Settings,
    load_projects,
)

SHARED = {
    "SNOWFLAKE_ACCOUNT": "acct",
    "SNOWFLAKE_USER": "loader",
    "SNOWFLAKE_PRIVATE_KEY_PATH": "rsa_key.p8",
    "SNOWFLAKE_WAREHOUSE": "WH",
    "SNOWFLAKE_DATABASE": "DB",
    "SNOWFLAKE_SCHEMA": "LANGFUSE",
}
TWO = {
    **SHARED,
    "LANGFUSE_PROJECTS": "support, search",
    "LANGFUSE_SUPPORT_PUBLIC_KEY": "pk-support",
    "LANGFUSE_SUPPORT_SECRET_KEY": "sk-support",
    "LANGFUSE_SEARCH_PUBLIC_KEY": "pk-search",
    "LANGFUSE_SEARCH_SECRET_KEY": "sk-search",
}


def _set(monkeypatch, values):
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_without_a_list_there_is_one_project_called_default(monkeypatch):
    _set(monkeypatch, {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})

    projects = load_projects(env_file=None)

    assert list(projects) == ["default"]
    assert (projects["default"].name, projects["default"].public_key) == ("default", "pk")


def test_each_project_has_its_own_keys(monkeypatch):
    _set(monkeypatch, TWO)

    projects = load_projects(env_file=None)

    assert list(projects) == ["support", "search"]
    assert projects["support"].public_key == "pk-support"
    assert projects["support"].secret_key.get_secret_value() == "sk-support"
    assert projects["search"].public_key == "pk-search"
    assert projects["search"].name == "search"


def test_other_settings_fall_back_to_the_shared_ones(monkeypatch):
    _set(
        monkeypatch,
        {
            **TWO,
            "LANGFUSE_HOST": "https://us.cloud.langfuse.com",
            "LANGFUSE_MAX_RETRIES": "9",
            "LANGFUSE_SEARCH_HOST": "https://langfuse.internal.example",
            "LANGFUSE_SEARCH_API_VERSION": "v3",
        },
    )

    projects = load_projects(env_file=None)

    assert projects["support"].host == "https://us.cloud.langfuse.com"
    assert (projects["support"].api_version, projects["support"].max_retries) == ("v4", 9)
    # A project's own value wins; what it does not set is shared.
    assert projects["search"].host == "https://langfuse.internal.example"
    assert (projects["search"].api_version, projects["search"].max_retries) == ("v3", 9)


def test_keys_never_fall_back_to_the_shared_ones(monkeypatch):
    """Two projects on one key pair would be the same Langfuse project twice."""
    _set(
        monkeypatch,
        {**TWO, "LANGFUSE_PUBLIC_KEY": "pk-shared", "LANGFUSE_SECRET_KEY": "sk-shared"},
    )
    monkeypatch.delenv("LANGFUSE_SEARCH_SECRET_KEY")

    with pytest.raises(ConfigError, match="LANGFUSE_SEARCH_SECRET_KEY: Field required"):
        load_projects(env_file=None)


def test_a_projects_keys_can_be_mounted_as_secret_files(monkeypatch, tmp_path):
    _set(monkeypatch, {**SHARED, "LANGFUSE_PROJECTS": "support,search"})
    for filename, value in {
        "langfuse_support_public_key": "pk-support-file",
        "langfuse_support_secret_key": "sk-support-file\n",
        "langfuse_search_public_key": "pk-search-file",
        "langfuse_search_secret_key": "sk-search-file",
    }.items():
        (tmp_path / filename).write_text(value)
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))

    projects = load_projects(env_file=None)

    assert projects["support"].secret_key.get_secret_value() == "sk-support-file"
    assert projects["search"].public_key == "pk-search-file"


@pytest.mark.parametrize("names", ["Support", "1st", "my-project", "a,a", "a b"])
def test_project_names_must_be_plain_and_unique(monkeypatch, names):
    _set(monkeypatch, {**SHARED, "LANGFUSE_PROJECTS": names})

    with pytest.raises(ConfigError, match="LANGFUSE_PROJECTS: names must be unique, lower case"):
        load_projects(env_file=None)


def test_a_bad_value_is_named_by_its_project_variable(monkeypatch):
    _set(monkeypatch, {**TWO, "LANGFUSE_SEARCH_API_VERSION": "v2"})

    with pytest.raises(ConfigError, match="LANGFUSE_SEARCH_API_VERSION"):
        load_projects(env_file=None)


def test_settings_load_all_shares_snowflake_and_defaults(monkeypatch):
    _set(monkeypatch, {**TWO, "SYNC_SAMPLE_RATE": "0.5"})

    projects = Settings.load_all(env_file=None)

    assert list(projects) == ["support", "search"]
    assert projects["support"].snowflake is projects["search"].snowflake
    assert projects["support"].sync.sample_rate == projects["search"].sync.sample_rate == 0.5
    assert projects["search"].langfuse.public_key == "pk-search"
    # Asking for "the" settings is ambiguous with several projects.
    with pytest.raises(ConfigError, match="Several projects are configured"):
        Settings.load(env_file=None)


def test_each_project_keeps_its_own_changes_in_the_one_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    _set(monkeypatch, {**TWO, "SYNC_STORE": "file", "SYNC_CONFIG_FILE": str(path)})
    deployment = Deployment.open(env_file=None)

    deployment.store("support").update({"sample_rate": 0.1, "filters": ["environment=production"]})
    deployment.store("search").update({"exclude_fields": ["observations:input"]})

    assert deployment.names == ["support", "search"]
    assert deployment.store("support").current().sync.sample_rate == 0.1
    assert deployment.store("search").current().sync.sample_rate == 1.0
    assert deployment.store("search").current().sync.exclude_fields == ("observations:input",)
    assert json.loads(path.read_text()) == {
        "version": 2,
        "projects": {
            "support": {"sample_rate": 0.1, "filters": ["environment=production"]},
            "search": {"exclude_fields": ["observations:input"]},
        },
    }

    # After a restart each project gets its own changes back, and only its own.
    reopened = Deployment.open(env_file=None)
    assert reopened.store("support").overridden() == ["sample_rate", "filters"]
    assert reopened.store("search").overridden() == ["exclude_fields"]
    # Resetting one leaves the other alone.
    reopened.store("support").reset()
    assert Deployment.open(env_file=None).store("search").overridden() == ["exclude_fields"]


def test_a_project_must_be_named_when_there_are_several(monkeypatch):
    _set(monkeypatch, TWO)
    deployment = Deployment.open(env_file=None)

    with pytest.raises(ConfigError, match="choose one of support, search"):
        deployment.store()
    with pytest.raises(ConfigError, match="No project named 'billing'"):
        deployment.store("billing")
    assert [store.project for store in deployment.stores()] == ["support", "search"]
    assert [store.project for store in deployment.stores("search")] == ["search"]


def test_a_single_project_need_not_be_named(settings):
    deployment = Deployment.of(ConfigStore(settings))

    assert deployment.names == ["default"]
    assert deployment.store().project == "default"


def test_a_file_from_before_projects_existed_belongs_to_the_default_project(settings, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "overrides": {"sample_rate": 0.3}}))

    store = ConfigStore(settings, path)

    assert store.current().sync.sample_rate == 0.3
    store.update({"schedule_minutes": 10})
    assert json.loads(path.read_text()) == {
        "version": 2,
        "projects": {"default": {"sample_rate": 0.3, "schedule_minutes": 10}},
    }
