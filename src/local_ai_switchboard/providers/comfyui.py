"""ComfyUI adapter. Image work runs only while the scheduler holds the GPU lease.

POST /free returning HTTP 200 only requests cleanup. Queue idle and free VRAM
are checked afterwards. A disabled registry entry cannot be submitted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.providers.base import JobContext, ProviderError, ProviderKind
from local_ai_switchboard.providers.comfy_process import (
    Launcher,
    ProcessHandle,
    SubprocessLauncher,
    build_argv,
    portable_main,
    portable_python,
)
from local_ai_switchboard.diagnostics.comfy_graph import validate_graph_support
from local_ai_switchboard.providers.workflows import (
    WorkflowError,
    WorkflowRegistry,
    apply_bindings,
)
from local_ai_switchboard.workflows import packaged_workflows_dir

logger = logging.getLogger("local_ai_switchboard.comfyui")

_PROMPT_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_FILENAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class Clock(Protocol):
    """Sleep source. Production waits on the wall clock."""

    def monotonic(self) -> float:
        """Return seconds from a monotonic clock."""

    async def sleep(self, seconds: float) -> None:
        """Wait for the given number of seconds."""


class WallClock:
    """The process monotonic clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class ComfyConfig:
    """Loopback ComfyUI, portable launch, and workflow locations."""

    base_url: str
    port: int
    portable_dir: Path | None
    workflows_dir: Path
    output_root: Path
    reserve_vram_mb: int | None
    memory_threshold_mb: int
    job_timeout_seconds: float
    poll_interval_seconds: float
    startup_timeout_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> ComfyConfig:
        port = settings.comfyui_url.port or 8188
        workflows = settings.workflows_dir or packaged_workflows_dir()
        return cls(
            base_url=str(settings.comfyui_url).rstrip("/"),
            port=port,
            portable_dir=settings.comfyui_portable_dir,
            workflows_dir=workflows,
            output_root=settings.resolved_storage_dir() / "outputs" / "images",
            reserve_vram_mb=settings.comfyui_reserve_vram_mb,
            memory_threshold_mb=settings.gpu_memory_threshold_mb,
            job_timeout_seconds=settings.job_timeout_seconds,
            poll_interval_seconds=settings.memory_poll_interval_seconds,
            startup_timeout_seconds=settings.comfyui_startup_timeout_seconds,
        )


@dataclass
class StoredImage:
    """One image copied out of ComfyUI into gateway storage."""

    filename: str
    path: Path
    media_type: str


