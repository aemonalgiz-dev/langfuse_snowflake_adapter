"""Reading and changing what is synced, per project."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...config import ConfigError, ConfigStore, SettingsUnavailable
from ...entities import ENTITIES, ENTITY_NAMES, OBSERVATION_FIELD_GROUPS, build_plan, table_name
from ...selection import parse_field, parse_filter
from ...sync import EntitySchema
from ..deps import ProjectDep, Services, ServicesDep, require_api_key
from ..schemas import (
    ConfigChange,
    ConfigChoices,
    ConfigProblem,
    ConfigResponse,
    ConfigUpdate,
    DeploymentInfo,
    EntitiesResponse,
    EntityInfo,
    ParsedField,
    ParsedFilter,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["configuration"], dependencies=[Depends(require_api_key)])

_OPERATORS = ["=", "!=", "~", "!~", ">", ">=", "<", "<="]
# Not columns of their own, but common things to filter on.
_EXTRA_FIELDS = {"observations": ["input", "output", "metadata."], "scores": ["metadata."]}


def _suggested_fields() -> dict[str, list[str]]:
    """The API field behind each column, as a starting point for writing filters."""
    suggestions = {}
    for name, spec in ENTITIES.items():
        fields = [column.paths[0] for column in spec.columns if column.paths]
        suggestions[name] = sorted({*fields, *_EXTRA_FIELDS.get(name, [])})
    return suggestions


def _describe(store: ConfigStore, services: Services) -> ConfigResponse:
    settings = store.current()
    filters = []
    for text in settings.sync.filters:
        parsed = parse_filter(text)
        filters.append(
            ParsedFilter(
                text=text,
                entity=parsed.entity,
                field=parsed.field,
                op=parsed.op,
                value=",".join(parsed.values),
            )
        )
    excluded = []
    for text in settings.sync.exclude_fields:
        field = parse_field(text)
        excluded.append(ParsedField(text=text, entity=field.entity, path=field.path))
    return ConfigResponse(
        project=store.project,
        values=store.values(),
        defaults=store.defaults(),
        overridden=store.overridden(),
        filters=filters,
        exclude_fields=excluded,
        choices=ConfigChoices(
            entities=list(ENTITY_NAMES),
            observation_fields=list(OBSERVATION_FIELD_GROUPS),
            operators=_OPERATORS,
            fields=_suggested_fields(),
        ),
        deployment=DeploymentInfo(
            langfuse_host=settings.langfuse.host,
            api_version=settings.langfuse.api_version,
            snowflake_account=settings.snowflake.account,
            snowflake_user=settings.snowflake.user,
            snowflake_role=settings.snowflake.role,
            snowflake_warehouse=settings.snowflake.warehouse,
            snowflake_database=settings.snowflake.database,
            snowflake_schema=settings.snowflake.schema_name,
            table_prefix=settings.sync.table_prefix,
        ),
        persisted=store.location is not None,
        stored_in=store.location,
        has_history=store.keeps_history,
        runs_kept=services.runs.kept_in is not None,
        runs_stored_in=services.runs.kept_in,
    )


@router.get("/config")
def get_config(project: ProjectDep, services: ServicesDep) -> ConfigResponse:
    """A project's settings: what a team can change, the defaults, and the deployment."""
    return _describe(project, services)


@router.put(
    "/config",
    responses={422: {"description": "The settings are not valid; nothing was changed."}},
)
def update_config(
    update: ConfigUpdate, project: ProjectDep, services: ServicesDep
) -> ConfigResponse:
    """Change a project's settings. They apply from its next run and are kept across restarts.

    Send only what should change. ``null`` puts a setting back to its default.
    """
    defaults = project.defaults()
    changes = {
        name: defaults[name] if value is None else value
        for name, value in update.model_dump(exclude_unset=True).items()
    }
    try:
        project.update(changes)
    except SettingsUnavailable as exc:
        raise _unsaved(project, exc) from exc
    except ConfigError as exc:
        problems = [
            ConfigProblem(field=field, message=message).model_dump()
            for field, message in (exc.problems or {"": str(exc)}).items()
        ]
        raise HTTPException(422, problems) from exc
    return _describe(project, services)


@router.delete("/config")
def reset_config(project: ProjectDep, services: ServicesDep) -> ConfigResponse:
    """Drop every saved change of a project and go back to the deployment's defaults."""
    try:
        project.reset()
    except SettingsUnavailable as exc:
        raise _unsaved(project, exc) from exc
    return _describe(project, services)


def _unsaved(project: ConfigStore, exc: Exception) -> HTTPException:
    logger.warning("Settings of %s could not be saved: %s", project.project, exc)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{exc} Nothing was changed.")


@router.get("/config/history")
def config_history(
    project: ProjectDep,
    limit: Annotated[int, Query(ge=1, le=500, description="How many changes to return.")] = 50,
) -> list[ConfigChange]:
    """Earlier versions of a project's settings, newest first.

    Each lists what differed from the deployment's defaults at the time. Only
    kept when settings are stored in Snowflake; otherwise the list is empty.
    """
    try:
        changes = project.history(limit)
    except Exception as exc:
        logger.warning("The settings history of %s could not be read: %s", project.project, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
        ) from exc
    return [
        ConfigChange(changed_at=change.changed_at, settings=change.settings)
        for change in changes or []
    ]


@router.get("/entities")
def entities(project: ProjectDep) -> EntitiesResponse:
    """How each of a project's entities is produced, and its sampling and filters."""
    settings = project.current()
    plan = build_plan(
        settings.langfuse.api_version,
        settings.sync.entities,
        observation_fields=settings.langfuse.observation_fields,
        expand_metadata=settings.langfuse.expand_metadata,
    )
    prefix = settings.sync.table_prefix
    tables = [
        EntityInfo(
            name=extract.spec.name,
            kind="table",
            object_name=table_name(prefix, extract.spec.name),
            endpoint=extract.endpoint.path,
        )
        for extract in plan.extracts
    ]
    views = [
        EntityInfo(
            name=view.name,
            kind="view",
            object_name=table_name(prefix, view.name),
            derived_from=view.source,
        )
        for view in plan.views
    ]
    return EntitiesResponse(
        api_version=plan.api_version,
        sample_rate=settings.sync.sample_rate,
        filters=list(settings.sync.filters),
        entities=tables + views,
    )


@router.get("/schema")
def schema(
    services: ServicesDep,
    project: ProjectDep,
    entity: Annotated[
        list[str] | None, Query(description="Entities to look at. Defaults to the project's.")
    ] = None,
    sample: Annotated[
        int, Query(ge=1, le=1000, description="How many of the newest records to look at.")
    ] = 200,
) -> list[EntitySchema]:
    """The fields a project's recent records actually have, read from Langfuse now.

    Projects log different things and change what they log. This is what there
    currently is to load, leave out or filter on, whatever the settings say.
    """
    unknown = sorted(set(entity or []) - set(ENTITY_NAMES))
    if unknown:
        raise HTTPException(422, f"unknown entities {', '.join(unknown)}")
    try:
        return services.backend.discover(project.project, entity, sample)
    except Exception as exc:
        logger.warning("The data of %s could not be read: %s", project.project, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
        ) from exc
