"""Settings that can be changed while the service runs.

The environment provides the defaults. What a team changes through the web app
is kept as a small set of overrides on top of them, per project, validated like
any other setting and saved so that it survives a restart: in Snowflake, next
to the data, or in a file.

The split follows who owns what. Engineering deploys the service with the
projects, their credentials, the connection details and the Langfuse API
version in the environment; none of that is editable here. The data team then
decides, for each project, what is synced and how: entities, filters,
sampling, fields and freshness.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..entities import build_plan
from ..selection import Selection
from .settings import (
    DEFAULT_PROJECT,
    ApiSettings,
    ConfigError,
    LangfuseSettings,
    Settings,
    SyncSettings,
)

EDITABLE: Mapping[str, tuple[str, ...]] = {
    "sync": (
        "entities",
        "sample_rate",
        "filters",
        "exclude_fields",
        "schedule_minutes",
        "lookback_minutes",
        "initial_backfill_days",
        "window_hours",
        "reconcile_every_hours",
        "reconcile_days",
        "reconcile_full",
        "check_deletions",
    ),
    "langfuse": ("observation_fields", "expand_metadata"),
}
FIELDS: tuple[str, ...] = EDITABLE["sync"] + EDITABLE["langfuse"]

_FILE_VERSION = 2


def _value(settings: Settings, name: str) -> Any:
    section = settings.sync if name in EDITABLE["sync"] else settings.langfuse
    value = getattr(section, name)
    return list(value) if isinstance(value, tuple) else value


class SettingsUnavailable(ConfigError):
    """The saved settings cannot be reached, so it is not known what should be synced."""


class SettingsStorage(Protocol):
    """Where every project's saved changes are kept. One is shared by all stores."""

    @property
    def location(self) -> str | None:
        """Where that is, in words. None if changes are not kept at all."""

    def read(self) -> dict[str, dict[str, Any]]:
        """Saved changes by project. Empty if nothing was saved yet."""

    def write(self, project: str, overrides: Mapping[str, Any]) -> None:
        """Replace one project's saved changes, leaving the others as they are."""


