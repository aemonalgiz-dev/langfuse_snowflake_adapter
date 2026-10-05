from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def iter_windows(
    start: datetime, end: datetime, size: timedelta
) -> Iterator[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into consecutive half-open windows of at most ``size``."""
    lower = start
    while lower < end:
        upper = min(lower + size, end)
        yield lower, upper
        lower = upper
