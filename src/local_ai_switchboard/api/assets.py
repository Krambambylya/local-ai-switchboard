"""Image jobs. The route admits work to the scheduler and does not call ComfyUI."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from local_ai_switchboard.api.auth import authorized, unauthorized
from local_ai_switchboard.providers.base import ProviderKind
from local_ai_switchboard.providers.comfyui import ComfyUIProvider, ImageExchange
from local_ai_switchboard.providers.ollama import error_payload
from local_ai_switchboard.providers.workflows import WorkflowError, request_parameters
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import JobStatus, QueueFull, SchedulerUnavailable
from local_ai_switchboard.storage.jobs import ImageJobRecord, JobStore, confined_file

router = APIRouter()

_REAL_WORKFLOW = (
    "A real ComfyUI API-format workflow export is required before image generation can be enabled."
)


@router.get("/v1/workflows")
async def list_workflows(request: Request) -> JSONResponse:
    """List registered workflows. Graphs are not included."""

    if not authorized(request):
        return unauthorized()
    provider = _comfy(request)
    if provider is None:
        return _unavailable("ComfyUI is not connected")
    workflows = [
        {
            "id": manifest.id,
            "title": manifest.title,
            "enabled": manifest.enabled,
            "disabled_reason": manifest.disabled_reason,
            "parameters": sorted(manifest.bindings),
        }
        for manifest in provider.registry.list()
    ]
    return JSONResponse({"workflows": workflows})


@router.post("/v1/assets")
async def create_asset(request: Request) -> JSONResponse:
    """Queue one registered image workflow and return its job id."""

    if not authorized(request):
        return unauthorized()
    provider = _comfy(request)
    if provider is None:
        return _unavailable("ComfyUI is not connected")
    try:
        body = await request.json()
    except ValueError:
        return _invalid("Request body must be a JSON object")
    if not isinstance(body, dict):
        return _invalid("Request body must be a JSON object")
    workflow_id = body.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        return _invalid("workflow_id is required")
    manifest = provider.registry.get(workflow_id)
    if manifest is None:
        return JSONResponse(
            error_payload(f"Workflow {workflow_id} is not registered", "not_found"),
            status_code=404,
        )
    if not manifest.enabled:
        reason = manifest.disabled_reason or _REAL_WORKFLOW
        return JSONResponse(error_payload(reason, "workflow_unavailable"), status_code=409)
    try:
        parameters = request_parameters(manifest, body)
    except WorkflowError as exc:
        return _invalid(str(exc))
    scheduler: GpuScheduler = request.app.state.scheduler
    exchange = ImageExchange(workflow_id, parameters)
    try:
        job = await scheduler.submit(ProviderKind.IMAGE, payload=exchange)
    except QueueFull:
        return JSONResponse(error_payload("GPU queue is full", "rate_limit_error"), status_code=429)
    except SchedulerUnavailable:
        return _unavailable("GPU scheduler is unavailable")
    return JSONResponse(_job_body(job), status_code=202)


@router.get("/v1/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> JSONResponse:
    """Return the status of one scheduler job."""

    if not authorized(request):
        return unauthorized()
    scheduler: GpuScheduler = request.app.state.scheduler
    job = await scheduler.get_job(job_id)
    if job is not None:
        return JSONResponse(_job_body(job))
    record = _stored_job(request, job_id)
    if record is None:
        return JSONResponse(error_payload("Job was not found", "not_found"), status_code=404)
    return JSONResponse(_record_body(record))


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> JSONResponse:
    """Cancel a queued image job, or interrupt the prompt that is running."""

    if not authorized(request):
        return unauthorized()
    scheduler: GpuScheduler = request.app.state.scheduler
    job = await scheduler.get_job(job_id)
    record = None if job is not None else _stored_job(request, job_id)
    if job is None and record is None:
        return JSONResponse(error_payload("Job was not found", "not_found"), status_code=404)
    if job is None:
        return JSONResponse(_record_body(record))
    await scheduler.cancel(job_id)
    current = await scheduler.get_job(job_id)
    return JSONResponse(_job_body(current or job))


@router.get("/v1/jobs/{job_id}/download")
async def download_job(request: Request, job_id: str) -> Response:
    """Send the image copied into gateway storage."""

    if not authorized(request):
        return unauthorized()
    provider = _comfy(request)
    scheduler: GpuScheduler = request.app.state.scheduler
    root = _output_root(request, provider)
    job = await scheduler.get_job(job_id)
    if isinstance(getattr(job, "payload", None), ImageExchange):
        if job.status is not JobStatus.SUCCEEDED or not job.payload.outputs:
            return JSONResponse(error_payload("Image is not ready", "conflict"), status_code=409)
        stored = job.payload.outputs[0]
        path = stored.path.resolve()
        if root is None or not path.is_relative_to(root.resolve()) or not path.is_file():
            return JSONResponse(error_payload("Image was not found", "not_found"), status_code=404)
        return FileResponse(path, media_type=stored.media_type, filename=stored.filename)
    record = _stored_job(request, job_id)
    if record is None:
        return JSONResponse(error_payload("Job was not found", "not_found"), status_code=404)
    if record.status != JobStatus.SUCCEEDED.value or not record.output_path or root is None:
        return JSONResponse(error_payload("Image is not ready", "conflict"), status_code=409)
    path = confined_file(root, record.output_path)
    if path is None:
        return JSONResponse(error_payload("Image was not found", "not_found"), status_code=404)
    return FileResponse(path, media_type=record.media_type or "application/octet-stream", filename=path.name)


def _store(request: Request) -> JobStore | None:
    store = getattr(request.app.state, "job_store", None)
    if isinstance(store, JobStore):
        return store
    return None


def _stored_job(request: Request, job_id: str) -> ImageJobRecord | None:
    store = _store(request)
    if store is None:
        return None
    return store.get(job_id)


def _output_root(request: Request, provider: ComfyUIProvider | None) -> Path | None:
    if provider is not None:
        return provider.output_root
    root = getattr(request.app.state, "output_root", None)
    if isinstance(root, Path):
        return root
    return None


def _record_body(record: ImageJobRecord | None) -> dict[str, object]:
    if record is None:
        return {}
    outputs: list[dict[str, str]] = []
    if record.status == JobStatus.SUCCEEDED.value and record.output_path:
        outputs.append(
            {
                "filename": Path(record.output_path).name,
                "media_type": record.media_type or "application/octet-stream",
            }
        )
    return {
        "id": record.id,
        "status": record.status,
        "workflow_id": record.workflow_id,
        "error": record.error,
        "prompt_id": record.prompt_id,
        "outputs": outputs,
    }


def _comfy(request: Request) -> ComfyUIProvider | None:
    provider = getattr(request.app.state, "comfy_provider", None)
    if isinstance(provider, ComfyUIProvider):
        return provider
    return None


def _job_body(job: object) -> dict[str, object]:
    payload = getattr(job, "payload", None)
    exchange = payload if isinstance(payload, ImageExchange) else None
    outputs = []
    if exchange is not None and getattr(job, "status", None) is JobStatus.SUCCEEDED:
        outputs = [
            {"filename": image.filename, "media_type": image.media_type}
            for image in exchange.outputs
        ]
    status = getattr(job, "status", None)
    return {
        "id": getattr(job, "id", ""),
        "status": status.value if isinstance(status, JobStatus) else "unknown",
        "workflow_id": None if exchange is None else exchange.workflow_id,
        "error": getattr(job, "error", None),
        "prompt_id": None if exchange is None else exchange.prompt_id,
        "outputs": outputs,
    }


def _invalid(message: str) -> JSONResponse:
    return JSONResponse(error_payload(message, "invalid_request_error"), status_code=400)


def _unavailable(message: str) -> JSONResponse:
    return JSONResponse(error_payload(message, "provider_error"), status_code=503)
