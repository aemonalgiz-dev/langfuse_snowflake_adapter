"""Resolve a selection of entities into what to extract and what to derive."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .definitions import (
    ENTITIES,
    ENTITY_NAMES,
    LISTING_FIELD_GROUPS,
    OBSERVATION_FIELD_GROUPS,
    VIEW_FIELD_GROUPS,
    VIEWS,
)
from .models import ApiVersion, Extract, Listing, SyncPlan


def build_plan(
    api_version: ApiVersion,
    selected: Sequence[str],
    *,
    observation_fields: Sequence[str] = OBSERVATION_FIELD_GROUPS,
    expand_metadata: Sequence[str] = (),
) -> SyncPlan:
    """Resolve the selected entities into endpoints to extract and views to create.

    Selecting a view also extracts the entity it is derived from, and selecting
    a child entity also extracts its parent. Parents come before their children.
    """
    if not selected:
        raise ValueError("No entities selected")
    unknown = sorted(set(selected) - set(ENTITY_NAMES))
    if unknown:
        raise ValueError(
            f"Unknown entities: {', '.join(unknown)}. Choose from: {', '.join(ENTITY_NAMES)}"
        )

    views = tuple(
        VIEWS[api_version][name]
        for name in ENTITY_NAMES
        if name in selected and name in VIEWS[api_version]
    )
    wanted = set(selected) | {view.source for view in views}
    # A child is listed per parent record, so its parent has to be read too.
    wanted |= {parent for name in wanted if (parent := ENTITIES[name].parent)}

    if views:
        missing = [group for group in VIEW_FIELD_GROUPS if group not in observation_fields]
        if missing:
            raise ValueError(
                f"The {' and '.join(view.name for view in views)} view(s) need the observation "
                f"field group(s) {', '.join(missing)}; add them to LANGFUSE_OBSERVATION_FIELDS"
            )

    extracts = []
    for name in ENTITY_NAMES:
        endpoint = ENTITIES[name].endpoints.get(api_version)
        if name not in wanted or endpoint is None:
            continue
        if name == "observations" and api_version == "v4":
            params = {**endpoint.params, "fields": ",".join(observation_fields)}
            if expand_metadata:
                params["expandMetadata"] = ",".join(expand_metadata)
            endpoint = replace(
                endpoint, params=params, listing=_observation_listing(observation_fields)
            )
        extracts.append(Extract(ENTITIES[name], endpoint))

    return SyncPlan(api_version, tuple(extracts), views)


def _observation_listing(observation_fields: Sequence[str]) -> Listing | None:
    """List only the light field groups that are also synced.

    Without "time" the stored rows have no updatedAt to compare against, so a
    listing could not tell that a record changed; there is none in that case.
    """
    if "time" not in observation_fields:
        return None
    groups = [group for group in LISTING_FIELD_GROUPS if group in observation_fields]
    fields = frozenset(name for group in groups for name in LISTING_FIELD_GROUPS[group])
    return Listing(params={"fields": ",".join(groups)}, fields=fields)
