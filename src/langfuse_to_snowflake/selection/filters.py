"""Field filters, written as ``[entity:]field<op>value``.

    environment=production,staging     any of the listed values
    observations:level!=DEBUG          none of the listed values
    observations:totalCost>=0.01       numeric or lexical comparison
    scores:comment~refund              substring
    observations:metadata.tier=gold    dotted path into nested objects
    traces:tags=vip                    a list matches if any element does
    observations:endTime=null          field is missing or null

A filter looks only at the record itself. Without an entity prefix it applies to
every entity, so prefix it unless the field means the same thing everywhere.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..entities import ENTITY_NAMES

_PATTERN = re.compile(
    r"^\s*(?:(?P<entity>[A-Za-z_]+):)?"
    r"(?P<field>[A-Za-z0-9_.\-]+)\s*"
    r"(?P<op>!=|>=|<=|!~|=|>|<|~)"
    r"\s*(?P<value>.*?)\s*$"
)
_ORDERING: Mapping[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
NULL = "null"


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    values: tuple[str, ...]
    entity: str | None = None

    def __str__(self) -> str:
        scope = f"{self.entity}:" if self.entity else ""
        return f"{scope}{self.field}{self.op}{','.join(self.values)}"

    def applies_to(self, entity: str) -> bool:
        return self.entity is None or self.entity == entity

    def matches(self, record: Mapping[str, Any]) -> bool:
        actual = _lookup(record, self.field)
        if self.op == "=":
            return _equals_any(actual, self.values)
        if self.op == "!=":
            return not _equals_any(actual, self.values)
        if self.op == "~":
            return _contains(actual, self.values[0])
        if self.op == "!~":
            return not _contains(actual, self.values[0])
        return _compare(actual, self.op, self.values[0])


def parse_filter(text: str) -> Filter:
    match = _PATTERN.match(text)
    if match is None:
        raise ValueError(
            f"cannot parse filter {text!r}; expected [entity:]field<op>value "
            "with one of = != > >= < <= ~ !~, e.g. observations:level=ERROR"
        )
    entity, field, op, value = match.group("entity", "field", "op", "value")
    if entity is not None and entity not in ENTITY_NAMES:
        raise ValueError(
            f"filter {text!r}: unknown entity {entity!r}; choose from {', '.join(ENTITY_NAMES)}"
        )
    if op in ("=", "!="):
        values = tuple(part.strip() for part in value.split(","))
    else:
        if not value:
            raise ValueError(f"filter {text!r}: {op} needs a value")
        values = (value,)
    return Filter(field=field, op=op, values=values, entity=entity)


def _lookup(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _equals(actual: Any, expected: str) -> bool:
    if actual is None:
        return expected == NULL
    if isinstance(actual, bool):
        return expected.lower() == ("true" if actual else "false")
    if isinstance(actual, int | float):
        try:
            return float(expected) == actual
        except ValueError:
            return False
    return isinstance(actual, str) and actual == expected


def _equals_any(actual: Any, expected: tuple[str, ...]) -> bool:
    candidates = actual if isinstance(actual, list) else [actual]
    return any(_equals(candidate, value) for candidate in candidates for value in expected)


def _contains(actual: Any, needle: str) -> bool:
    if actual is None:
        return False
    if isinstance(actual, str):
        return needle in actual
    if isinstance(actual, list):
        return any(_contains(item, needle) for item in actual)
    if isinstance(actual, Mapping):
        return needle in json.dumps(actual, ensure_ascii=False)
    return needle in str(actual)


def _compare(actual: Any, op: str, expected: str) -> bool:
    if actual is None or isinstance(actual, bool | list | Mapping):
        return False
    try:
        # Numbers, and numbers the API sends as strings, such as prices.
        return _ORDERING[op](float(actual), float(expected))
    except ValueError:
        # Anything else compares as text, which orders ISO 8601 timestamps correctly.
        return isinstance(actual, str) and _ORDERING[op](actual, expected)
