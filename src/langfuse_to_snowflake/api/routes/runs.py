"""Starting sync and reconcile runs, and following them."""

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import Services, ServicesDep, require_api_key, resolve_project
from ..runs import RunInProgress
from ..schemas import ReconcileRequest, Run, SyncRequest

router = APIRouter(tags=["runs"], dependencies=[Depends(require_api_key)])


def _submit(services: Services, request: SyncRequest | ReconcileRequest) -> Run:
    # A run is always for one project; fill in the name when it could be left out.
    project = resolve_project(services.deployment, request.project).project
    try:
        return services.runs.submit(request.model_copy(update={"project": project}))
    except RunInProgress as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Run {exc.run_id} is still in progress"
        ) from exc


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def start_sync(services: ServicesDep, request: SyncRequest | None = None) -> Run:
    """Start a sync of one project in the background. Only one run is active at a time."""
    return _submit(services, request or SyncRequest())


@router.post("/reconcile", status_code=status.HTTP_202_ACCEPTED)
def start_reconcile(services: ServicesDep, request: ReconcileRequest | None = None) -> Run:
    """Bring a project's already-synced data back in step with Langfuse, in the background.

    Loads records that were missed, updates those edited since they were
    loaded (re-annotated scores, for instance), and counts those deleted.
    """
    return _submit(services, request or ReconcileRequest())


@router.get("/runs")
def list_runs(services: ServicesDep, project: str | None = None) -> list[Run]:
    """Recent runs, newest first, of one project or of all.

    History is in memory and resets on restart.
    """
    runs = services.runs.recent()
    return [run for run in runs if project is None or run.project == project]


@router.get("/runs/{run_id}")
def get_run(run_id: str, services: ServicesDep) -> Run:
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run {run_id}")
    return run
