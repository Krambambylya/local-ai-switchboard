"""Studio tasks. The browser uploads files here and never talks to Ollama or ComfyUI."""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from local_ai_switchboard.providers.ollama import error_payload
from local_ai_switchboard.studio.images import ImageRejected, prepare_image
from local_ai_switchboard.studio.orchestrator import MultimodalTaskOrchestrator
from local_ai_switchboard.studio.plan import PlanError, replace_regions
from local_ai_switchboard.studio.plan import parse_plan
from local_ai_switchboard.studio.store import StudioFile, StudioStore, StudioTask

router = APIRouter()

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_OPERATIONS = frozenset({"analyze", "extract", "generate", "edit", "extract_and_edit"})


@router.post("/v1/studio/tasks")
async def create_task(request: Request) -> JSONResponse:
    """Store uploads and queue one multimodal task."""

    store = _store(request)
    orchestrator = _orchestrator(request)
    if store is None or orchestrator is None:
        return _unavailable()
    form = await request.form()
    prompt = form.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _invalid("prompt is required")
    if len(prompt) > 8000:
        return _invalid("prompt is too long")
    conversation = form.get("conversation_id")
    conversation_id = None
    if conversation not in {None, ""}:
        if not isinstance(conversation, str) or _ID.fullmatch(conversation) is None:
            return _invalid("conversation_id is not valid")
        conversation_id = conversation
    operation = form.get("operation")
    if operation in {None, ""}:
        operation = None
    elif not isinstance(operation, str) or operation not in _OPERATIONS:
        return _invalid("operation is not supported")
    settings = request.app.state.settings
    uploads = [item for item in form.getlist("attachments") if hasattr(item, "read")]
    if len(uploads) > settings.studio_max_images:
        return _invalid("Too many images.")
    prepared = []
    try:
        for upload in uploads:
            data = await _read_limited(upload, settings.studio_max_image_bytes)
            prepared.append((getattr(upload, "filename", None), prepare_image(
                data,
                max_bytes=settings.studio_max_image_bytes,
                analysis_edge=settings.studio_analysis_edge,
            )))
    except ImageRejected as exc:
        return _invalid(str(exc))
    task_id = uuid.uuid4().hex
    store.create_task(task_id, prompt=prompt.strip(), conversation_id=conversation_id, operation=operation)
    for index, (filename, image) in enumerate(prepared, start=1):
        label = _label(filename) or f"Image {index}"
        _store_bytes(
            store,
            task_id,
            f"input/original-{index:02d}{image.suffix}",
            image.original,
            role="original",
            media_type=image.media_type,
            width=image.width,
            height=image.height,
            label=label,
        )
        _store_bytes(
            store,
            task_id,
            f"previews/oriented-{index:02d}.png",
            image.oriented_png,
            role="oriented",
            media_type="image/png",
            width=image.width,
            height=image.height,
            label=label,
        )
        _store_bytes(
            store,
            task_id,
            f"previews/working-{index:02d}.png",
            image.working_png,
            role="working",
            media_type="image/png",
            width=image.working_width,
            height=image.working_height,
            label=label,
        )
    orchestrator.start(task_id)
    task = store.get_task(task_id)
    return JSONResponse(_task_body(store, task), status_code=202)


