"""Building blocks for describing an entity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

ApiVersion = Literal["v4", "v3"]
Pagination = Literal["cursor", "page"]
# How a query parameter takes several values: not at all, by repeating the
# parameter, or as one comma-separated value.
FilterParam = Literal["single", "repeat", "csv"]


@dataclass(frozen=True)
class Column:
    """A typed column extracted from the raw record.

    ``paths`` are dotted JSON paths tried in order. ``expr`` is a SQL expression
    with a ``{raw}`` placeholder for cases a path and a cast cannot express.
    """

    name: str
    type: str
    paths: tuple[str, ...] = ()
    expr: str | None = None


@dataclass(frozen=True)
class Listing:
    """A cheap way to list an entity: enough to tell what is missing or has changed.

    ``params`` replace the endpoint's own, and ``fields`` are the record fields
    the listing returns. A filter on any other field needs the full record.
    """

    params: Mapping[str, str]
    fields: frozenset[str]
    updated_path: str = "updatedAt"


@dataclass(frozen=True)
class Endpoint:
    """A list endpoint.

    ``from_param`` and ``to_param`` name its time filter. An endpoint without
    one cannot be read incrementally: it is read in full each time, as a snapshot.
    A ``{parent_id}`` in ``path`` is filled with each ID of the entity's parent.
    """

    path: str
    pagination: Pagination
    from_param: str | None
    to_param: str | None
    max_limit: int
    params: Mapping[str, str | list[str]] = field(default_factory=dict)
    # Record fields the endpoint can match by equality itself, keyed by the
    # field name, which is also the query parameter name.
    filter_params: Mapping[str, FilterParam] = field(default_factory=dict)
    listing: Listing | None = None
    # The lightest parameters that still return every record's key. Used to
    # audit for deletions when the normal read is filtered.
    key_listing: Mapping[str, str] = field(default_factory=dict)

    @property
    def windowed(self) -> bool:
        return self.from_param is not None


@dataclass(frozen=True)
class EntitySpec:
    name: str
    columns: tuple[Column, ...]
    endpoints: Mapping[ApiVersion, Endpoint]
    # Columns that, with PROJECT_ID, identify a row, and where the record holds them.
    key: tuple[str, ...] = ("ID",)
    key_paths: tuple[str, ...] = ("id",)
    # The timestamp the API filters on, as a column and as a record field.
    time_column: str = "TIMESTAMP"
    time_path: str = "timestamp"
    # When a batch holds the same key twice, the row with the greatest value here wins.
    dedupe_order: str = "UPDATED_AT"
    # The entity whose records this one is listed under, such as a queue for its items.
    parent: str | None = None


@dataclass(frozen=True)
class ViewSpec:
    name: str
    source: str


@dataclass(frozen=True)
class Extract:
    spec: EntitySpec
    endpoint: Endpoint


@dataclass(frozen=True)
class SyncPlan:
    api_version: ApiVersion
    extracts: tuple[Extract, ...]
    views: tuple[ViewSpec, ...]
