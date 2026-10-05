"""The API's routes, grouped by what they are about."""

from fastapi import APIRouter

from . import config, runs, status

router = APIRouter()
router.include_router(status.router)
router.include_router(config.router)
router.include_router(runs.router)

__all__ = ["router"]
