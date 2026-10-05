"""Look at recent records to see which fields a project's data actually has.

Projects differ in what they log, and what they log changes. Rather than
assume a shape, this reads a sample of the newest records straight from
Langfuse and lists every field found in them, nested ones by dotted path, so
that someone can decide what to leave out or filter on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

_MAX_DEPTH = 4


class FieldSchema(BaseModel):
    path: str
    # The JSON types seen for the field across the sample.
    types: list[str]
    # Share of the sampled records in which the field has a value.
    share: float
    # Left out of what is loaded, by the project's settings.
    excluded: bool = False
    # Needed to load the record at all; cannot be left out.
    required: bool = False
    # The Snowflake column the field feeds, if it has one of its own.
    column: str | None = None


class EntitySchema(BaseModel):
    entity: str
    table: str
    # How many records the fields were taken from.
    sampled: int = 0
    fields: list[FieldSchema] = []
    unavailable: str | None = None


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    return "object"


def _walk(value: Mapping[str, Any], prefix: str, depth: int) -> Iterable[tuple[str, Any]]:
    for key, item in value.items():
        path = f"{prefix}{key}"
        yield path, item
        # Keys containing a dot could not be told apart from nesting later on.
        if isinstance(item, Mapping) and depth < _MAX_DEPTH and "." not in str(key):
            yield from _walk(item, f"{path}.", depth + 1)


def describe(
    records: Iterable[Mapping[str, Any]],
) -> tuple[int, dict[str, tuple[list[str], float]]]:
    """The fields found in the records: for each path, its types and how often it has a value."""
    total = 0
    present: Counter[str] = Counter()
    types: dict[str, set[str]] = {}
    for record in records:
        total += 1
        for path, value in _walk(record, "", 1):
            kind = _type_of(value)
            types.setdefault(path, set())
            if kind != "null":
                present[path] += 1
                types[path].add(kind)
    return total, {
        path: (sorted(kinds), present[path] / total if total else 0.0)
        for path, kinds in sorted(types.items())
    }
