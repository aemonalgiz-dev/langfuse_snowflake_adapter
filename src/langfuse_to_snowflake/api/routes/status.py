"""Liveness, the projects, their sync state and schedules."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ... import __version__
from ..deps import ProjectDep, ServicesDep, require_api_key
from ..schemas import ProjectInfo, ScheduleResponse, StateEntry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["status"])
_protected = [Depends(require_api_key)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/projects", dependencies=_protected)
def projects(services: ServicesDep) -> list[ProjectInfo]:
    """The Langfuse projects this deployment syncs. Engineering declares them."""
    listed = []
    for store in services.deployment.stores():
        settings = store.current()
        listed.append(
            ProjectInfo(
                name=store.project,
                langfuse_host=settings.langfuse.host,
                api_version=settings.langfuse.api_version,
                changed_settings=len(store.overridden()),
                schedule_minutes=settings.sync.schedule_minutes,
            )
        )
    return listed


@router.get("/state", dependencies=_protected)
def state(services: ServicesDep, project: ProjectDep) -> list[StateEntry]:
    """Per entity: the watermark it is synced up to and when it was last reconciled."""
    try:
        entries = services.backend.read_state(project.project)
    except Exception as exc:
        # Reading state means reaching Langfuse and Snowflake; say which failed and why.
        logger.warning("Sync state of %s could not be read: %s", project.project, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
        ) from exc
    return [
        StateEntry(
            entity=entry.entity,
            watermark=entry.watermark,
            api_version=entry.api_version,
            updated_at=entry.updated_at,
            reconciled_at=entry.reconciled_at,
        )
        for entry in entries
    ]


@router.get("/schedule", dependencies=_protected)
def schedule(services: ServicesDep, project: ProjectDep) -> ScheduleResponse:
    """Whether the project is synced by itself, and when the next sync is due."""
    return services.schedulers[project.project].status()
