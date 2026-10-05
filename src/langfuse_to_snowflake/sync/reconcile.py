"""Bring a time range that was already synced back in step with Langfuse.

The incremental sync reads each record once, shortly after it starts. Anything
that happens later goes unnoticed: a record that surfaced late, a score a
reviewer changed, a trace someone deleted. Reconciling a range repairs that.

Two ways to do it, chosen per entity:

* ``full`` re-reads every record and merges it. The merge compares content, so
  any change is picked up. Used for scores and for every legacy endpoint.
* ``listing`` first lists IDs and ``updatedAt`` with a light request, compares
  them with what Snowflake holds, and re-reads only the hours that differ.
  Used for v4 observations, whose full records can be large.

Either way, rows that Snowflake holds and Langfuse no longer has are counted.
That needs the key of every record in the range, so when a filter is active
the keys are read without it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ..entities import EntitySpec, Extract, Listing
from ..selection import EntitySelection
from ..snowflake import Key
from .loading import Loaded, Loader, record_key
from .models import ReconcileResult
from .protocols import Source, Warehouse
from .timing import iter_windows

logger = logging.getLogger(__name__)

_HOUR = timedelta(hours=1)


class Reconciler:
    def __init__(
        self, source: Source, warehouse: Warehouse, loader: Loader, window: timedelta
    ) -> None:
        self._source = source
        self._warehouse = warehouse
        self._loader = loader
        self._window = window

    def reconcile(
        self,
        extract: Extract,
        selection: EntitySelection,
        project_id: str,
        start: datetime,
        end: datetime,
        *,
        full: bool = False,
        check_deletions: bool = True,
        on_window: Callable[[ReconcileResult], None] | None = None,
    ) -> ReconcileResult:
        listing = extract.endpoint.listing
        # A filter on a field the listing does not return needs the full record.
        use_listing = not full and listing is not None and selection.can_decide_from(listing.fields)
        result = ReconcileResult(
            mode="listing" if use_listing else "full", window_start=start, window_end=end
        )
        if check_deletions:
            result.rows_deleted_upstream = 0
        # A filtered read cannot tell a deleted record from one the filter left
        # out, so judging deletions takes the keys of every record, unfiltered.
        filtered = bool(selection.filters)

        for lower, upper in iter_windows(start, end, self._window):
            if use_listing and listing is not None:
                # The listing is light enough to take unfiltered and filter here.
                upstream = self._compare_listing(
                    extract,
                    listing,
                    selection,
                    project_id,
                    lower,
                    upper,
                    result,
                    pushdown=not check_deletions,
                )
            else:
                loaded = self._loader.load(
                    extract,
                    selection,
                    project_id,
                    lower,
                    upper,
                    collect_keys=check_deletions and not filtered,
                )
                result.rows_compared += loaded.fetched
                _add(result, loaded)
                upstream = loaded.keys
                if check_deletions and filtered:
                    # Full records are worth filtering at the source, so the
                    # keys come from a second, lighter pass.
                    upstream = self._loader.list_keys(extract, lower, upper)
            if check_deletions:
                held = self._warehouse.get_keys(extract.spec, project_id, lower, upper)
                result.rows_deleted_upstream = (result.rows_deleted_upstream or 0) + log_deleted(
                    extract.spec, [key for key in held if key not in upstream]
                )
            if on_window is not None:
                on_window(result)
        return result

    def _compare_listing(
        self,
        extract: Extract,
        listing: Listing,
        selection: EntitySelection,
        project_id: str,
        lower: datetime,
        upper: datetime,
        result: ReconcileResult,
        *,
        pushdown: bool,
    ) -> set[Key]:
        """Re-read the hours holding records that are missing or newer than the stored row."""
        spec = extract.spec
        endpoint = replace(extract.endpoint, params=dict(listing.params))
        if pushdown:
            endpoint = selection.pushdown(endpoint)
        upstream: set[Key] = set()
        wanted: dict[Key, tuple[datetime | None, datetime | None]] = {}
        for page in self._source.iter_pages(endpoint, lower, upper):
            for record in page:
                key = record_key(spec, record)
                upstream.add(key)
                if selection.keeps(record):
                    wanted[key] = (
                        _parse(record.get(listing.updated_path)),
                        _parse(record.get(spec.time_path)),
                    )
        result.rows_compared += len(upstream)

        held = self._warehouse.get_keys(spec, project_id, lower, upper)
        hours: set[datetime | None] = set()
        for key, (updated_at, timestamp) in wanted.items():
            if key not in held or _is_newer(updated_at, held[key]):
                hours.add(_hour(timestamp) if timestamp else None)

        for range_start, range_end in _ranges(hours, lower, upper):
            _add(result, self._loader.load(extract, selection, project_id, range_start, range_end))
        return upstream


def log_deleted(spec: EntitySpec, deleted: list[Key]) -> int:
    """Report rows whose record is gone from Langfuse; returns how many there are."""
    if deleted:
        sample = ", ".join("/".join(part for part in key if part) for key in deleted[:5])
        logger.warning(
            "%s: %d row(s) in Snowflake no longer exist in Langfuse and were left in place, e.g. %s",
            spec.name,
            len(deleted),
            sample,
        )
    return len(deleted)


def _add(result: ReconcileResult, loaded: Loaded) -> None:
    result.rows_inserted += loaded.result.rows_inserted
    result.rows_updated += loaded.result.rows_updated
    result.rows_rejected += loaded.result.rows_rejected


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_newer(upstream: datetime | None, held: datetime | None) -> bool:
    return upstream is not None and (held is None or upstream > held)


def _hour(timestamp: datetime) -> datetime:
    return timestamp.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _ranges(
    hours: Iterable[datetime | None], lower: datetime, upper: datetime
) -> list[tuple[datetime, datetime]]:
    """Merge consecutive hours into ranges, clipped to ``[lower, upper)``.

    A record without a usable timestamp (None) cannot be placed, so the whole
    window is re-read.
    """
    hours = set(hours)
    if not hours:
        return []
    if None in hours:
        return [(lower, upper)]
    ranges: list[tuple[datetime, datetime]] = []
    for hour in sorted(hour for hour in hours if hour is not None):
        start, end = max(hour, lower), min(hour + _HOUR, upper)
        if ranges and ranges[-1][1] >= start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges
