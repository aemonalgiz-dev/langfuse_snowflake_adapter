"""Combine filters, sampling and field exclusion into what happens to each record."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..entities import VIEWS, ApiVersion, Endpoint
from .fields import FieldRef, parse_field, without
from .filters import NULL, Filter, parse_filter
from .sampling import is_sampled, sample_key


@dataclass(frozen=True)
class EntitySelection:
    """What applies to one entity: which records are kept, and which fields of them."""

    entity: str
    sample_rate: float = 1.0
    filters: tuple[Filter, ...] = ()
    # Dotted paths removed from every kept record before it is loaded.
    excluded: tuple[str, ...] = ()

    @property
    def keeps_everything(self) -> bool:
        return self.sample_rate >= 1 and not self.filters

    def can_decide_from(self, fields: frozenset[str]) -> bool:
        """Whether a record holding only ``fields`` is enough to apply every filter."""
        return all(item.field.split(".")[0] in fields for item in self.filters)

    def keeps(self, record: Mapping[str, Any]) -> bool:
        if not all(item.matches(record) for item in self.filters):
            return False
        if self.sample_rate >= 1:
            return True
        key = sample_key(self.entity, record)
        return key is None or is_sampled(key, self.sample_rate)

    def shape(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        """The record as it is to be loaded: without the excluded fields.

        Filters are applied to the whole record first, so a field can be
        filtered on and still be left out of Snowflake.
        """
        return without(record, self.excluded) if self.excluded else record

    def pushdown(self, endpoint: Endpoint) -> Endpoint:
        """Add query parameters for the filters the endpoint can apply itself.

        This only saves requests and transfer: every record is still checked
        with ``keeps``, so a parameter the server ignores cannot let records in.
        """
        params = dict(endpoint.params)
        for item in self.filters:
            mode = endpoint.filter_params.get(item.field)
            if mode is None or item.op != "=" or item.field in params:
                continue
            if any(value in ("", NULL) for value in item.values):
                continue
            if mode == "single":
                if len(item.values) == 1:
                    params[item.field] = item.values[0]
            elif mode == "repeat":
                params[item.field] = list(item.values)
            else:
                params[item.field] = ",".join(item.values)
        return replace(endpoint, params=params)


@dataclass(frozen=True)
class Selection:
    sample_rate: float = 1.0
    filters: tuple[Filter, ...] = ()
    excluded: tuple[FieldRef, ...] = ()

    @classmethod
    def parse(
        cls, sample_rate: float, filters: Sequence[str], exclude_fields: Sequence[str] = ()
    ) -> Selection:
        if not 0 < sample_rate <= 1:
            raise ValueError("sample rate must be greater than 0 and at most 1")
        return cls(
            sample_rate,
            tuple(parse_filter(text) for text in filters),
            tuple(parse_field(text) for text in exclude_fields),
        )

    def check(self, api_version: ApiVersion) -> None:
        """Reject filters and exclusions on entities that are not extracted under this API version."""
        for item in (*self.filters, *self.excluded):
            view = VIEWS[api_version].get(item.entity or "")
            if view is not None:
                kind = "filter" if isinstance(item, Filter) else "field"
                raise ValueError(
                    f"{kind} '{item}': on Langfuse {api_version}, {view.name} are derived from "
                    f"{view.source} and have no records of their own. Use {view.source} "
                    f"instead, e.g. {view.source}:userId (trace attributes there: userId, "
                    "sessionId, traceName, tags, release, environment)."
                )

    def for_entity(self, entity: str) -> EntitySelection:
        return EntitySelection(
            entity,
            self.sample_rate,
            tuple(item for item in self.filters if item.applies_to(entity)),
            tuple(item.path for item in self.excluded if item.applies_to(entity)),
        )
