"""Writing to Snowflake: schema, merged loads and sync state.

Snowpark takes a couple of seconds to import, so the modules built on it are
only loaded when one of their classes is asked for.
"""

from typing import TYPE_CHECKING, Any

from .auth import load_private_key
from .errors import SchemaConflictError
from .models import EntityState, Key, LoadResult
from .session import Catalog, Sessions

if TYPE_CHECKING:
    from .adapter import SnowflakeAdapter
    from .runs_table import RunsTable
    from .settings_table import SettingsTable

_LAZY = {
    "SnowflakeAdapter": "adapter",
    "RunsTable": "runs_table",
    "SettingsTable": "settings_table",
}

__all__ = [
    "Catalog",
    "EntityState",
    "Key",
    "LoadResult",
    "RunsTable",
    "SchemaConflictError",
    "Sessions",
    "SettingsTable",
    "SnowflakeAdapter",
    "load_private_key",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from importlib import import_module

        return getattr(import_module(f".{_LAZY[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