class ConfigFile:
    """A file holding every project's saved changes. Without a path, nothing is kept."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        # Stands in for the file when there is none: lasts as long as the process.
        self._memory: dict[str, dict[str, Any]] = {}

    @property
    def location(self) -> str | None:
        return str(self.path) if self.path is not None else None

    def read(self) -> dict[str, dict[str, Any]]:
        """Saved changes by project. Empty if nothing was saved yet."""
        with self._lock:
            return self._read()

    def write(self, project: str, overrides: Mapping[str, Any]) -> None:
        """Replace one project's saved changes, leaving the others as they are."""
        if self.path is None:
            with self._lock:
                self._memory[project] = dict(overrides)
            return
        with self._lock:
            projects = self._read()
            projects[project] = dict(overrides)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            document = json.dumps({"version": _FILE_VERSION, "projects": projects}, indent=2)
            # Written beside the target and moved into place, so that a crash
            # midway cannot leave half a file behind.
            handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    file.write(document + "\n")
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

    def _read(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return {project: dict(overrides) for project, overrides in self._memory.items()}
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            # Before projects existed the file held one set of overrides.
            projects = (
                document["projects"]
                if "projects" in document
                else {DEFAULT_PROJECT: document["overrides"]}
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"{self.path} cannot be read ({exc})") from exc
        if not isinstance(projects, dict) or not all(
            isinstance(overrides, dict) for overrides in projects.values()
        ):
            raise ConfigError(f"{self.path} does not hold settings per project")
        return projects


class ConfigStore:
    """One project's settings: the environment's, plus what was changed for it."""

    def __init__(
        self,
        base: Settings,
        path: Path | None = None,
        *,
        storage: SettingsStorage | None = None,
        saved: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """``saved`` is what was already read from ``storage``, to spare reading it again."""
        self._base = base
        self._storage = storage or ConfigFile(path)
        self._lock = threading.Lock()
        self._current = base
        self.apply(_read(self._storage) if saved is None else saved)

    @classmethod
    def open(cls, env_file: str | Path | None = ".env") -> ConfigStore:
        """The only project's settings, with any changes saved to SYNC_CONFIG_FILE applied."""
        base = Settings.load(env_file)
        return cls(base, base.sync.config_file)

    @property
    def project(self) -> str:
        return self._base.langfuse.name

    @property
    def location(self) -> str | None:
        """Where changes are saved, in words. None if they only last until a restart."""
        return self._storage.location

    @property
    def path(self) -> Path | None:
        """The file changes are saved to, if it is a file."""
        return getattr(self._storage, "path", None)

    def current(self) -> Settings:
        with self._lock:
            return self._current

    def values(self) -> dict[str, Any]:
        """The editable settings as they are in effect."""
        current = self.current()
        return {name: _value(current, name) for name in FIELDS}

    def defaults(self) -> dict[str, Any]:
        """The editable settings as the environment gives them."""
        return {name: _value(self._base, name) for name in FIELDS}

    def overridden(self) -> list[str]:
        values, defaults = self.values(), self.defaults()
        return [name for name in FIELDS if values[name] != defaults[name]]

    def apply(self, saved: Mapping[str, Mapping[str, Any]]) -> Settings:
        """Take over this project's part of what storage holds for every project.

        Raises ConfigError if what was saved is not a valid configuration.
        """
        own = saved.get(self.project) or {}
        # A setting that is no longer editable is ignored rather than fatal.
        known = {name: value for name, value in own.items() if name in FIELDS}
        try:
            settings = self._build(known) if known else self._base
        except ConfigError as exc:
            raise ConfigError(
                f"{self.location} holds settings for {self.project} that are not valid: {exc}"
            ) from exc
        with self._lock:
            self._current = settings
            return settings

    def refresh(self) -> Settings:
        """Read storage again, so that what was saved elsewhere takes effect here."""
        return self.apply(_read(self._storage))

    def update(self, changes: Mapping[str, Any]) -> Settings:
        """Apply changes to the editable settings and keep them.

        Raises ConfigError, with the problem per setting, if the result would
        not be a valid configuration; nothing is changed in that case. Nothing
        is changed either if the result cannot be saved.
        """
        unknown = sorted(set(changes) - set(FIELDS))
        if unknown:
            raise ConfigError(
                f"{', '.join(unknown)} cannot be changed here",
                dict.fromkeys(unknown, "not an editable setting"),
            )
        with self._lock:
            settings = self._build({**self._overrides(self._current), **changes})
            _write(self._storage, self.project, self._overrides(settings))
            self._current = settings
            return settings

    def reset(self) -> Settings:
        """Drop every saved change and go back to what the environment gives."""
        with self._lock:
            _write(self._storage, self.project, {})
            self._current = self._base
            return self._current

    @property
    def keeps_history(self) -> bool:
        return hasattr(self._storage, "history")

    def history(self, limit: int = 50) -> list[Any] | None:
        """Earlier versions of this project's saved changes, newest first.

        None where storage keeps only the latest.
        """
        if not self.keeps_history:
            return None
        return self._storage.history(self.project, limit)  # type: ignore[attr-defined]

    def _overrides(self, settings: Settings) -> dict[str, Any]:
        """Only what differs from the environment is kept, so that a change to
        the environment still reaches every setting nobody has touched."""
        return {
            name: value
            for name in FIELDS
            if (value := _value(settings, name)) != _value(self._base, name)
        }

    def _build(self, overrides: Mapping[str, Any]) -> Settings:
        def chosen(section: str) -> dict[str, Any]:
            return {name: overrides[name] for name in EDITABLE[section] if name in overrides}

        try:
            sync = SyncSettings(
                _env_file=None, **{**self._base.sync.model_dump(), **chosen("sync")}
            )
            langfuse = LangfuseSettings(
                **{**self._base.langfuse.model_dump(), **chosen("langfuse")}
            )
        except ValidationError as exc:
            problems = {
                ".".join(str(part) for part in error["loc"]): error["msg"].removeprefix(
                    "Value error, "
                )
                for error in exc.errors(include_input=False, include_url=False)
            }
            summary = "; ".join(f"{name}: {message}" for name, message in problems.items())
            raise ConfigError(summary, problems) from exc

        try:
            # What only shows up when the settings are taken together: a view
            # without the field groups it is built from, a filter on a view.
            build_plan(
                langfuse.api_version,
                sync.entities,
                observation_fields=langfuse.observation_fields,
                expand_metadata=langfuse.expand_metadata,
            )
            Selection.parse(sync.sample_rate, sync.filters, sync.exclude_fields).check(
                langfuse.api_version
            )
        except ValueError as exc:
            raise ConfigError(str(exc), {"": str(exc)}) from exc
        return self._base.model_copy(update={"sync": sync, "langfuse": langfuse})


def _read(storage: SettingsStorage) -> dict[str, dict[str, Any]]:
    try:
        return storage.read()
    except ConfigError:
        raise
    except Exception as exc:
        raise SettingsUnavailable(
            f"The saved settings could not be read from {storage.location} "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _write(storage: SettingsStorage, project: str, overrides: Mapping[str, Any]) -> None:
    try:
        storage.write(project, overrides)
    except ConfigError:
        raise
    except Exception as exc:
        raise SettingsUnavailable(
            f"The change could not be saved to {storage.location} ({type(exc).__name__}: {exc})"
        ) from exc


class Deployment:
    """Every project this service syncs, each with its own settings."""

    def __init__(
        self,
        stores: Mapping[str, ConfigStore],
        api: ApiSettings,
        storage: SettingsStorage | None = None,
    ) -> None:
        if not stores:
            raise ConfigError("No project is configured")
        self._stores = dict(stores)
        self._storage = storage
        self.api = api

    @classmethod
    def load(cls, projects: Mapping[str, Settings], storage: SettingsStorage) -> Deployment:
        """The given projects, with what ``storage`` holds for each applied.

        Raises SettingsUnavailable if storage cannot be read: without the
        saved settings it is not known what may be synced.
        """
        if not projects:
            raise ConfigError("No project is configured")
        saved = _read(storage)
        stores = {
            name: ConfigStore(settings, storage=storage, saved=saved)
            for name, settings in projects.items()
        }
        return cls(stores, next(iter(projects.values())).api, storage)

    @classmethod
    def open(cls, env_file: str | Path | None = ".env") -> Deployment:
        """The projects the environment declares, with changes saved to SYNC_CONFIG_FILE applied.

        This never reaches Snowflake. A running service is opened with
        ``open_runtime``, which also follows SYNC_STORE.
        """
        projects = Settings.load_all(env_file)
        first = next(iter(projects.values()))
        return cls.load(projects, ConfigFile(first.sync.config_file))

    @classmethod
    def of(cls, store: ConfigStore) -> Deployment:
        """A deployment of one project."""
        return cls({store.project: store}, store.current().api)

    @property
    def names(self) -> list[str]:
        return list(self._stores)

    def refresh(self) -> None:
        """Read what was saved again, for every project at once."""
        if self._storage is None:
            for store in self._stores.values():
                store.refresh()
            return
        saved = _read(self._storage)
        for store in self._stores.values():
            store.apply(saved)

    def store(self, project: str | None = None) -> ConfigStore:
        """One project's settings. The name may be left out when there is only one."""
        if project is None:
            if len(self._stores) != 1:
                raise ConfigError(
                    f"Several projects are configured; choose one of {', '.join(self._stores)}"
                )
            return next(iter(self._stores.values()))
        try:
            return self._stores[project]
        except KeyError:
            raise ConfigError(
                f"No project named {project!r}; there is {', '.join(self._stores)}"
            ) from None

    def stores(self, project: str | None = None) -> list[ConfigStore]:
        """The named project, or all of them."""
        return [self.store(project)] if project else list(self._stores.values())
