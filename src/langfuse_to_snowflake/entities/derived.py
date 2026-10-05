"""Columns that take more than a path and a conversion to work out."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .values import as_boolean, as_string, lookup

Record = Mapping[str, Any]


def is_root_observation(record: Record) -> bool:
    """v4 says so itself; on v3 an observation without a parent is the root."""
    stated = as_boolean(record.get("isRootObservation"))
    if stated is not None:
        return stated
    return as_string(record.get("parentObservationId")) is None


# Scores v3 returns one typed value; v2 returned a number plus stringValue.


def score_value_numeric(record: Record) -> float | None:
    value = record.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    return None


def score_value_string(record: Record) -> str | None:
    legacy = as_string(record.get("stringValue"))
    if legacy is not None:
        return legacy
    value = record.get("value")
    return as_string(value) if isinstance(value, str | bool) else None


# Scores v3 nests the scored object under ``subject``; v2 used flat *Id fields.

_LEGACY_SUBJECTS = (
    ("observationId", "observation"),
    ("traceId", "trace"),
    ("sessionId", "session"),
    ("datasetRunId", "experiment"),
)


def _stated_kind(record: Record) -> str | None:
    kind = as_string(lookup(record, "subject.kind"))
    return kind.lower() if kind is not None else None


def score_subject_kind(record: Record) -> str | None:
    kind = _stated_kind(record)
    if kind is not None:
        return kind
    for field, legacy_kind in _LEGACY_SUBJECTS:
        if as_string(record.get(field)) is not None:
            return legacy_kind
    return None


def _subject_id(record: Record, kind: str) -> str | None:
    return as_string(lookup(record, "subject.id")) if _stated_kind(record) == kind else None


def score_trace_id(record: Record) -> str | None:
    """The trace a score belongs to, also when it is attached to an observation in it."""
    for candidate in (record.get("traceId"), lookup(record, "subject.traceId")):
        found = as_string(candidate)
        if found is not None:
            return found
    return _subject_id(record, "trace")


def score_subject(kind: str, legacy_field: str) -> Callable[[Record], str | None]:
    """The ID of the scored object, if it is of ``kind``."""

    def derive(record: Record) -> str | None:
        legacy = as_string(record.get(legacy_field))
        return legacy if legacy is not None else _subject_id(record, kind)

    return derive
