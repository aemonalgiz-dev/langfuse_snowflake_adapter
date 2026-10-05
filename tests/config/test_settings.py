import pytest

from langfuse_to_snowflake.config import ConfigError, LangfuseSettings, Settings, SyncSettings

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


def _set(monkeypatch, **overrides):
    for name, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_defaults(monkeypatch):
    _set(monkeypatch)

    settings = Settings.load(env_file=None)

    assert settings.langfuse.host == "https://cloud.langfuse.com"
    assert settings.langfuse.api_version == "v4"
    assert settings.langfuse.secret_key.get_secret_value() == "sk-test"
    assert settings.snowflake.schema_name == "LANGFUSE"
    assert settings.snowflake.max_retries == 2
    assert settings.sync.entities == (
        "observations",
        "scores",
        "traces",
        "sessions",
        "comments",
        "annotation_queues",
        "annotation_queue_items",
    )
    assert settings.sync.check_deletions is True
    assert settings.sync.table_prefix == "LANGFUSE_"
    assert settings.sync.sample_rate == 1.0
    assert settings.sync.filters == ()
    assert settings.sync.reconcile_days == 30
    assert settings.sync.reconcile_every_hours == 24
    assert settings.sync.reconcile_full is False
    assert (settings.api.host, settings.api.port, settings.api.key) == ("127.0.0.1", 8000, None)


def test_reconcile_settings(monkeypatch):
    _set(
        monkeypatch,
        SYNC_RECONCILE_DAYS="90",
        SYNC_RECONCILE_EVERY_HOURS="0",
        SYNC_RECONCILE_FULL="true",
        SYNC_CHECK_DELETIONS="false",
    )

    settings = Settings.load(env_file=None)

    assert settings.sync.reconcile_days == 90
    assert settings.sync.reconcile_every_hours == 0
    assert settings.sync.reconcile_full is True
    assert settings.sync.check_deletions is False


def test_environment_overrides(monkeypatch):
    _set(
        monkeypatch,
        LANGFUSE_BASE_URL="https://us.cloud.langfuse.com",
        LANGFUSE_API_VERSION="v3",
        LANGFUSE_OBSERVATION_FIELDS="usage, basic",
        LANGFUSE_EXPAND_METADATA="customer,ticket",
        SNOWFLAKE_MAX_RETRIES="5",
        SYNC_ENTITIES="scores, observations",
        SYNC_TABLE_PREFIX="lf_",
        SYNC_API_KEY="s3cret",
    )

    settings = Settings.load(env_file=None)

    assert settings.langfuse.host == "https://us.cloud.langfuse.com"
    assert settings.langfuse.api_version == "v3"
    # core is always requested, and groups keep the API's order.
    assert settings.langfuse.observation_fields == ("core", "basic", "usage")
    assert settings.langfuse.expand_metadata == ("customer", "ticket")
    assert settings.snowflake.max_retries == 5
    assert settings.sync.entities == ("scores", "observations")
    assert settings.sync.table_prefix == "LF_"
    assert settings.api.key.get_secret_value() == "s3cret"


def test_sampling_and_filters(monkeypatch):
    _set(
        monkeypatch,
        SYNC_SAMPLE_RATE="0.25",
        SYNC_FILTERS="environment = production,staging ; observations:level!=DEBUG;",
    )

    settings = Settings.load(env_file=None)

    assert settings.sync.sample_rate == 0.25
    # Filters are validated and stored in canonical form.
    assert settings.sync.filters == (
        "environment=production,staging",
        "observations:level!=DEBUG",
    )


def test_settings_are_read_from_an_env_file(tmp_path):
    env_file = tmp_path / "custom.env"
    lines = [f"{name}={value}" for name, value in REQUIRED.items()]
    lines.append("SYNC_FILTERS=scores:name=accuracy;observations:totalCost>=0.01")
    env_file.write_text("\n".join(lines))

    settings = Settings.load(env_file=env_file)

    assert settings.langfuse.public_key == "pk-test"
    assert settings.snowflake.database == "DB"
    assert settings.sync.filters == ("scores:name=accuracy", "observations:totalCost>=0.01")


