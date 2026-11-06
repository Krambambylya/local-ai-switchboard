"""Liveness and readiness endpoints.

/health/live and /health/ready do not require credentials. Ready returns only
ok or degraded and does not include paths or GPU details.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from local_ai_switchboard.api.schemas import LiveStatus, ReadyStatus
from local_ai_switchboard.services.preflight import run_preflight

router = APIRouter()


@router.get("/health/live", response_model=LiveStatus)
def live() -> LiveStatus:
    """Report that the process is running."""

    return LiveStatus(status="live")


@router.get(
    "/health/ready",
    response_model=ReadyStatus,
    responses={503: {"model": ReadyStatus}},
)
def ready(request: Request) -> JSONResponse:
    """Report whether required local configuration is present."""

    report = run_preflight(request.app.state.settings, gpu_probe=request.app.state.gpu_probe)
    scheduler = request.app.state.scheduler
    recovered = bool(getattr(scheduler, "recovery_ok", False))
    status: str = "ok" if report.ready and recovered else "degraded"
    code = 200 if report.ready else 503
    return JSONResponse(ReadyStatus(status=status).model_dump(), status_code=code)
