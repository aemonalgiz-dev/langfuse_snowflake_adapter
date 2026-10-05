"""Does the real Langfuse API accept what the client sends? Read-only."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from langfuse_to_snowflake.config import SyncSettings
from langfuse_to_snowflake.entities import ENTITY_NAMES, build_plan
from langfuse_to_snowflake.langfuse import LangfuseApiError, LangfuseClient
from langfuse_to_snowflake.selection import Selection
from langfuse_to_snowflake.snowflake import SnowflakeAdapter
from langfuse_to_snowflake.sync import SyncService

pytestmark = pytest.mark.live

NOW = datetime.now(UTC).replace(microsecond=0)
WEEK = (NOW - timedelta(days=7), NOW)


@pytest.fixture
def client(live_langfuse):
    with LangfuseClient(live_langfuse) as opened:
        yield opened


def _extracts(live_langfuse):
    plan = build_plan(
        live_langfuse.api_version,
        ENTITY_NAMES,
        observation_fields=live_langfuse.observation_fields,
        expand_metadata=live_langfuse.expand_metadata,
    )
    return {extract.spec.name: extract for extract in plan.extracts}


def test_the_keys_belong_to_a_project(client):
    project = client.get_project()

    assert project["id"] and project["name"]


def test_every_endpoint_accepts_its_request(client, live_langfuse):
    """Paths, parameters and field groups are all taken from documentation; this is the proof."""
    problems = []
    for name, extract in _extracts(live_langfuse).items():
        endpoint = extract.endpoint
        if "{parent_id}" in endpoint.path:
            continue
        try:
            pages = (
                client.iter_pages(endpoint, *WEEK)
                if endpoint.windowed
                else client.iter_pages(endpoint)
            )
            for page in pages:
                assert all("id" in record for record in page), name
                break
        except LangfuseApiError as exc:
            if exc.status_code in (402, 403, 404):
                continue  # a feature this project's plan does not have
            problems.append(f"{name}: {exc}")

    assert not problems, "\n".join(problems)


def test_the_lighter_requests_used_for_reconciling_are_accepted(client, live_langfuse):
    extracts = _extracts(live_langfuse)
    if live_langfuse.api_version != "v4":
        pytest.skip("only the v4 API has a lighter listing")
    observations = extracts["observations"].endpoint

    listing = observations.listing
    assert listing is not None
    for params in (dict(listing.params), dict(observations.key_listing)):
        light = replace(observations, params=params)
        for page in client.iter_pages(light, *WEEK):
            assert all({"id", "traceId", "startTime"} <= set(record) for record in page)
            break


def test_filters_sent_to_the_api_are_accepted(client, live_langfuse):
    """A filter the API understands is sent along; the API must not reject it."""
    selection = Selection.parse(1.0, ["environment=production,staging", "observations:level=ERROR"])
    for name in ("observations", "scores"):
        endpoint = selection.for_entity(name).pushdown(_extracts(live_langfuse)[name].endpoint)
        assert "environment" in endpoint.params
        for _ in client.iter_pages(endpoint, *WEEK):
            break


def test_looking_at_recent_data_works(client, live_langfuse, snowflake_settings):
    """What the web app does when someone asks to see a project's fields."""
    service = SyncService(
        live_langfuse,
        SyncSettings(_env_file=None),
        client,
        SnowflakeAdapter(snowflake_settings),  # never connected: this only reads Langfuse
    )

    schemas = service.discover(sample=25)

    assert [schema.entity for schema in schemas] == [
        "observations",
        "scores",
        "comments",
        "annotation_queues",
        "annotation_queue_items",
    ]
    for schema in schemas:
        if schema.sampled:
            assert "id" in {field.path for field in schema.fields}
