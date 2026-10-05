"""Settings classes, one per group of environment variables."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ..entities import ENTITY_NAMES, OBSERVATION_FIELD_GROUPS, ApiVersion, validate_prefix
from ..selection import parse_field, parse_filter

# Comma-separated in the environment, e.g. SYNC_ENTITIES=observations,scores
CsvTuple = Annotated[tuple[str, ...], NoDecode]

_Section = TypeVar("_Section", bound=BaseSettings)


class ConfigError(Exception):
    """Configuration is missing or invalid.

    ``problems`` maps each offending setting to what is wrong with it; a
    problem that is not about one setting is filed under the empty string.
    """

    def __init__(self, message: str, problems: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or {}


def _split(value: object, separators: str = ",") -> object:
    if isinstance(value, str):
        parts = re.split(f"[{re.escape(separators)}]", value)
        return tuple(part.strip() for part in parts if part.strip())
    return value


def _settings_config(env_prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


DEFAULT_PROJECT = "default"
_PROJECT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class LangfuseSettings(BaseModel):
    """One Langfuse project: where it is, its API keys and how to read it.

    Built by ``load_projects`` from the environment; see there for the
    variable names.
    """

    # The name engineering gave the project. It labels the project in the web
    # app and selects its variables; Langfuse's own project ID is looked up.
    name: str = DEFAULT_PROJECT
    host: str = "https://cloud.langfuse.com"
    public_key: str
    secret_key: SecretStr
    api_version: ApiVersion = "v4"
    page_size: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=6, ge=0)
    observation_fields: tuple[str, ...] = OBSERVATION_FIELD_GROUPS
    expand_metadata: tuple[str, ...] = ()

    @field_validator("observation_fields", "expand_metadata", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split(value)

    @field_validator("observation_fields")
    @classmethod
    def _known_field_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(OBSERVATION_FIELD_GROUPS))
        if unknown:
            raise ValueError(
                f"unknown field group(s) {', '.join(unknown)}; "
                f"choose from {', '.join(OBSERVATION_FIELD_GROUPS)}"
            )
        # The API always returns core; keep the list explicit and ordered.
        return tuple(g for g in OBSERVATION_FIELD_GROUPS if g == "core" or g in value)


class _LangfuseEnvironment(BaseSettings):
    """What the environment holds for Langfuse under one prefix. Nothing is required here."""

    model_config = _settings_config("LANGFUSE_")

    # Only read without a project prefix: the names of the projects to sync.
    projects: CsvTuple = ()
    host: str | None = None
    base_url: str | None = None
    public_key: str | None = None
    secret_key: SecretStr | None = None
    api_version: str | None = None
    page_size: int | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    # Left as text; LangfuseSettings splits and checks them.
    observation_fields: str | None = None
    expand_metadata: str | None = None

    @field_validator("projects", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split(value)


def _langfuse_environment(prefix: str, env_file: str | Path | None) -> dict[str, object]:
    reader = type(
        "_Environment", (_LangfuseEnvironment,), {"model_config": _settings_config(prefix)}
    )
    try:
        loaded = reader(_env_file=env_file, _secrets_dir=secrets_dir())
    except ValidationError as exc:
        raise ConfigError(_describe(exc, prefix)) from exc
    values = loaded.model_dump(exclude_none=True)
    # LANGFUSE_BASE_URL is the SDK's newer name for the host.
    base_url = values.pop("base_url", None)
    if base_url and "host" not in values:
        values["host"] = base_url
    return values


def load_projects(env_file: str | Path | None = ".env") -> dict[str, LangfuseSettings]:
    """The Langfuse projects this deployment syncs, by name.

    With ``LANGFUSE_PROJECTS=support,search`` each project takes its settings
    from ``LANGFUSE_<NAME>_*``, most importantly its own
    ``LANGFUSE_SUPPORT_PUBLIC_KEY`` and ``LANGFUSE_SUPPORT_SECRET_KEY``.
    Anything else a project does not set itself, such as the host, falls back
    to the unprefixed ``LANGFUSE_*`` variable. Keys never fall back: two
    projects sharing one key pair would be the same project twice.

    Without ``LANGFUSE_PROJECTS`` there is one project, read from ``LANGFUSE_*``.
    """
    shared = _langfuse_environment("LANGFUSE_", env_file)
    names = shared.pop("projects", ())
    if not names:
        return {DEFAULT_PROJECT: _project(DEFAULT_PROJECT, shared, "LANGFUSE_")}

    invalid = [name for name in names if not _PROJECT_NAME.match(name)]
    if invalid or len(set(names)) != len(names):
        raise ConfigError(
            "Invalid configuration:\n  LANGFUSE_PROJECTS: names must be unique, lower case, "
            f"start with a letter and use only letters, digits and _ (got {', '.join(names)})"
        )
    defaults = {k: v for k, v in shared.items() if k not in ("public_key", "secret_key")}
    projects = {}
    for name in names:
        prefix = f"LANGFUSE_{name.upper()}_"
        own = _langfuse_environment(prefix, env_file)
        own.pop("projects", None)
        projects[name] = _project(name, {**defaults, **own}, prefix)
    return projects


def _project(name: str, values: dict[str, object], prefix: str) -> LangfuseSettings:
    try:
        return LangfuseSettings(name=name, **values)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, prefix)) from exc


class SnowflakeSettings(BaseSettings):
    model_config = _settings_config("SNOWFLAKE_")

    account: str
    user: str
    private_key_path: Path | None = None
    private_key: SecretStr | None = None
    private_key_passphrase: SecretStr | None = None
    role: str | None = None
    warehouse: str
    database: str
    # "schema" would shadow a BaseModel attribute.
    schema_name: str = Field(validation_alias="SNOWFLAKE_SCHEMA")
    max_retries: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def _one_private_key(self) -> SnowflakeSettings:
        if (self.private_key_path is None) == (self.private_key is None):
            raise ValueError(
                "set exactly one of SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PRIVATE_KEY"
            )
        return self


class SyncSettings(BaseSettings):
    model_config = _settings_config("SYNC_")

    entities: CsvTuple = ENTITY_NAMES
    table_prefix: str = "LANGFUSE_"
    lookback_minutes: int = Field(default=180, ge=0)
    initial_backfill_days: int = Field(default=30, ge=1)
    window_hours: int = Field(default=24, ge=1)
    # Records are sent to Snowflake in batches of at most this many rows and
    # about this many bytes. A batch is held in memory while it is sent.
    batch_max_rows: int = Field(default=20_000, ge=1)
    batch_max_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    # A record larger than this is not loaded but counted as rejected. 16 MB is
    # what a Snowflake row can hold unless the account has been given more.
    record_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    # Share of traces to keep, chosen by a hash of the trace ID.
    sample_rate: float = Field(default=1.0, gt=0, le=1)
    # Semicolon-separated, e.g. environment=production;observations:level!=DEBUG
    filters: Annotated[tuple[str, ...], NoDecode] = ()
    # Fields removed from every record before it is loaded, comma-separated,
    # e.g. observations:input,observations:metadata.email,metadata.internal
    exclude_fields: CsvTuple = ()
    # Reconciliation keeps this many days in step with Langfuse: records that
    # were missed, edited (such as re-annotated scores) or deleted since loading.
    reconcile_days: int = Field(default=30, ge=1)
    # An incremental sync also reconciles once this long has passed. 0 turns that off.
    reconcile_every_hours: int = Field(default=24, ge=0)
    # Always re-read everything instead of comparing a listing first.
    reconcile_full: bool = False
    # Report rows whose record no longer exists in Langfuse. When a filter is
    # active this takes an extra, unfiltered listing of IDs.
    check_deletions: bool = True
    # The HTTP service starts a sync by itself this often, in minutes. 0 turns that off.
    schedule_minutes: int = Field(default=0, ge=0)
    # Where the service keeps what it has to remember: the settings changed
    # through the web app, and the history of runs. "snowflake" keeps both in
    # tables next to the data, so that the service itself holds nothing.
    # "file" keeps the settings in SYNC_CONFIG_FILE and no run history.
    store: Literal["snowflake", "file"] = "snowflake"
    # Only with SYNC_STORE=file. Without a file, changed settings last until
    # the service restarts.
    config_file: Path | None = None

    @field_validator("entities", "exclude_fields", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split(value)

    @field_validator("exclude_fields")
    @classmethod
    def _valid_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(parse_field(text)) for text in value))

    @field_validator("filters", mode="before")
    @classmethod
    def _filter_list(cls, value: object) -> object:
        return _split(value, ";\n")

    @field_validator("entities")
    @classmethod
    def _known_entities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("select at least one entity")
        unknown = sorted(set(value) - set(ENTITY_NAMES))
        if unknown:
            raise ValueError(
                f"unknown entities {', '.join(unknown)}; choose from {', '.join(ENTITY_NAMES)}"
            )
        return value

    @field_validator("filters")
    @classmethod
    def _valid_filters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(parse_filter(text)) for text in value)

    @field_validator("table_prefix")
    @classmethod
    def _valid_prefix(cls, value: str) -> str:
        return validate_prefix(value)

    @model_validator(mode="after")
    def _one_store(self) -> SyncSettings:
        if self.config_file is not None and self.store != "file":
            raise ValueError(
                "SYNC_CONFIG_FILE is only used with SYNC_STORE=file; "
                "set that, or remove the file to keep settings in Snowflake"
            )
        return self


class ApiSettings(BaseSettings):
    model_config = _settings_config("SYNC_API_")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    key: SecretStr | None = None


class Settings(BaseModel):
    """Everything one project's sync needs: its Langfuse project and the shared rest."""

    langfuse: LangfuseSettings
    snowflake: SnowflakeSettings
    sync: SyncSettings
    api: ApiSettings

    @classmethod
    def load_all(cls, env_file: str | Path | None = ".env") -> dict[str, Settings]:
        """The settings of every project, as the environment gives them.

        Projects share the Snowflake connection and start from the same sync
        defaults. What a team changes per project in the web app is layered on
        top by ``ConfigStore``.
        """
        projects = load_projects(env_file)
        snowflake = _load(SnowflakeSettings, env_file)
        sync = _load(SyncSettings, env_file)
        api = _load(ApiSettings, env_file)
        return {
            name: cls(langfuse=langfuse, snowflake=snowflake, sync=sync, api=api)
            for name, langfuse in projects.items()
        }

    @classmethod
    def load(cls, env_file: str | Path | None = ".env") -> Settings:
        """The settings of the only project. Use ``load_all`` when there may be several."""
        projects = cls.load_all(env_file)
        if len(projects) != 1:
            raise ConfigError(
                f"Several projects are configured ({', '.join(projects)}); say which one is meant"
            )
        return next(iter(projects.values()))


def secrets_dir() -> Path | None:
    """Where secrets are mounted as files, one file per setting, if anywhere.

    Docker and Kubernetes both mount secrets as files, by default under
    /run/secrets. A file named after a setting's environment variable, in lower
    case (``langfuse_secret_key``, ``snowflake_private_key``, ``sync_api_key``),
    provides that setting. An environment variable of the same name wins.
    """
    path = Path(os.environ.get("SYNC_SECRETS_DIR", "/run/secrets"))
    return path if path.is_dir() else None


def _load(section: type[_Section], env_file: str | Path | None) -> _Section:
    try:
        return section(_env_file=env_file, _secrets_dir=secrets_dir())
    except ValidationError as exc:
        raise ConfigError(_describe(exc, section.model_config.get("env_prefix", ""))) from exc


def _describe(exc: ValidationError, prefix: str) -> str:
    """Name each problem by the environment variable that would fix it."""
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        name = "_".join(str(part) for part in error["loc"]).upper()
        if name and not name.startswith(prefix):
            name = prefix + name
        problems.append(f"{name}: {error['msg']}" if name else error["msg"])
    return "Invalid configuration:\n  " + "\n  ".join(problems)