@router.get("/v1/studio/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> JSONResponse:
    store = _store(request)
    if store is None or _ID.fullmatch(task_id) is None:
        return _missing()
    task = store.get_task(task_id)
    if task is None:
        return _missing()
    return JSONResponse(_task_body(store, task))


@router.post("/v1/studio/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request) -> JSONResponse:
    store, orchestrator = _services(request)
    if store is None or orchestrator is None or _ID.fullmatch(task_id) is None:
        return _missing()
    outcome = await orchestrator.approve(task_id)
    if outcome == "missing":
        return _missing()
    if outcome == "not-ready":
        return _invalid("The task is not waiting for approval.")
    task = store.get_task(task_id)
    return JSONResponse(_task_body(store, task))


@router.patch("/v1/studio/tasks/{task_id}/regions")
async def update_regions(task_id: str, request: Request) -> JSONResponse:
    store = _store(request)
    if store is None or _ID.fullmatch(task_id) is None:
        return _missing()
    task = store.get_task(task_id)
    if task is None:
        return _missing()
    if task.status != "awaiting_approval" or not isinstance(task.plan, dict):
        return _invalid("Regions can be changed while the plan is waiting for approval.")
    try:
        body = await request.json()
    except ValueError:
        return _invalid("Request body must be a JSON object")
    if not isinstance(body, dict):
        return _invalid("Request body must be a JSON object")
    try:
        current = parse_plan(json_text(task.plan))
        updated = replace_regions(current, body.get("regions"))
    except PlanError as exc:
        return _invalid(str(exc))
    store.update_task(task_id, plan=updated.as_dict())
    task = store.get_task(task_id)
    return JSONResponse(_task_body(store, task))


@router.post("/v1/studio/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> JSONResponse:
    store, orchestrator = _services(request)
    if store is None or orchestrator is None or _ID.fullmatch(task_id) is None:
        return _missing()
    if store.get_task(task_id) is None:
        return _missing()
    await orchestrator.cancel(task_id)
    task = store.get_task(task_id)
    return JSONResponse(_task_body(store, task))


@router.post("/v1/studio/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request) -> JSONResponse:
    store, orchestrator = _services(request)
    if store is None or orchestrator is None or _ID.fullmatch(task_id) is None:
        return _missing()
    if store.get_task(task_id) is None:
        return _missing()
    started = await orchestrator.retry(task_id)
    if not started:
        return _invalid("The task cannot be retried.")
    task = store.get_task(task_id)
    return JSONResponse(_task_body(store, task))


@router.get("/v1/studio/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: str, request: Request) -> JSONResponse:
    store = _store(request)
    if store is None or _ID.fullmatch(task_id) is None or store.get_task(task_id) is None:
        return _missing()
    return JSONResponse({"artifacts": [_file_body(item) for item in store.list_files(task_id)]})


@router.get("/v1/studio/tasks/{task_id}/archive")
async def download_archive(task_id: str, request: Request) -> Response:
    store = _store(request)
    if store is None or _ID.fullmatch(task_id) is None or store.get_task(task_id) is None:
        return _missing()
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in store.list_files(task_id):
            path = store.resolve(item.relative_path)
            if path is None:
                continue
            archive.write(path, arcname=f"{item.role}/{Path(item.relative_path).name}")
            count += 1
    if count == 0:
        return JSONResponse(error_payload("No files are available", "not_ready"), status_code=409)
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'},
    )


@router.get("/v1/studio/files/{file_id}")
async def download_file(file_id: str, request: Request) -> Response:
    store = _store(request)
    if store is None or _ID.fullmatch(file_id) is None:
        return _missing()
    stored = store.get_file(file_id)
    if stored is None:
        return _missing()
    path = store.resolve(stored.relative_path)
    if path is None:
        return _missing()
    return FileResponse(path, media_type=stored.media_type, filename=Path(stored.relative_path).name)


def json_text(plan: dict[str, object]) -> str:
    return json.dumps(plan)


def _store_bytes(
    store: StudioStore,
    task_id: str,
    name: str,
    data: bytes,
    *,
    role: str,
    media_type: str,
    width: int,
    height: int,
    label: str,
) -> None:
    path = store.job_dir(task_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    relative = f"jobs/{task_id}/{name}"
    store.add_file(
        uuid.uuid4().hex,
        task_id=task_id,
        role=role,
        relative_path=relative,
        media_type=media_type,
        width=width,
        height=height,
        byte_size=len(data),
        label=label,
    )


async def _read_limited(upload: object, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await upload.read(1024 * 1024)  # type: ignore[attr-defined]
        if not block:
            break
        if not isinstance(block, bytes):
            raise ImageRejected("The file could not be read.")
        total += len(block)
        if total > limit:
            raise ImageRejected("The file is larger than the configured limit.")
        chunks.append(block)
    return b"".join(chunks)


def _task_body(store: StudioStore, task: StudioTask | None) -> dict[str, object]:
    if task is None:
        return {}
    files = store.list_files(task.id)
    current = next((item for item in reversed(task.stages) if item.get("status") == "running"), None)
    message = current.get("message") if isinstance(current, dict) else None
    assets = []
    if isinstance(task.plan, dict) and isinstance(task.plan.get("assets"), list):
        assets = task.plan["assets"]
    return {
        "id": task.id,
        "conversation_id": task.conversation_id,
        "status": task.status,
        "stage": task.stage,
        "message": message,
        "summary": task.summary,
        "error": task.error,
        "stages": task.stages,
        "assets": assets,
        "files": [_file_body(item) for item in files],
        "models": task.models,
        "switch_result": task.switch_result,
        "operation": task.route or task.operation,
    }


def _file_body(item: StudioFile) -> dict[str, object]:
    return {
        "id": item.id,
        "role": item.role,
        "name": Path(item.relative_path).name,
        "media_type": item.media_type,
        "width": item.width,
        "height": item.height,
        "byte_size": item.byte_size,
        "label": item.label,
        "asset_id": item.asset_id,
    }


def _label(filename: object) -> str | None:
    if not isinstance(filename, str) or not filename.strip():
        return None
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        return None
    return name[:200]


def _store(request: Request) -> StudioStore | None:
    store = getattr(request.app.state, "studio_store", None)
    return store if isinstance(store, StudioStore) else None


def _orchestrator(request: Request) -> MultimodalTaskOrchestrator | None:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    return orchestrator if isinstance(orchestrator, MultimodalTaskOrchestrator) else None


def _services(request: Request) -> tuple[StudioStore | None, MultimodalTaskOrchestrator | None]:
    return _store(request), _orchestrator(request)


def _invalid(message: str) -> JSONResponse:
    return JSONResponse(error_payload(message, "invalid_request_error"), status_code=400)


def _missing() -> JSONResponse:
    return JSONResponse(error_payload("The task was not found", "not_found"), status_code=404)


def _unavailable() -> JSONResponse:
    return JSONResponse(error_payload("Studio tasks are unavailable", "provider_error"), status_code=503)
