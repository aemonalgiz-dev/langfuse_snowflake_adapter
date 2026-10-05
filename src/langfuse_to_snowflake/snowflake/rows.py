"""Preparing records for loading: typed values, a fingerprint, and bounded batches.

Everything here is plain Python. Snowflake receives rows that are already
typed, so what it does with them is the same wherever it runs: store them,
and merge on the key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..entities import EntitySpec, column_values
from .models import Key

# What a batch row holds, in order: the project, the entity's columns, the
# record as JSON text and the fingerprint of that text.
Row = tuple[Any, ...]


class Rejected(ValueError):
    """A record that cannot be loaded."""


@dataclass
class Batch:
    rows: list[Row] = field(default_factory=list)
    rows_rejected: int = 0
    first_error: str | None = None


def fingerprint(text: str) -> str:
    return hashlib.blake2b(text.encode("ascii"), digest_size=16).hexdigest()


def serialize(record: Mapping[str, Any]) -> str:
    """The record as JSON text. Keys are sorted, so the same record always reads the same."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def prepare(
    spec: EntitySpec, project_id: str, record: Mapping[str, Any], max_record_bytes: int
) -> tuple[Key, Any, Row]:
    """A record's key, the value that orders its versions, and its row."""
    values = column_values(spec, record)
    if values["ID"] is None:
        raise Rejected("the record has no id")
    text = serialize(record)
    if len(text) > max_record_bytes:
        raise Rejected(
            f"record {values['ID']} is {len(text)} bytes, over the limit of {max_record_bytes}"
        )
    key = tuple(values[name] for name in spec.key)
    row = (project_id, *values.values(), text, fingerprint(text))
    return key, values.get(spec.dedupe_order), row


def _newer(order: Any, than: Any) -> bool:
    """Whether a version replaces the one already held. On a tie the later one read wins."""
    if order is None:
        return than is None
    return than is None or order >= than


def batches(
    spec: EntitySpec,
    project_id: str,
    records: Iterable[Mapping[str, Any]],
    *,
    max_rows: int,
    max_bytes: int,
    max_record_bytes: int,
) -> Iterator[Batch]:
    """Group records into batches of at most ``max_rows`` rows and about ``max_bytes``.

    A batch holds each key once, as its newest version: a merge cannot take the
    same row twice.
    """
    held: dict[Key, tuple[Any, Row]] = {}
    batch = Batch()
    size = 0
    for record in records:
        try:
            key, order, row = prepare(spec, project_id, record, max_record_bytes)
        except Rejected as exc:
            batch.rows_rejected += 1
            batch.first_error = batch.first_error or str(exc)
            continue
        previous = held.get(key)
        if previous is None or _newer(order, previous[0]):
            held[key] = (order, row)
        size += len(row[-2])
        if len(held) >= max_rows or size >= max_bytes:
            batch.rows = [row for _, row in held.values()]
            yield batch
            held, batch, size = {}, Batch(), 0
    if held or batch.rows_rejected:
        batch.rows = [row for _, row in held.values()]
        yield batch
