"""Run one studio task as GPU stages on the existing scheduler.

This object never calls Ollama or ComfyUI itself, and it never calls submit
from inside a provider. Approval waits outside the GPU queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from local_ai_switchboard.providers.base import ProviderKind
from local_ai_switchboard.providers.comfyui import ImageExchange
from local_ai_switchboard.providers.ollama import ChatExchange, build_chat_body, user_content
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import JobResult, JobStatus, QueueFull, SchedulerState, SchedulerUnavailable
from local_ai_switchboard.storage.jobs import relative_output
from PIL import Image

from local_ai_switchboard.studio.images import crop_normalized, open_png, png_bytes
from local_ai_switchboard.studio.plan import PlanError, choose_route, needs_approval, parse_plan
from local_ai_switchboard.studio.store import StudioStore, StudioTask, task_is_terminal

logger = logging.getLogger("local_ai_switchboard.studio")

_ANALYSIS = (
    "You inspect the user's request and any attached images. "
    "Reply with one JSON object and no other text. "
    'Schema: {"summary": string, "description": string, '
    '"operations": ["analyze"|"extract"|"generate"|"edit"|"extract_and_edit"], '
    '"assets": [{"id": string, "label": string, "type": string, '
    '"bbox": {"x": number, "y": number, "width": number, "height": number} or null, '
    '"confidence": number, "operation": string, "edit_instruction": string or null}]}. '
    "Coordinates are normalized from 0 to 1 on the attached image. "
    "Use analyze when no new image is needed. Use extract for crops only. "
    "Use edit for a generative change of a whole attached image. "
    "Use extract_and_edit when specific regions should be cropped and then edited. "
    "Use generate when a new image should be created without editing a source. "
    "Do not invent pixel masks."
)
_REVIEW = (
    "Summarize what was done for the user in plain language. "
    "Do not claim the image was improved, restored, or upscaled. "
    "If FLUX edited or generated an image, say that it redrew the image from the instruction."
)


class OrchestratorError(RuntimeError):
    """A task stopped before it could publish a result."""


class MultimodalTaskOrchestrator:
    """Application service for one user task that may use Qwen and FLUX."""

    def __init__(
        self,
        *,
        store: StudioStore,
        scheduler: GpuScheduler,
        model: str,
        analysis_edge: int,
        edit_edge: int,
        edit_workflow: str = "flux2-klein-edit",
        t2i_workflow: str = "flux2-klein-t2i",
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._model = model
        self._analysis_edge = analysis_edge
        self._edit_edge = edit_edge
        self._edit_workflow = edit_workflow
        self._t2i_workflow = t2i_workflow
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    def start(self, task_id: str) -> None:
        self._spawn(task_id, self._guard(task_id, self._analyze(task_id)))

    async def approve(self, task_id: str) -> str | None:
        task = self._store.get_task(task_id)
        if task is None:
            return "missing"
        if task.status != "awaiting_approval":
            return "not-ready"
        self._store.update_task(task_id, status="running", approved=True, stage="cropping")
        self._spawn(task_id, self._guard(task_id, self._execute(task_id)))
        return None

    async def cancel(self, task_id: str) -> bool:
        task = self._store.get_task(task_id)
        if task is None or task_is_terminal(task.status):
            return False
        self._cancel.setdefault(task_id, asyncio.Event()).set()
        if task.scheduler_job_id:
            await self._scheduler.cancel(task.scheduler_job_id)
        if task.status == "awaiting_approval":
            self._store.update_task(task_id, status="cancelled", stage="cancelled", error=None)
        return True

    async def retry(self, task_id: str) -> bool:
        task = self._store.get_task(task_id)
        if task is None or task.status not in {"failed", "interrupted", "cancelled"}:
            return False
        self._cancel[task_id] = asyncio.Event()
        if task.plan and task.route and task.approved:
            self._store.update_task(task_id, status="running", error=None, stage="cropping")
            self._spawn(task_id, self._guard(task_id, self._execute(task_id)))
            return True
        if task.plan and task.route and needs_approval(task.route):
            self._store.update_task(
                task_id,
                status="awaiting_approval",
                error=None,
                stage="approval",
            )
            return True
        self._store.update_task(task_id, status="running", error=None, plan=None, route=None, approved=False)
        self._spawn(task_id, self._guard(task_id, self._analyze(task_id)))
        return True

    async def join(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            await task

    async def shutdown(self) -> None:
        for event in self._cancel.values():
            event.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _spawn(self, task_id: str, work: Any) -> None:
        self._cancel.setdefault(task_id, asyncio.Event())
        self._tasks[task_id] = asyncio.create_task(work, name=f"studio-{task_id}")

    async def _guard(self, task_id: str, work: Any) -> None:
        try:
            await work
        except asyncio.CancelledError:
            current = self._store.get_task(task_id)
            if current is not None and not task_is_terminal(current.status):
                self._store.update_task(task_id, status="cancelled", stage="cancelled")
            raise
        except PlanError as exc:
            self._fail(task_id, str(exc), stage="planning")
        except OrchestratorError as exc:
            self._fail(task_id, str(exc))
        except Exception:
            logger.exception("studio task %s failed", task_id)
            self._fail(task_id, "The task failed.")

    async def _analyze(self, task_id: str) -> None:
        task = self._require(task_id)
        self._mark(task_id, "uploading", "succeeded", "Uploading images.")
        self._mark(task_id, "analyzing", "running", "Analyzing image with Qwen.", model=self._model, provider="ollama")
        images = self._image_parts(task_id, "working")
        prior = self._store.latest_summary(task.conversation_id, exclude=task_id)
        prompt = _ANALYSIS + f"\nUser request: {task.prompt}"
        if task.operation:
            prompt += f"\nPreferred operation: {task.operation}."
        if prior:
            prompt += f"\nPrevious result: {prior}"
        exchange = ChatExchange(
            build_chat_body(self._model, [user_content(prompt, images)], json_mode=True)
        )
        job = await self._submit([(ProviderKind.LLM, exchange)])
        if job is None:
            return
        result = await self._wait(task_id, job, "analysis")
        if await self._stopped(task_id, result):
            return
        text = await _read_exchange(exchange)
        self._write_json(task_id, "analysis/qwen-analysis.json", {"content": text})
        plan = parse_plan(text)
        self._write_json(task_id, "analysis/plan.json", plan.as_dict())
        route = choose_route(plan, task.operation, has_images=bool(images))
        self._store.update_task(
            task_id,
            plan=plan.as_dict(),
            route=route,
            summary=plan.description,
            models={"analysis": self._model},
        )
        self._mark(task_id, "analyzing", "succeeded", "Analyzing image with Qwen.", model=self._model, provider="ollama")
        self._mark(task_id, "planning", "succeeded", "Planning operations.")
        logger.info("studio task %s planned route=%s", task_id, route)
        if needs_approval(route):
            self._mark(task_id, "approval", "running", "Confirm or adjust the regions.")
            self._store.update_task(task_id, status="awaiting_approval", stage="approval", scheduler_job_id=None)
            self._write_manifest(task_id)
            return
        await self._execute(task_id)

    async def _execute(self, task_id: str) -> None:
        task = self._require(task_id)
        route = task.route or "analyze"
        if route == "analyze":
            self._complete(
                task_id,
                task.summary or task.prompt,
                "Qwen analyzed the request. FLUX was not called.",
            )
            return
        if route == "extract":
            self._crops(task_id)
            self._complete(
                task_id,
                task.summary or task.prompt,
                "Qwen located regions and Pillow cropped them. FLUX was not called.",
            )
            return
        exchanges = self._image_stages(task_id, route)
        if not exchanges:
            raise OrchestratorError("The plan did not include an image to edit.")
        review = ChatExchange({}, prepare=lambda: self._review_body(task_id, exchanges))
        stages: list[tuple[ProviderKind, object]] = [(ProviderKind.IMAGE, item) for item in exchanges]
        stages.append((ProviderKind.LLM, review))
        phase = "generate" if route == "generate" else "edit"
        job = await self._submit(stages)
        if job is None:
            return
        result = await self._wait(task_id, job, phase)
        if await self._stopped(task_id, result):
            return
        self._record_outputs(task_id, exchanges)
        text = await _read_exchange(review)
        workflow = exchanges[0].workflow_id
        models = dict(task.models)
        models["image"] = workflow
        models["review"] = self._model
        self._store.update_task(task_id, models=models)
        self._complete(
            task_id,
            text,
            "The scheduler confirmed GPU release between Qwen and FLUX before the next model started.",
        )

    async def _submit(self, stages: list[tuple[ProviderKind, object]]) -> Any:
        try:
            return await self._scheduler.submit_sequence(stages)
        except QueueFull:
            raise OrchestratorError("GPU queue is full.") from None
        except SchedulerUnavailable as exc:
            raise OrchestratorError(str(exc) or "The scheduler is unavailable.") from exc

    def _image_stages(self, task_id: str, route: str) -> list[ImageExchange]:
        task = self._require(task_id)
        instruction = _instruction(task)
        if route == "generate":
            return [self._generate_exchange(task_id, instruction)]
        if route == "extract_and_edit":
            exchanges: list[ImageExchange] = []
            for asset_id, path, edit in self._crops(task_id):
                if edit is None:
                    continue
                exchanges.append(self._edit_exchange(task_id, path, edit, asset_id))
            return exchanges
        source = self._edit_source(task_id)
        return [self._edit_exchange(task_id, source, instruction, None)]

    def _crops(self, task_id: str) -> list[tuple[str, Path, str | None]]:
        self._mark(task_id, "cropping", "running", "Cropping with Pillow.")
        task = self._require(task_id)
        oriented = self._first_path(task_id, "oriented")
        image = open_png(oriented.read_bytes())
        saved: list[tuple[str, Path, str | None]] = []
        assets = (task.plan or {}).get("assets") or []
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("bbox"), dict):
                continue
            bbox = asset["bbox"]
            cropped = crop_normalized(
                image,
                x=float(bbox["x"]),
                y=float(bbox["y"]),
                width=float(bbox["width"]),
                height=float(bbox["height"]),
            )
            data, width, height = png_bytes(cropped)
            asset_id = str(asset.get("id") or "asset")
            path = self._write_bytes(task_id, f"extracted/{_safe_stem(asset_id)}.png", data)
            self._register(
                task_id,
                path,
                role="extracted",
                media_type="image/png",
                width=width,
                height=height,
                label=str(asset.get("label") or asset_id),
                asset_id=asset_id,
            )
            edit = asset.get("edit_instruction") if asset.get("operation") == "extract_and_edit" else None
            if not isinstance(edit, str) or not edit.strip():
                edit = _instruction(task) if asset.get("operation") == "extract_and_edit" else None
            saved.append((asset_id, path, edit.strip() if isinstance(edit, str) else None))
        self._mark(task_id, "cropping", "succeeded", "Cropping with Pillow.")
        return saved

    def _edit_source(self, task_id: str) -> Path:
        image = open_png(self._first_path(task_id, "oriented").read_bytes())
        data, width, height = png_bytes(image, edge=self._edit_edge)
        path = self._write_bytes(task_id, "previews/edit-01.png", data)
        self._register(
            task_id,
            path,
            role="edit-source",
            media_type="image/png",
            width=width,
            height=height,
            label="FLUX input",
        )
        return path

    def _edit_exchange(self, task_id: str, source: Path, instruction: str, asset_id: str | None) -> ImageExchange:
        output = self._store.job_dir(task_id) / "generated"
        output.mkdir(parents=True, exist_ok=True)
        prepared = self._fit(source)
        return ImageExchange(
            self._edit_workflow,
            {"prompt": instruction[:4000], "steps": 4, "seed": 1, "megapixels": 0.5},
            source=prepared,
            output_dir=output,
            asset_id=asset_id,
        )

    def _generate_exchange(self, task_id: str, instruction: str) -> ImageExchange:
        output = self._store.job_dir(task_id) / "generated"
        output.mkdir(parents=True, exist_ok=True)
        return ImageExchange(
            self._t2i_workflow,
            {"prompt": instruction[:4000], "width": self._edit_edge, "height": self._edit_edge, "steps": 4, "seed": 1},
            output_dir=output,
        )

    def _fit(self, source: Path) -> Path:
        image = open_png(source.read_bytes())
        if max(image.size) <= self._edit_edge:
            return source
        data, _, _ = png_bytes(image, edge=self._edit_edge)
        target = source.with_name(f"{source.stem}-flux.png")
        target.write_bytes(data)
        return target

    def _review_body(self, task_id: str, exchanges: list[ImageExchange]) -> dict[str, Any]:
        task = self._require(task_id)
        images: list[tuple[str, bytes]] = []
        for exchange in exchanges:
            for stored in exchange.outputs[:1]:
                images.append((stored.media_type, stored.path.read_bytes()))
        summary = ""
        if isinstance(task.plan, dict) and isinstance(task.plan.get("summary"), str):
            summary = task.plan["summary"]
        text = f"{_REVIEW}\nUser request: {task.prompt}\nPlan summary: {summary}"
        return build_chat_body(self._model, [user_content(text, images)])

    def _record_outputs(self, task_id: str, exchanges: list[ImageExchange]) -> None:
        for exchange in exchanges:
            for stored in exchange.outputs:
                relative = relative_output(self._store.root, stored.path)
                if relative is None:
                    raise OrchestratorError("The generated image was stored outside the task directory.")
                width, height = _dimensions(stored.path)
                self._store.add_file(
                    uuid.uuid4().hex,
                    task_id=task_id,
                    role="generated",
                    relative_path=relative,
                    media_type=stored.media_type,
                    width=width,
                    height=height,
                    byte_size=stored.path.stat().st_size,
                    label=stored.filename,
                    asset_id=exchange.asset_id,
                )

    async def _wait(self, task_id: str, job: Any, phase: str) -> JobResult:
        self._store.update_task(task_id, scheduler_job_id=job.id, status="running")
        while not job.done.done():
            event = self._cancel.get(task_id)
            if event is not None and event.is_set():
                await self._scheduler.cancel(job.id)
            status = await self._scheduler.status()
            self._note_scheduler(task_id, status.state, phase)
            try:
                await asyncio.wait_for(asyncio.shield(job.done), 0.3)
            except TimeoutError:
                continue
        return job.done.result()

    async def _stopped(self, task_id: str, result: JobResult) -> bool:
        event = self._cancel.get(task_id)
        if result.status is JobStatus.CANCELLED or (event is not None and event.is_set()):
            current = self._store.get_task(task_id)
            if current is not None and not task_is_terminal(current.status):
                self._store.update_task(task_id, status="cancelled", stage="cancelled", scheduler_job_id=None)
            return True
        if result.status is not JobStatus.SUCCEEDED:
            message = result.error or "The GPU stage failed."
            status = await self._scheduler.status()
            if status.state is SchedulerState.ERROR:
                self._fail(task_id, message)
                await self.note_recovery(task_id)
            else:
                self._fail(task_id, f"{message} The scheduler stopped the remaining stages.")
            return True
        return False

    async def note_recovery(self, task_id: str) -> None:
        """Use the existing recovery path when a stage left the scheduler in ERROR."""

        status = await self._scheduler.status()
        if status.state is not SchedulerState.ERROR:
            return
        recovered = await self._scheduler.recover()
        task = self._store.get_task(task_id)
        if task is None or not task.error:
            return
        suffix = (
            " Scheduler recovery returned to IDLE."
            if recovered
            else " Scheduler recovery did not free the GPU."
        )
        if suffix.strip() in task.error:
            return
        self._store.update_task(task_id, error=(task.error + suffix)[:500])

    def _note_scheduler(self, task_id: str, state: SchedulerState, phase: str) -> None:
        image_stage = "generating" if phase == "generate" else "editing"
        image_message = "Generating an image with FLUX." if phase == "generate" else "Editing image with FLUX."
        if state is SchedulerState.SWITCH_TO_IMAGE:
            self._mark(task_id, "releasing_ollama", "running", "Releasing Ollama GPU resources.", provider="ollama")
        elif state in {SchedulerState.IMAGE_QUEUED, SchedulerState.IMAGE_STARTING}:
            self._mark(task_id, "releasing_ollama", "succeeded", "Releasing Ollama GPU resources.", provider="ollama")
            self._mark(task_id, "starting_comfy", "running", "Starting ComfyUI.", provider="comfyui")
        elif state is SchedulerState.IMAGE_ACTIVE:
            self._mark(task_id, "starting_comfy", "succeeded", "Starting ComfyUI.", provider="comfyui")
            self._mark(task_id, image_stage, "running", image_message, provider="comfyui")
        elif state is SchedulerState.SWITCH_TO_LLM and phase != "analysis":
            self._mark(task_id, image_stage, "succeeded", image_message, provider="comfyui")
            self._mark(task_id, "releasing_comfy", "running", "Releasing ComfyUI GPU resources.", provider="comfyui")
        elif state in {SchedulerState.LLM_ACTIVE, SchedulerState.LLM_STARTING} and phase == "analysis":
            self._mark(task_id, "analyzing", "running", "Analyzing image with Qwen.", model=self._model, provider="ollama")
        elif state in {SchedulerState.LLM_ACTIVE, SchedulerState.LLM_STARTING} and phase != "analysis":
            self._mark(task_id, "releasing_comfy", "succeeded", "Releasing ComfyUI GPU resources.", provider="comfyui")
            self._mark(task_id, "reviewing", "running", "Reviewing result with Qwen.", model=self._model, provider="ollama")

    def _complete(self, task_id: str, text: str, switch: str) -> None:
        current = self._store.get_task(task_id)
        if current is not None:
            for item in current.stages:
                if item.get("status") == "running" and isinstance(item.get("id"), str):
                    self._mark(task_id, item["id"], "succeeded", str(item.get("message") or "Completed."))
        self._mark(task_id, "completed", "succeeded", "Completed.")
        self._store.update_task(
            task_id,
            status="succeeded",
            stage="completed",
            summary=text,
            error=None,
            switch_result=switch,
            scheduler_job_id=None,
        )
        self._write_manifest(task_id)
        logger.info("studio task %s completed", task_id)

    def _fail(self, task_id: str, message: str, *, stage: str | None = None) -> None:
        current = self._store.get_task(task_id)
        if current is not None and task_is_terminal(current.status):
            return
        if stage:
            self._mark(task_id, stage, "failed", message)
        self._mark(task_id, "failed", "failed", "Failed.")
        self._store.update_task(task_id, status="failed", stage="failed", error=message[:500], scheduler_job_id=None)
        self._write_manifest(task_id)
        logger.info("studio task %s failed", task_id)

    def _mark(
        self,
        task_id: str,
        stage_id: str,
        status: str,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        task = self._store.get_task(task_id)
        if task is None:
            return
        stages = [dict(item) for item in task.stages]
        found = False
        for item in stages:
            if item.get("id") != stage_id:
                continue
            if item.get("status") == "succeeded" and status == "running":
                return
            item["status"] = status
            item["message"] = message
            if model:
                item["model"] = model
            if provider:
                item["provider"] = provider
            found = True
        if not found:
            stages.append(
                {
                    "id": stage_id,
                    "status": status,
                    "message": message,
                    "model": model,
                    "provider": provider,
                }
            )
        fields: dict[str, Any] = {"stages": stages}
        if status == "running":
            fields["stage"] = stage_id
        self._store.update_task(task_id, **fields)

    def _image_parts(self, task_id: str, role: str) -> list[tuple[str, bytes]]:
        parts: list[tuple[str, bytes]] = []
        for stored in self._store.list_files(task_id, role):
            path = self._store.resolve(stored.relative_path)
            if path is None:
                continue
            parts.append((stored.media_type, path.read_bytes()))
        return parts

    def _first_path(self, task_id: str, role: str) -> Path:
        for stored in self._store.list_files(task_id, role):
            path = self._store.resolve(stored.relative_path)
            if path is not None:
                return path
        raise OrchestratorError("The source image is missing.")

    def _write_bytes(self, task_id: str, name: str, data: bytes) -> Path:
        path = self._store.job_dir(task_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _write_json(self, task_id: str, name: str, payload: dict[str, Any]) -> None:
        self._write_bytes(task_id, name, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def _register(
        self,
        task_id: str,
        path: Path,
        *,
        role: str,
        media_type: str,
        width: int | None,
        height: int | None,
        label: str | None = None,
        asset_id: str | None = None,
    ) -> None:
        relative = relative_output(self._store.root, path)
        if relative is None:
            raise OrchestratorError("A task file was stored outside the task directory.")
        self._store.add_file(
            uuid.uuid4().hex,
            task_id=task_id,
            role=role,
            relative_path=relative,
            media_type=media_type,
            width=width,
            height=height,
            byte_size=path.stat().st_size,
            label=label,
            asset_id=asset_id,
        )

    def _write_manifest(self, task_id: str) -> None:
        task = self._store.get_task(task_id)
        if task is None:
            return
        files = [
            {
                "id": item.id,
                "role": item.role,
                "name": Path(item.relative_path).name,
                "media_type": item.media_type,
                "width": item.width,
                "height": item.height,
                "byte_size": item.byte_size,
                "asset_id": item.asset_id,
            }
            for item in self._store.list_files(task_id)
        ]
        payload = {
            "task_id": task.id,
            "status": task.status,
            "route": task.route,
            "prompt": task.prompt,
            "models": task.models,
            "assets": (task.plan or {}).get("assets", []),
            "files": files,
            "switch_result": task.switch_result,
            "error": task.error,
            "updated_at": task.updated_at,
        }
        if task.route in {"edit", "extract_and_edit", "generate"}:
            payload["flux"] = {
                "workflow": self._t2i_workflow if task.route == "generate" else self._edit_workflow,
                "steps": 4,
                "seed": 1,
                "megapixels": None if task.route == "generate" else 0.5,
            }
        self._write_json(task_id, "manifest.json", payload)

    def _require(self, task_id: str) -> StudioTask:
        task = self._store.get_task(task_id)
        if task is None:
            raise OrchestratorError("The task does not exist.")
        return task


def _instruction(task: StudioTask) -> str:
    assets = (task.plan or {}).get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, dict) and isinstance(asset.get("edit_instruction"), str):
                text = asset["edit_instruction"].strip()
                if text:
                    return text
    if isinstance(task.plan, dict) and isinstance(task.plan.get("description"), str):
        text = task.plan["description"].strip()
        if text:
            return text
    return task.prompt


def _safe_stem(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
    return cleaned[:64] or "asset"


def _dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError):
        return None, None


async def _read_exchange(exchange: ChatExchange) -> str:
    await exchange.ready.wait()
    chunks: list[bytes] = []
    while True:
        item = await exchange.chunks.get()
        if item is None:
            break
        chunks.append(item)
    if exchange.status_code != 200:
        raise OrchestratorError("Qwen did not return a completion.")
    try:
        payload = json.loads(b"".join(chunks))
    except json.JSONDecodeError as exc:
        raise OrchestratorError("Qwen returned an unreadable completion.") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OrchestratorError("Qwen returned an empty completion.")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise OrchestratorError("Qwen returned an empty completion.")
    return content
