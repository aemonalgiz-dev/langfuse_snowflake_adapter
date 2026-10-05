"""Names of the Snowflake objects each entity maps to."""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_prefix(prefix: str) -> str:
    """Prefixes become part of object names, so only plain identifiers are allowed."""
    if prefix and not _IDENTIFIER.match(prefix):
        raise ValueError(
            f"table prefix {prefix!r} must be empty or contain only letters, digits, _ and $, "
            "and not start with a digit"
        )
    return prefix.upper()


def table_name(prefix: str, entity: str) -> str:
    return f"{prefix}{entity}".upper()


def state_table_name(prefix: str) -> str:
    return f"{prefix}SYNC_STATE".upper()


def settings_table_name(prefix: str) -> str:
    return f"{prefix}SYNC_SETTINGS".upper()


def runs_table_name(prefix: str) -> str:
    return f"{prefix}SYNC_RUNS".upper()