class ImageExchange:
    """The image request the scheduler hands to ComfyUI."""

    def __init__(
        self,
        workflow_id: str,
        parameters: dict[str, Any],
        *,
        source: Path | None = None,
        output_dir: Path | None = None,
        asset_id: str | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.parameters = dict(parameters)
        self.source = source
        self.output_dir = output_dir
        self.asset_id = asset_id
        self.prompt_id: str | None = None
        self.outputs: list[StoredImage] = []


class ComfyUIProvider:
    """Local ComfyUI. The scheduler is the only caller of prompt and free."""

    kind = ProviderKind.IMAGE

    def __init__(
        self,
        config: ComfyConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        launcher: Launcher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._client = client
        self._owned = client is None
        self._launcher = launcher or SubprocessLauncher()
        self._clock = clock or WallClock()
        self._registry = WorkflowRegistry(config.workflows_dir)
        self._client_id = uuid.uuid4().hex
        self._busy = False
        self._child: ProcessHandle | None = None
        self._cancel_event = asyncio.Event()
        self.prepare_calls = 0
        for problem in self._registry.errors:
            logger.error("workflow registry: %s", problem)
        for manifest in self._registry.list():
            if not manifest.enabled:
                logger.info("workflow %s is disabled: %s", manifest.id, manifest.disabled_reason)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> ComfyUIProvider:
        return cls(ComfyConfig.from_settings(settings), **kwargs)

    @property
    def registry(self) -> WorkflowRegistry:
        return self._registry

    @property
    def output_root(self) -> Path:
        return self._config.output_root

    async def aclose(self) -> None:
        """Interrupt in-flight work and stop a child this gateway started."""

        try:
            if self._busy:
                self._cancel_event.set()
                await self._interrupt()
            await self._stop_child()
        finally:
            client = self._client
            self._client = None
            if client is not None and self._owned:
                await client.aclose()

    async def health(self) -> bool:
        """True when GET /system_stats returns a device list."""

        try:
            payload = await self._get_json("/system_stats")
        except ProviderError:
            return False
        devices = payload.get("devices")
        return isinstance(devices, list) and bool(devices)

    async def prepare(self) -> None:
        """Record that the scheduler selected ComfyUI.

        A down server fails the image job inside run() so the scheduler stays usable.
        """

        self.prepare_calls += 1

    async def run(self, job: JobContext) -> None:
        """Submit one registered workflow and return after history says it completed."""

        exchange = getattr(job, "payload", None)
        if not isinstance(exchange, ImageExchange):
            raise ProviderError("image job is missing a workflow request")
        manifest = self._registry.get(exchange.workflow_id)
        if manifest is None or manifest.workflow is None:
            reason = "Workflow is not available"
            if manifest is not None and manifest.disabled_reason:
                reason = manifest.disabled_reason
            raise ProviderError(reason)
        self._busy = True
        self._cancel_event.clear()
        try:
            if job.cancel_requested:
                return
            await self._ensure_server()
            if job.cancel_requested or self._cancel_event.is_set():
                return
            values = dict(exchange.parameters)
            if exchange.source is not None:
                values["image"] = await self._upload_image(exchange.source)
            try:
                graph = apply_bindings(manifest, values)
            except WorkflowError as exc:
                raise ProviderError(str(exc)) from exc
            if job.cancel_requested or self._cancel_event.is_set():
                return
            await self._assert_graph_supported(graph)
            if job.cancel_requested or self._cancel_event.is_set():
                return
            prompt_id = await self._submit_prompt(graph)
            exchange.prompt_id = prompt_id
            logger.info("comfyui prompt %s workflow=%s", prompt_id, manifest.id)
            entry = await self._wait_for_completion(job, prompt_id)
            if job.cancel_requested or self._cancel_event.is_set():
                return
            await self._copy_outputs(job, exchange, entry)
        except asyncio.CancelledError:
            await self._interrupt()
            raise
        finally:
            self._busy = False

    async def cancel(self, job: JobContext) -> None:
        """Interrupt the running prompt. Safe to call more than once."""

        del job
        self._cancel_event.set()
        await self._interrupt()

    async def release_resources(self) -> None:
        """Ask ComfyUI to unload models. HTTP 200 does not mean VRAM is free."""

        response = await self._request(
            "POST",
            "/free",
            {"unload_models": True, "free_memory": True},
        )
        if response.status_code != 200:
            raise ProviderError(
                "ComfyUI free request failed",
                diagnostics={"status": str(response.status_code)},
            )
        logger.info("comfyui free requested")

    async def resources_released(self) -> bool:
        """True only when the queue is idle and ``vram_free`` meets the threshold."""

        try:
            queue = await self._get_json("/queue")
            stats = await self._get_json("/system_stats")
        except ProviderError:
            return False
        return queue_is_idle(queue) and memory_is_free(stats, self._config.memory_threshold_mb)

    async def occupancy(self) -> str:
        """Inspect the configured ComfyUI. Do not treat a down port as a process to kill."""

        if not await self.health():
            return "absent"
        try:
            queue = await self._get_json("/queue")
            stats = await self._get_json("/system_stats")
        except ProviderError:
            return "unknown"
        if queue_is_idle(queue) and memory_is_free(stats, self._config.memory_threshold_mb):
            return "clear"
        return "occupied"

    async def is_busy(self) -> bool:
        return self._busy

    async def _ensure_server(self) -> None:
        if await self.health():
            return
        if self._child is not None and self._child.poll() is None:
            await self._wait_until_healthy()
            return
        portable = self._config.portable_dir
        if portable is None:
            raise ProviderError("ComfyUI is not running")
        python_exe = portable_python(portable)
        main_py = portable_main(portable)
        if not python_exe.is_file() or not main_py.is_file():
            raise ProviderError("ComfyUI portable installation was not found")
        argv = build_argv(portable, self._config.port, self._config.reserve_vram_mb)
        self._child = self._launcher.start(argv, portable)
        logger.info("started ComfyUI on 127.0.0.1:%s", self._config.port)
        await self._wait_until_healthy()

    async def _wait_until_healthy(self) -> None:
        deadline = self._clock.monotonic() + self._config.startup_timeout_seconds
        while self._clock.monotonic() < deadline:
            if await self.health():
                return
            if self._child is not None and self._child.poll() is not None:
                raise ProviderError("ComfyUI exited before it became healthy")
            await self._pause()
        raise ProviderError("ComfyUI did not become healthy")

    async def _assert_graph_supported(self, graph: dict[str, Any]) -> None:
        """Reject a graph ComfyUI cannot run. This does not submit the prompt."""

        classes = sorted(
            {
                node.get("class_type")
                for node in graph.values()
                if isinstance(node, dict) and isinstance(node.get("class_type"), str)
            }
        )
        available: dict[str, Any] = {}
        for class_type in classes:
            info = await self._object_info(class_type)
            if info is not None:
                available[class_type] = info
        problems = validate_graph_support(graph, available)
        if not problems:
            return
        detail = "; ".join(problems)
        if len(detail) > 1500:
            detail = detail[:1500]
        raise ProviderError(
            f"ComfyUI cannot run this workflow: {detail}",
            diagnostics={"object_info": detail[:500]},
        )

    async def _object_info(self, class_type: str) -> dict[str, Any] | None:
        path = "/object_info/" + quote(class_type, safe="")
        try:
            payload = await self._get_json(path)
        except ProviderError:
            return None
        info = payload.get(class_type)
        if not isinstance(info, dict):
            return None
        return info

    async def _submit_prompt(self, graph: dict[str, Any]) -> str:
        response = await self._request(
            "POST",
            "/prompt",
            {"prompt": graph, "client_id": self._client_id},
        )
        if response.status_code != 200:
            raise ProviderError(
                "ComfyUI rejected the prompt",
                diagnostics={"status": str(response.status_code)},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("ComfyUI rejected the prompt") from exc
        if not isinstance(payload, dict):
            raise ProviderError("ComfyUI rejected the prompt")
        node_errors = payload.get("node_errors") or {}
        if node_errors:
            raise ProviderError("ComfyUI rejected the prompt", diagnostics={"status": "node_errors"})
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or _PROMPT_ID.fullmatch(prompt_id) is None:
            raise ProviderError("ComfyUI did not return a prompt id")
        return prompt_id

    async def _wait_for_completion(self, job: JobContext, prompt_id: str) -> dict[str, Any]:
        deadline = self._clock.monotonic() + self._config.job_timeout_seconds
        while True:
            if job.cancel_requested or self._cancel_event.is_set():
                await self._interrupt()
                return {}
            await self._get_json("/queue")
            history = await self._get_json("/history/" + quote(prompt_id, safe=""))
            entry = history.get(prompt_id)
            if isinstance(entry, dict):
                completed, status = _completion(entry)
                if completed:
                    if status != "success":
                        message, extra = _execution_failure(entry)
                        extra["prompt_id"] = prompt_id
                        raise ProviderError(message, diagnostics=extra)
                    return entry
            if self._clock.monotonic() >= deadline:
                raise ProviderError(
                    "ComfyUI job timed out",
                    diagnostics={"prompt_id": prompt_id},
                )
            await self._pause()

    async def _copy_outputs(self, job: JobContext, exchange: ImageExchange, entry: dict[str, Any]) -> None:
        if job.cancel_requested or self._cancel_event.is_set():
            return
        images = [image for image in _output_images(entry) if image.get("type", "output") == "output"]
        if not images:
            raise ProviderError("ComfyUI completed without an output image")
        if len(images) > 8:
            raise ProviderError("ComfyUI returned too many images")
        destination = exchange.output_dir or (self._config.output_root / job.id)
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for image in images:
            filename = _safe_filename(str(image.get("filename", "")))
            subfolder = _safe_subfolder(image.get("subfolder", ""))
            stored_name = _unique_name(filename, used)
            response = await self._http().get(
                f"{self._config.base_url}/view",
                params={"filename": filename, "subfolder": subfolder, "type": "output"},
            )
            if response.status_code != 200 or not response.content:
                raise ProviderError("ComfyUI did not return the image")
            target = (destination / stored_name).resolve()
            if not target.is_relative_to(destination):
                raise ProviderError("Refusing to store an image outside the job directory")
            target.write_bytes(response.content)
            exchange.outputs.append(
                StoredImage(filename=stored_name, path=target, media_type=_media_type(stored_name))
            )
        if exchange.outputs:
            _write_completion_marker(destination, exchange)

    async def _upload_image(self, path: Path) -> str:
        """Copy a local file into ComfyUI's input folder. The bytes are not logged."""

        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ProviderError("The source image could not be read") from exc
        filename = path.name
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ProviderError("The source image name is not usable")
        try:
            response = await self._http().post(
                f"{self._config.base_url}/upload/image",
                data={"type": "input", "overwrite": "true"},
                files={"image": (filename, payload, "application/octet-stream")},
            )
        except httpx.HTTPError as exc:
            raise ProviderError("ComfyUI did not accept the source image") from exc
        if response.status_code != 200:
            raise ProviderError(
                "ComfyUI did not accept the source image",
                diagnostics={"status": str(response.status_code)},
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("ComfyUI did not accept the source image") from exc
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str):
            raise ProviderError("ComfyUI did not accept the source image")
        safe = _safe_filename(name)
        logger.info("comfyui upload name=%s bytes=%s", safe, len(payload))
        return safe

    async def _interrupt(self) -> None:
        try:
            response = await self._request("POST", "/interrupt", {})
        except ProviderError:
            logger.warning("comfyui interrupt failed")
            return
        if response.status_code not in {200, 204}:
            logger.warning("comfyui interrupt status=%s", response.status_code)

    async def _stop_child(self) -> None:
        child = self._child
        self._child = None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        exit_code = await asyncio.to_thread(child.wait, 2)
        if exit_code is None and child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, 2)

    async def _pause(self) -> None:
        try:
            await asyncio.wait_for(
                self._cancel_event.wait(),
                timeout=self._config.poll_interval_seconds,
            )
        except TimeoutError:
            return

    async def _get_json(self, path: str) -> dict[str, Any]:
        response = await self._request("GET", path, None)
        if response.status_code != 200:
            raise ProviderError("ComfyUI request failed", diagnostics={"path": path, "status": str(response.status_code)})
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("ComfyUI request failed", diagnostics={"path": path}) from exc
        if not isinstance(payload, dict):
            raise ProviderError("ComfyUI request failed", diagnostics={"path": path})
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> httpx.Response:
        try:
            return await self._http().request(method, f"{self._config.base_url}{path}", json=body)
        except httpx.HTTPError as exc:
            logger.warning("comfyui %s %s failed: %s", method, path, type(exc).__name__)
            raise ProviderError("ComfyUI is not reachable") from exc

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
            )
            self._owned = True
        return self._client


def queue_is_idle(payload: dict[str, Any]) -> bool:
    """True when ComfyUI has nothing running and nothing pending."""

    running = payload.get("queue_running")
    pending = payload.get("queue_pending")
    return isinstance(running, list) and isinstance(pending, list) and not running and not pending


def memory_is_free(payload: dict[str, Any], threshold_mb: int) -> bool:
    """True when ComfyUI reports enough available VRAM. Missing data is not free.

    ``vram_free`` is CUDA free memory plus unused caching-allocator bytes.
    ``torch_vram_free`` is only that unused reserved slice. After ``empty_cache``
    it is near zero while the card is free, so it is not a release signal.
    ``nvidia-smi`` still has to show that the reserved bytes were returned.
    """

    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
        return False
    value = devices[0].get("vram_free")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value >= threshold_mb * 1024 * 1024


def _execution_failure(entry: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Return the job error. History without an exception keeps the original text."""

    status = entry.get("status")
    status_text = ""
    if isinstance(status, dict):
        raw = status.get("status_str")
        if isinstance(raw, str):
            status_text = raw
    diagnostics = {"status": status_text or "error"}
    detail = _exception_text(status if isinstance(status, dict) else None)
    if not detail:
        return "ComfyUI execution failed", diagnostics
    diagnostics["exception_message"] = detail
    return f"ComfyUI execution failed: {detail}", diagnostics


def _exception_text(status: dict[str, Any] | None) -> str:
    if not isinstance(status, dict):
        return ""
    messages = status.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for item in messages:
        body: object = None
        if isinstance(item, dict):
            body = item
        elif isinstance(item, list) and len(item) >= 2:
            body = item[1]
        if not isinstance(body, dict):
            continue
        message = body.get("exception_message")
        if not isinstance(message, str) or not message.strip():
            continue
        kind = body.get("exception_type")
        text = message.strip()
        if isinstance(kind, str) and kind.strip():
            text = f"{kind.strip()}: {text}"
        parts.append(" ".join(text.split()))
    if not parts:
        return ""
    detail = " ".join(parts)
    if len(detail) > 500:
        return detail[:500]
    return detail


def _completion(entry: dict[str, Any]) -> tuple[bool, str]:
    status = entry.get("status")
    if not isinstance(status, dict) or status.get("completed") is not True:
        return False, ""
    text = status.get("status_str")
    return True, text if isinstance(text, str) else ""


def _output_images(entry: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return []
    images: list[dict[str, Any]] = []
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        listed = node_output.get("images")
        if not isinstance(listed, list):
            continue
        images.extend(image for image in listed if isinstance(image, dict))
    return images


def _write_completion_marker(directory: Path, exchange: ImageExchange) -> None:
    """Record a finished file before SQLite commit so a failed commit can be reconciled."""

    stored = exchange.outputs[0]
    relative = f"{directory.name}/{stored.filename}"
    payload = {
        "output_path": relative,
        "media_type": stored.media_type,
        "workflow_id": exchange.workflow_id,
    }
    temporary = directory / ".complete.partial"
    marker = directory / ".complete"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, marker)


def _safe_filename(filename: str) -> str:
    if _FILENAME.fullmatch(filename) is None or Path(filename).name != filename:
        raise ProviderError("ComfyUI returned an unsafe image filename")
    return filename


def _safe_subfolder(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ProviderError("ComfyUI returned an unsafe image subfolder")
    parts = Path(value).parts
    if value.startswith(("/", "\\")) or any(part in {".", ".."} for part in parts):
        raise ProviderError("ComfyUI returned an unsafe image subfolder")
    if not all(_FILENAME.fullmatch(part) for part in parts):
        raise ProviderError("ComfyUI returned an unsafe image subfolder")
    return value


def _unique_name(filename: str, used: set[str]) -> str:
    if filename not in used:
        used.add(filename)
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    number = 2
    while True:
        candidate = f"{stem}-{number}{suffix}"
        if candidate not in used and _FILENAME.fullmatch(candidate):
            used.add(candidate)
            return candidate
        number += 1


def _media_type(filename: str) -> str:
    return _MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")
