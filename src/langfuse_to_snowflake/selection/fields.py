"""Leaving fields out of what is loaded, written as ``[entity:]path``.

    observations:input              the whole field
    observations:metadata.email     one key of a nested object
    metadata.internal_notes         without an entity: wherever it occurs

An excluded field is removed from the record before it is loaded, so it never
reaches Snowflake: not in ``RAW``, and the column fed by it stays empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..entities import ENTITIES, ENTITY_NAMES

_PATTERN = re.compile(
    r"^\s*(?:(?P<entity>[A-Za-z_]+):)?(?P<path>[A-Za-z0-9_\-]+(?:\.[^.\s]+)*)\s*$"
)


@dataclass(frozen=True)
class FieldRef:
    path: str
    entity: str | None = None

    def __str__(self) -> str:
        return f"{self.entity}:{self.path}" if self.entity else self.path

    def applies_to(self, entity: str) -> bool:
        return self.entity is None or self.entity == entity


def required_fields(entity: str) -> frozenset[str]:
    """The fields a record cannot be loaded or kept current without."""
    spec = ENTITIES[entity]
    return frozenset({*spec.key_paths, spec.time_path})


def parse_field(text: str) -> FieldRef:
    match = _PATTERN.match(text)
    if match is None:
        raise ValueError(
            f"cannot parse field {text!r}; expected [entity:]field, e.g. observations:input "
            "or observations:metadata.email"
        )
    entity, path = match.group("entity", "path")
    if entity is not None and entity not in ENTITY_NAMES:
        raise ValueError(
            f"field {text!r}: unknown entity {entity!r}; choose from {', '.join(ENTITY_NAMES)}"
        )
    # Excluding one of these would leave rows that cannot be identified or placed in time.
    for name in [entity] if entity else ENTITY_NAMES:
        if path in required_fields(name):
            raise ValueError(
                f"field {text!r}: {path} is needed to load {name} and cannot be left out"
            )
    return FieldRef(path=path, entity=entity)


def without(record: Mapping[str, Any], paths: Iterable[str]) -> Mapping[str, Any]:
    """The record without the given dotted paths. The original is not modified."""
    result = record
    for path in paths:
        result = _drop(result, path.split("."))
    return result


def _drop(value: Mapping[str, Any], parts: list[str]) -> Mapping[str, Any]:
    head, rest = parts[0], parts[1:]
    if head not in value:
        return value
    if not rest:
        return {key: item for key, item in value.items() if key != head}
    inner = value[head]
    if not isinstance(inner, Mapping):
        return value
    trimmed = _drop(inner, rest)
    return value if trimmed is inner else {**value, head: trimmed}


def is_excluded(path: str, excluded: Iterable[str]) -> bool:
    """Whether a path is left out, directly or because a parent of it is."""
    return any(path == item or path.startswith(item + ".") for item in excluded)
