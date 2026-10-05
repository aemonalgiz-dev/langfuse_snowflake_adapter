"""Deterministic trace-level sampling.

Whether a record is kept depends only on a hash of the trace it belongs to, so
every observation and score of a trace gets the same answer, on every run and
in every process. Raising the rate only ever adds traces to the sample.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


def sample_key(entity: str, record: Mapping[str, Any]) -> str | None:
    """The trace a record belongs to, or None if it belongs to no trace.

    Records without a trace (sessions, and scores, comments or queue items on
    anything but a trace) cannot be lined up with the sampled traces and are
    always kept.
    """
    if entity in ("comments", "annotation_queue_items"):
        on_trace = str(record.get("objectType", "")).upper() == "TRACE"
        return record.get("objectId") if on_trace else None
    if entity == "observations":
        return record.get("traceId") or record.get("id")
    if entity == "traces":
        return record.get("id")
    if entity == "scores":
        subject = record.get("subject")
        if not isinstance(subject, Mapping):
            return record.get("traceId")
        if str(subject.get("kind", "")).lower() == "trace":
            return subject.get("id")
        return subject.get("traceId")
    return None


def is_sampled(key: str, rate: float) -> bool:
    if rate >= 1:
        return True
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") < rate * 2**64
