"""What the routes share: the running services, the project in question and the key check."""

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, Security, status
from fastapi.security import APIKeyHeader

from ..config import ConfigError, ConfigStore, Deployment
from .backend import Backend
from .runs import RunManager
from .scheduler import Scheduler


@dataclass
class Services:
    deployment: Deployment
    backend: Backend
    runs: RunManager
    # One per project, each following that project's own interval.
    schedulers: dict[str, Scheduler]
    api_key: str | None


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def resolve_project(deployment: Deployment, project: str | None) -> ConfigStore:
    """The named project's settings, or the only project's when no name is given."""
    try:
        return deployment.store(project)
    except ConfigError as exc:
        code = status.HTTP_404_NOT_FOUND if project else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(exc)) from exc


def get_project(
    services: ServicesDep,
    project: Annotated[
        str | None,
        Query(description="The project meant. May be left out when there is only one."),
    ] = None,
) -> ConfigStore:
    return resolve_project(services.deployment, project)


ProjectDep = Annotated[ConfigStore, Depends(get_project)]

_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    services: ServicesDep, provided: Annotated[str | None, Security(_key_header)] = None
) -> None:
    expected = services.api_key
    if expected is None:
        return
    if provided is None or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key")