def test_secrets_can_be_mounted_as_files(monkeypatch, tmp_path):
    """As Docker and Kubernetes deliver them: one file per secret, named after the setting."""
    _set(monkeypatch)
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "SNOWFLAKE_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name)
    pem = "-----BEGIN PRIVATE KEY-----\nMIIB\nAAAA\n-----END PRIVATE KEY-----"
    secrets = {
        "langfuse_public_key": "pk-from-file",
        "langfuse_secret_key": "sk-from-file\n",  # a trailing newline is the norm
        "snowflake_private_key": pem + "\n",
        "sync_api_key": "ui-key-from-file",
        "unrelated_secret": "ignored",
    }
    for filename, content in secrets.items():
        (tmp_path / filename).write_text(content)
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))

    settings = Settings.load(env_file=None)

    assert settings.langfuse.public_key == "pk-from-file"
    assert settings.langfuse.secret_key.get_secret_value() == "sk-from-file"
    assert settings.snowflake.private_key.get_secret_value() == pem
    assert settings.snowflake.private_key_path is None
    assert settings.api.key.get_secret_value() == "ui-key-from-file"
    # Settings that are not secret still come from the environment.
    assert settings.snowflake.account == "acct"


def test_an_environment_variable_wins_over_a_secret_file(monkeypatch, tmp_path):
    _set(monkeypatch, LANGFUSE_SECRET_KEY="sk-from-env")
    (tmp_path / "langfuse_secret_key").write_text("sk-from-file")
    monkeypatch.setenv("SYNC_SECRETS_DIR", str(tmp_path))

    assert Settings.load(env_file=None).langfuse.secret_key.get_secret_value() == "sk-from-env"


def test_a_missing_secrets_directory_is_not_an_error(monkeypatch, tmp_path):
    _set(monkeypatch, SYNC_SECRETS_DIR=str(tmp_path / "nowhere"))

    assert Settings.load(env_file=None).langfuse.public_key == "pk-test"


def test_missing_settings_are_named_by_environment_variable(monkeypatch):
    _set(monkeypatch)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    monkeypatch.delenv("SNOWFLAKE_SCHEMA")

    with pytest.raises(ConfigError, match="LANGFUSE_SECRET_KEY: Field required"):
        Settings.load(env_file=None)

    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    with pytest.raises(ConfigError, match="SNOWFLAKE_SCHEMA: Field required"):
        Settings.load(env_file=None)


def test_exactly_one_private_key_source(monkeypatch):
    _set(monkeypatch, SNOWFLAKE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----")
    with pytest.raises(ConfigError, match="exactly one of SNOWFLAKE_PRIVATE_KEY_PATH"):
        Settings.load(env_file=None)

    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY")
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_PATH")
    with pytest.raises(ConfigError, match="exactly one of SNOWFLAKE_PRIVATE_KEY_PATH"):
        Settings.load(env_file=None)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SYNC_ENTITIES", "scores,prompts", "SYNC_ENTITIES: .*unknown entities prompts"),
        ("SYNC_ENTITIES", "", "SYNC_ENTITIES: .*at least one"),
        ("SYNC_TABLE_PREFIX", "lf-", "SYNC_TABLE_PREFIX: .*table prefix"),
        ("LANGFUSE_API_VERSION", "v2", "LANGFUSE_API_VERSION"),
        ("LANGFUSE_OBSERVATION_FIELDS", "core,everything", "unknown field group"),
        ("SYNC_WINDOW_HOURS", "0", "SYNC_WINDOW_HOURS"),
        ("SYNC_RECONCILE_DAYS", "0", "SYNC_RECONCILE_DAYS"),
        ("SYNC_RECONCILE_EVERY_HOURS", "-1", "SYNC_RECONCILE_EVERY_HOURS"),
        ("SYNC_SAMPLE_RATE", "0", "SYNC_SAMPLE_RATE"),
        ("SYNC_SAMPLE_RATE", "1.5", "SYNC_SAMPLE_RATE"),
        ("SYNC_FILTERS", "just some words", "SYNC_FILTERS: .*cannot parse filter"),
        ("SYNC_FILTERS", "prompts:name=x", "SYNC_FILTERS: .*unknown entity 'prompts'"),
    ],
)
def test_invalid_values_are_rejected(monkeypatch, name, value, message):
    _set(monkeypatch, **{name: value})

    with pytest.raises(ConfigError, match=message):
        Settings.load(env_file=None)


def test_secrets_are_not_shown_in_repr():
    settings = LangfuseSettings(_env_file=None, public_key="pk-test", secret_key="sk-very-secret")

    assert "sk-very-secret" not in repr(settings)


def test_settings_accept_field_names_directly():
    assert SyncSettings(_env_file=None, entities="scores").entities == ("scores",)
    assert SyncSettings(_env_file=None, entities=("traces",)).entities == ("traces",)
    assert SyncSettings(_env_file=None, filters=["scores:name=a"]).filters == ("scores:name=a",)
