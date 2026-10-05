"""Read records of an entity from Langfuse and load what the selection keeps."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ..entities import Endpoint, EntitySpec, Extract
from ..selection import EntitySelection
from ..snowflake import Key, LoadResult
from .protocols import Source, Warehouse


def record_key(spec: EntitySpec, record: Mapping[str, Any]) -> Key:
    return tuple(record.get(path) for path in spec.key_paths)


@dataclass
class Loaded:
    fetched: int = 0
    skipped: int = 0
    result: LoadResult = field(default_factory=LoadResult)
    # Keys of every record Langfuse returned, including those the selection dropped.
    keys: set[Key] = field(default_factory=set)


class Loader:
    def __init__(self, source: Source, warehouse: Warehouse) -> None:
        self._source = source
        self._warehouse = warehouse

    def load(
        self,
        extract: Extract,
        selection: EntitySelection,
        project_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        endpoints: Sequence[Endpoint] | None = None,
        collect_keys: bool = False,
        pushdown: bool = True,
    ) -> Loaded:
        """Load the records with a timestamp in ``[start, end)``, or all of a snapshot.

        ``endpoints`` replaces the entity's own, for one that is listed per parent.

        With ``pushdown``, filters the endpoint can apply itself are sent along.
        Every record is still checked here, so that only reduces what has to be
        downloaded; turn it off when the keys of all records are needed.
        """
        loaded = Loaded()
        chosen = endpoints if endpoints is not None else [extract.endpoint]
        if pushdown:
            chosen = [selection.pushdown(endpoint) for endpoint in chosen]
        loaded.result = self._warehouse.load(
            extract.spec,
            project_id,
            self._select(extract.spec, selection, chosen, start, end, loaded, collect_keys),
        )
        return loaded

    def list_keys(
        self, extract: Extract, start: datetime | None = None, end: datetime | None = None
    ) -> set[Key]:
        """The key of every record Langfuse has, unfiltered, with the lightest request there is."""
        light = replace(extract.endpoint, params=dict(extract.endpoint.key_listing))
        return {
            record_key(extract.spec, record)
            for page in self._source.iter_pages(light, start, end)
            for record in page
        }

    def _select(
        self,
        spec: EntitySpec,
        selection: EntitySelection,
        endpoints: Sequence[Endpoint],
        start: datetime | None,
        end: datetime | None,
        loaded: Loaded,
        collect_keys: bool,
    ) -> Iterator[dict[str, Any]]:
        for endpoint in endpoints:
            for page in self._source.iter_pages(endpoint, start, end):
                for record in page:
                    loaded.fetched += 1
                    if collect_keys:
                        loaded.keys.add(record_key(spec, record))
                    if selection.keeps(record):
                        yield selection.shape(record)
                    else:
                        loaded.skipped += 1
