"""Turning a raw record into the typed values of an entity's columns.

Every conversion gives None for a value it cannot read, rather than failing:
one odd field must not keep a record out of the warehouse. The record itself
is always loaded whole, so nothing is lost.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .models import Column, EntitySpec

_TRUE = frozenset({"true", "t", "yes", "y", "on", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "off", "0"})


def lookup(record: Mapping[str, Any], path: str) -> Any:
    """``usage.input`` -> ``record["usage"]["input"]``, or None wherever the path ends early."""
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def as_string(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    # Numbers, objects and arrays read as their JSON text.
    return json.dumps(value, separators=(",", ":"))


def as_timestamp(value: Any) -> datetime | None:
    """An ISO 8601 timestamp, in UTC. One without an offset is taken to be UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_number(value: Any) -> int | None:
    """A whole number. A fraction is rounded, half away from zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        if not number.is_finite():
            return None
        return int(number.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return None


def as_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return {1: True, 0: False}.get(value)
    if isinstance(value, str):
        text = value.strip().lower()
        return True if text in _TRUE else False if text in _FALSE else None
    return None


def as_variant(value: Any) -> Any:
    return value


_CONVERTERS: Mapping[str, Callable[[Any], Any]] = {
    "STRING": as_string,
    "TIMESTAMP_TZ": as_timestamp,
    "FLOAT": as_float,
    "NUMBER": as_number,
    "BOOLEAN": as_boolean,
    "VARIANT": as_variant,
}


def column_value(column: Column, record: Mapping[str, Any]) -> Any:
    if column.derive is not None:
        return column.derive(record)
    convert = _CONVERTERS[column.type]
    for path in column.paths:
        value = convert(lookup(record, path))
        if value is not None:
            return value
    return None


def column_values(spec: EntitySpec, record: Mapping[str, Any]) -> dict[str, Any]:
    """The value of every column of ``spec``, by column name."""
    return {column.name: column_value(column, record) for column in spec.columns}
