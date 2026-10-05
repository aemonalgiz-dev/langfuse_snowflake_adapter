"""The FastAPI application: the HTTP API, its schedulers and the web app."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import __version__, web
from ..config import ConfigStore, Deployment, Settings
from .backend import Backend, LiveBackend
from .deps import Services
from .routes import router
from .runs import RunManager
from .scheduler import Scheduler
from .schemas import SyncRequest

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | ConfigStore | Deployment | None = None, backend: Backend | None = None
) -> FastAPI:
    """Build the application.

    Give a ``Deployment`` for every project the environment declares. A single
    ``ConfigStore`` or plain ``Settings`` makes a one-project application; with
    plain ``Settings``, changes last only until the process ends.
    """
    if settings is None:
        deployment = Deployment.open()
    elif isinstance(settings, Deployment):
        deployment = settings
    elif isinstance(settings, ConfigStore):
        deployment = Deployment.of(settings)
    else:
        deployment = Deployment.of(ConfigStore(settings))

    backend = backend or LiveBackend(lambda project: deployment.store(project).current())
    runs = RunManager(backend)

    def scheduler_for(store: ConfigStore) -> Scheduler:
        return Scheduler(
            interval=lambda: store.current().sync.schedule_minutes,
            submit=lambda: runs.submit(SyncRequest(project=store.project), trigger="schedule"),
        )

    # Each project keeps its own interval. Only one run is active at a time, so
    # a project whose turn comes while another is running waits for the next tick.
    schedulers = {store.project: scheduler_for(store) for store in deployment.stores()}
    api_key = deployment.api.key.get_secret_value() if deployment.api.key else None
    if api_key is None:
        logger.warning("SYNC_API_KEY is not set; the API accepts unauthenticated requests")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for scheduler in schedulers.values():
            scheduler.start()
        yield
        for scheduler in schedulers.values():
            scheduler.stop()
        runs.shutdown()

    app = FastAPI(
        title="Langfuse to Snowflake",
        version=__version__,
        description="Configure, trigger and monitor syncs of Langfuse projects into Snowflake.",
        lifespan=lifespan,
    )
    app.state.services = Services(deployment, backend, runs, schedulers, api_key)
    app.state.runs = runs
    app.include_router(router)
    web.mount(app)
    return app
