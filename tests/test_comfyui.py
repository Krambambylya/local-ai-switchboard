"""ComfyUI image jobs against a mocked local server. No portable process is started."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_ai_switchboard.api import assets as assets_module
from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.monitor import NvidiaGpuMonitor
from local_ai_switchboard.gpu.nvidia_smi import GpuDevice, GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.base import ProviderError, ProviderKind
from local_ai_switchboard.providers.comfy_process import (
    SubprocessLauncher,
    assert_explicit_listen,
    build_argv,
)
from local_ai_switchboard.providers.comfyui import (
    ComfyConfig,
    ComfyUIProvider,
    ImageExchange,
    memory_is_free,
)
from local_ai_switchboard.providers.unavailable import UnavailableProvider
from local_ai_switchboard.providers.workflows import (
    WorkflowError,
    WorkflowRegistry,
    apply_bindings,
    validate_manifest,
)
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import JobStatus, SchedulerConfig, SchedulerState
from local_ai_switchboard.services.logging import flush_logs
from local_ai_switchboard.workflows import packaged_workflows_dir

PROMPT = "UNIQUE-IMAGE-PROMPT"
PNG = b"\x89PNG\r\n\x1a\nfake-image"
FREE_BODY = {"unload_models": True, "free_memory": True}
SAMPLE_REASON = (
    "A real ComfyUI API-format workflow export is required before image generation can be enabled."
)

FIXTURE = {
    "id": "fixture-image",
    "title": "Test fixture, not a FLUX graph",
    "enabled": True,
    "workflow": {
        "source": {
            "class_type": "FixtureSource",
            "inputs": {"text": "", "width": 768, "height": 768, "seed": 0, "batch": 1},
        }
    },
    "bindings": {
        "prompt": {"node": "source", "input": "text"},
        "width": {"node": "source", "input": "width"},
        "height": {"node": "source", "input": "height"},
        "seed": {"node": "source", "input": "seed"},
        "batch_size": {"node": "source", "input": "batch"},
    },
    "required": ["prompt"],
    "defaults": {"width": 768, "height": 768, "batch_size": 1},
}


class ComfyServer:
    """Minimal ComfyUI HTTP stand-in. HTTP 200 from /free does not change VRAM."""

    def __init__(self) -> None:
        self.healthy = True
        self.vram_free = 8 * 1024**3
        self.torch_vram_free = 8 * 1024**3
        self.queue_running: list[object] | None = []
        self.queue_pending: list[object] | None = []
        self.prompts: list[dict[str, object]] = []
        self.frees: list[object] = []
        self.interrupts = 0
        self.views: list[dict[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.history_reads = 0
        self.complete_after = 2
        self.history_mode = "success"
        self.free_status = 200
        self.release_on_free = False
        self.images: list[dict[str, str]] = [
            {"filename": "fixture.png", "subfolder": "", "type": "output"}
        ]
        self.error_message = ""
        self.image_bytes = PNG
        self.object_catalog: dict[str, dict[str, object]] = {
            "FixtureSource": {
                "input": {
                    "required": {
                        "text": ["STRING", {}],
                        "width": ["INT", {}],
                        "height": ["INT", {}],
                        "seed": ["INT", {}],
                        "batch": ["INT", {}],
                    }
                },
                "output": ["IMAGE"],
            }
        }
        self.prompted = threading.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/system_stats":
            if not self.healthy:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "vram_free": self.vram_free,
                            "torch_vram_free": self.torch_vram_free,
                        }
                    ]
                },
            )
        if path == "/prompt" and request.method == "POST":
            body = json.loads(request.content)
            self.prompts.append(body)
            self.queue_running = [[1, "prompt-1", {}, {}, []]]
            self.prompted.set()
            return httpx.Response(
                200,
                json={"prompt_id": "prompt-1", "number": 1, "node_errors": {}},
            )
        if path == "/queue":
            return httpx.Response(
                200,
                json={"queue_running": self.queue_running, "queue_pending": self.queue_pending},
            )
        if path.startswith("/history/"):
            self.history_reads += 1
            if self.history_mode == "never" or self.history_reads < self.complete_after:
                return httpx.Response(200, json={})
            self.queue_running = []
            if self.history_mode == "error":
                status: dict[str, object] = {"completed": True, "status_str": "error"}
                if self.error_message:
                    status["messages"] = [
                        [
                            "execution_error",
                            {
                                "exception_type": "torch.OutOfMemoryError",
                                "exception_message": self.error_message,
                            },
                        ]
                    ]
                entry = {"status": status, "outputs": {}}
            else:
                entry = {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"result": {"images": self.images}},
                }
            return httpx.Response(200, json={"prompt-1": entry})
        if path == "/view":
            self.views.append(dict(request.url.params))
            return httpx.Response(200, content=self.image_bytes)
        if path == "/free":
            self.frees.append(json.loads(request.content))
            if self.release_on_free:
                self.vram_free = 8 * 1024**3
                self.torch_vram_free = 8 * 1024**3
                self.queue_running = []
                self.queue_pending = []
            return httpx.Response(self.free_status, json={})
        if path == "/interrupt":
            self.interrupts += 1
            return httpx.Response(200, json={})
        if path.startswith("/object_info/"):
            name = path.removeprefix("/object_info/")
            info = self.object_catalog.get(name)
            if info is None:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={name: info})
        return httpx.Response(404, json={"path": path})


class Headroom:
    def __init__(self, free_mb: int = 7000) -> None:
        self.free_mb = free_mb

    async def snapshot(self) -> GpuSnapshot:
        device = GpuDevice(
            index=0,
            name="NVIDIA GeForce RTX 4060",
            memory_total_mb=8188,
            memory_used_mb=max(0, 8188 - self.free_mb),
            memory_free_mb=self.free_mb,
            utilization_gpu=0,
        )
        return GpuSnapshot(True, (device,), ())


class LlmStub:
    kind = ProviderKind.LLM

    def __init__(self) -> None:
        self.prepare_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare(self) -> None:
        self.prepare_calls += 1

    async def run(self, job: object) -> None:
        del job
        self.started.set()
        await self.release.wait()

    async def cancel(self, job: object) -> None:
        del job
        self.release.set()

    async def release_resources(self) -> None:
        return None

    async def resources_released(self) -> bool:
        return True

    async def is_busy(self) -> bool:
        return False


class RecordingLauncher:
    def __init__(self, server: ComfyServer, child: FakeChild) -> None:
        self.server = server
        self.child = child
        self.calls: list[tuple[list[str], Path]] = []

    def start(self, argv: list[str], cwd: Path) -> FakeChild:
        self.calls.append((list(argv), Path(cwd)))
        self.server.healthy = True
        return self.child


class FakeChild:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int | None:
        del timeout
        return self.exit_code


class BoomLauncher:
    def start(self, argv: list[str], cwd: Path) -> FakeChild:
        raise AssertionError(f"launch was not expected: {argv} {cwd}")


def _write_registry(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fixture-image.json").write_text(json.dumps(FIXTURE), encoding="utf-8")
    sample = packaged_workflows_dir() / "flux2-klein-4b.json"
    (directory / sample.name).write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")


def _touch_portable(root: Path) -> None:
    (root / "python_embeded").mkdir(parents=True)
    (root / "python_embeded" / "python.exe").write_bytes(b"")
    (root / "ComfyUI").mkdir()
    (root / "ComfyUI" / "main.py").write_text("print('comfy')\n", encoding="utf-8")


def _provider(
    settings: Settings,
    workflows: Path,
    server: ComfyServer,
    *,
    portable: Path | None = None,
    reserve: int | None = None,
    launcher: object | None = None,
    port: int = 8188,
    job_timeout: float = 2,
    poll: float = 0.01,
    startup: float = 1,
) -> ComfyUIProvider:
    config = ComfyConfig(
        base_url=f"http://127.0.0.1:{port}",
        port=port,
        portable_dir=portable,
        workflows_dir=workflows,
        output_root=settings.resolved_storage_dir() / "outputs" / "images",
        reserve_vram_mb=reserve,
        memory_threshold_mb=1024,
        job_timeout_seconds=job_timeout,
        poll_interval_seconds=poll,
        startup_timeout_seconds=startup,
    )
    return ComfyUIProvider(
        config,
        transport=httpx.MockTransport(server.handler),
        launcher=launcher,  # type: ignore[arg-type]
    )


def _scheduler(
    provider: ComfyUIProvider,
    llm: LlmStub | None = None,
    monitor: Headroom | None = None,
    *,
    job_timeout: float = 5,
    release_timeout: float = 0.05,
) -> GpuScheduler:
    return GpuScheduler(
        config=SchedulerConfig(
            job_timeout_seconds=job_timeout,
            release_timeout_seconds=release_timeout,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers={
            ProviderKind.LLM: llm or LlmStub(),
            ProviderKind.IMAGE: provider,
        },
        monitor=monitor or Headroom(),
    )


async def _image_job(scheduler: GpuScheduler, prompt: str = PROMPT) -> object:
    job = await scheduler.submit(
        ProviderKind.IMAGE,
        payload=ImageExchange("fixture-image", {"prompt": prompt}),
    )
    return await asyncio.wait_for(job.done, timeout=2)


def _wait_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.01)
    raise AssertionError(last)


def test_packaged_sample_has_no_api_graph() -> None:
    sample_path = packaged_workflows_dir() / "flux2-klein-4b.json"
    text = sample_path.read_text(encoding="utf-8")
    assert "class_type" not in text
    manifest = WorkflowRegistry(packaged_workflows_dir()).get("flux2-klein-4b")
    assert manifest is not None
    assert manifest.enabled is False
    assert manifest.workflow is None
    assert SAMPLE_REASON in (manifest.disabled_reason or "")
    assert ComfyConfig.from_settings(Settings()).workflows_dir == packaged_workflows_dir()


def test_bindings_require_an_existing_scalar_input(tmp_path: Path) -> None:
    missing = json.loads(json.dumps(FIXTURE))
    missing["bindings"]["prompt"]["node"] = "missing"
    with pytest.raises(WorkflowError, match="missing node"):
        validate_manifest(missing)
    linked = json.loads(json.dumps(FIXTURE))
    linked["workflow"]["source"]["inputs"]["text"] = ["other", 0]
    with pytest.raises(WorkflowError, match="linked input"):
        validate_manifest(linked)

    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "good.json").write_text(json.dumps(FIXTURE), encoding="utf-8")
    (directory / "bad.json").write_text(json.dumps(missing), encoding="utf-8")
    registry = WorkflowRegistry(directory)
    assert registry.get("fixture-image") is not None
    assert registry.errors
    assert "missing node" in registry.errors[0]

    manifest = validate_manifest(FIXTURE)
    graph = apply_bindings(manifest, {"prompt": "hello", "width": 512, "height": 640, "batch_size": 1})
    assert graph["source"]["inputs"]["text"] == "hello"
    assert manifest.workflow is not None
    assert manifest.workflow["source"]["inputs"]["text"] == ""
    again = apply_bindings(manifest, {"prompt": "second"})
    assert again["source"]["inputs"]["text"] == "second"
    assert manifest.workflow["source"]["inputs"]["text"] == ""
    with pytest.raises(WorkflowError, match="integer"):
        apply_bindings(manifest, {"prompt": "hello", "width": True})


def test_launch_arguments_listen_on_loopback_only(tmp_path: Path) -> None:
    portable = tmp_path / "ComfyUI_windows_portable"
    settings = Settings(
        install_root=tmp_path,
        storage_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        comfyui_url="http://127.0.0.1:8190",
        comfyui_portable_dir=portable,
        comfyui_reserve_vram_mb=1024,
    )
    config = ComfyConfig.from_settings(settings)
    argv = build_argv(config.portable_dir or portable, config.port, config.reserve_vram_mb)
    assert Path(argv[0]).parts[-2:] == ("python_embeded", "python.exe")
    assert "python_embedded" not in argv[0]
    assert argv[1] == "-s"
    assert Path(argv[2]).parts[-2:] == ("ComfyUI", "main.py")
    assert argv.count("--listen") == 1
    assert argv[argv.index("--listen") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8190"
    assert "--disable-auto-launch" in argv
    assert "--windows-standalone-build" in argv
    assert argv[argv.index("--reserve-vram") + 1] == "1024"
    assert "0.0.0.0" not in argv
    plain = build_argv(portable, 8188, None)
    assert "--reserve-vram" not in plain
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        assert_explicit_listen(["python", "--listen"])
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        assert_explicit_listen(["python", "--listen", "0.0.0.0"])


def test_launcher_uses_a_list_and_disables_the_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "local_ai_switchboard.providers.comfy_process.subprocess.Popen",
        FakePopen,
    )
    argv = build_argv(tmp_path, 8188, None)
    SubprocessLauncher().start(argv, tmp_path)
    assert isinstance(captured["args"], list)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_routes_do_not_call_comfy_directly() -> None:
    source = inspect.getsource(assets_module)
    assert "httpx" not in source
    assert "/prompt" not in source
    assert "8188" not in source


def test_image_lifecycle_uses_the_registered_workflow(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
) -> None:
    workflows = settings.resolved_storage_dir() / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    provider = _provider(settings, workflows, server)
    providers = {
        ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
        ProviderKind.IMAGE: provider,
    }
    scheduler = GpuScheduler(
        config=SchedulerConfig(
            job_timeout_seconds=5,
            release_timeout_seconds=0.2,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers=providers,
        monitor=NvidiaGpuMonitor(lambda: unavailable_gpu),
    )
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers=providers,
        scheduler=scheduler,
    )
    with TestClient(app) as client:
        assert client.post("/prompt", json={}).status_code == 404
        assert client.get("/queue").status_code == 404
        assert client.post("/free", json={}).status_code == 404
        open_asset = client.post(
            "/v1/assets",
            headers={"Authorization": "Bearer not-a-credential"},
            json={"workflow_id": "no-such-workflow", "prompt": "hi"},
        )
        assert open_asset.status_code == 404

        listed = client.get("/v1/workflows")
        assert listed.status_code == 200
        by_id = {item["id"]: item for item in listed.json()["workflows"]}
        assert by_id["flux2-klein-4b"]["enabled"] is False
        assert SAMPLE_REASON in by_id["flux2-klein-4b"]["disabled_reason"]
        assert by_id["fixture-image"]["parameters"] == ["batch_size", "height", "prompt", "seed", "width"]
        assert "class_type" not in listed.text
        assert "FixtureSource" not in listed.text

        disabled = client.post(
            "/v1/assets",
            json={"workflow_id": "flux2-klein-4b", "prompt": PROMPT},
        )
        assert disabled.status_code == 409
        assert SAMPLE_REASON in disabled.text
        unknown = client.post(
            "/v1/assets",
            json={"workflow_id": "no-such-workflow", "prompt": PROMPT},
        )
        assert unknown.status_code == 404
        rejected = client.post(
            "/v1/assets",
            json={
                "workflow_id": "fixture-image",
                "prompt": PROMPT,
                "negative_prompt": "nope",
                "output_format": "png",
            },
        )
        assert rejected.status_code == 400
        assert "negative_prompt" in rejected.text
        assert "output_format" in rejected.text
        graph = client.post(
            "/v1/assets",
            json={
                "workflow_id": "fixture-image",
                "prompt": PROMPT,
                "workflow": {"9": {"class_type": "KSampler", "inputs": {}}},
            },
        )
        assert graph.status_code == 400
        assert "workflow" in graph.json()["error"]["message"]
        assert server.prompts == []

        created = client.post(
            "/v1/assets",
            json={"workflow_id": "fixture-image", "prompt": PROMPT, "width": 512, "height": 512},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        finished = _wait_job(client, job_id)
        assert finished["status"] == "succeeded"
        assert finished["workflow_id"] == "fixture-image"
        assert finished["prompt_id"] == "prompt-1"
        assert finished["outputs"] == [{"filename": "fixture.png", "media_type": "image/png"}]
        assert str(settings.resolved_storage_dir()) not in json.dumps(finished)
        assert PROMPT not in json.dumps(finished)

        submitted = server.prompts[0]["prompt"]
        assert isinstance(submitted, dict)
        source = submitted["source"]
        assert isinstance(source, dict)
        assert source["class_type"] == "FixtureSource"
        inputs = source["inputs"]
        assert isinstance(inputs, dict)
        assert inputs["text"] == PROMPT
        assert inputs["width"] == 512
        assert inputs["height"] == 512
        assert inputs["batch"] == 1
        encoded = json.dumps(server.prompts[0])
        assert "FLUX" not in encoded
        assert "Klein" not in encoded
        assert "KSampler" not in encoded
        assert server.history_reads >= 2
        assert ("GET", "/queue") in server.calls
        assert ("GET", "/history/prompt-1") in server.calls
        assert server.views[0]["filename"] == "fixture.png"
        assert server.views[0]["type"] == "output"
        stored = provider.registry.get("fixture-image")
        assert stored is not None and stored.workflow is not None
        assert stored.workflow["source"]["inputs"]["text"] == ""

        download = client.get(f"/v1/jobs/{job_id}/download")
        assert download.status_code == 200
        assert download.content == PNG
        written = list((settings.resolved_storage_dir() / "outputs" / "images").rglob("*.png"))
        assert len(written) == 1
        assert written[0].read_bytes() == PNG
        assert written[0].is_relative_to(provider.output_root.resolve())
        assert server.frees == []
        flush_logs()
        log_text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
        assert PROMPT not in log_text

    assert server.frees == [FREE_BODY]


async def test_execution_error_and_timeout_write_no_file(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.history_mode = "error"
    server.complete_after = 1
    provider = _provider(settings, workflows, server)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
        assert failed.status is JobStatus.FAILED
        assert failed.error == "ComfyUI execution failed"
        assert server.views == []
        assert _stored_files(provider) == []

        server.history_mode = "never"
        server.history_reads = 0
        server.prompts.clear()
        provider._config = _replaced(provider, job_timeout_seconds=0.05)
        timed_out = await _image_job(scheduler)
        assert timed_out.status is JobStatus.FAILED
        assert timed_out.error == "ComfyUI job timed out"
        assert server.views == []
        assert _stored_files(provider) == []
    finally:
        await scheduler.stop()
        await provider.aclose()


async def test_active_and_queued_cancel(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.history_mode = "never"
    provider = _provider(settings, workflows, server, job_timeout=30, poll=5)
    scheduler = _scheduler(provider, job_timeout=30, release_timeout=0.2)
    await scheduler.start()
    try:
        first = await scheduler.submit(
            ProviderKind.IMAGE,
            payload=ImageExchange("fixture-image", {"prompt": PROMPT}),
        )
        assert await asyncio.to_thread(server.prompted.wait, 2)
        second = await scheduler.submit(
            ProviderKind.IMAGE,
            payload=ImageExchange("fixture-image", {"prompt": "queued-prompt"}),
        )
        await scheduler.cancel(second.id)
        assert (await second.done).status is JobStatus.CANCELLED
        assert len(server.prompts) == 1
        assert server.interrupts == 0
        await scheduler.cancel(first.id)
        assert (await asyncio.wait_for(first.done, timeout=2)).status is JobStatus.CANCELLED
        assert server.interrupts >= 1
        assert server.views == []
        assert _stored_files(provider) == []
    finally:
        await scheduler.stop()
        await provider.aclose()


def test_empty_cache_is_free_when_vram_free_meets_the_threshold() -> None:
    assert memory_is_free({"devices": [{"vram_free": 7_000_000_000, "torch_vram_free": 0}]}, 1024)
    assert not memory_is_free(
        {"devices": [{"vram_free": 1000, "torch_vram_free": 7_000_000_000}]},
        1024,
    )
    assert not memory_is_free({"devices": [{}]}, 1024)


async def test_free_response_is_not_proof_of_free_vram(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.complete_after = 1
    server.vram_free = 1000
    server.torch_vram_free = 1000
    provider = _provider(settings, workflows, server)
    await provider.release_resources()
    assert server.frees == [FREE_BODY]
    assert await provider.resources_released() is False
    server.vram_free = 8 * 1024**3
    server.torch_vram_free = 0
    server.queue_running = []
    assert await provider.resources_released() is True
    server.torch_vram_free = 8 * 1024**3
    server.queue_running = [[1, "prompt-1", {}, {}, []]]
    assert await provider.resources_released() is False
    server.queue_running = []
    assert await provider.resources_released() is True

    monitor = Headroom(588)
    llm = LlmStub()
    scheduler = _scheduler(provider, llm, monitor)
    await scheduler.start()
    try:
        server.vram_free = 8 * 1024**3
        server.torch_vram_free = 8 * 1024**3
        finished = await _image_job(scheduler)
        assert finished.status is JobStatus.SUCCEEDED
        blocked = await scheduler.submit(ProviderKind.LLM)
        outcome = await asyncio.wait_for(blocked.done, timeout=2)
        assert outcome.status is JobStatus.FAILED
        details = dict(outcome.diagnostics)
        assert details["provider_released"] == "true"
        assert "588" in details["memory"]
        assert llm.prepare_calls == 0
        status = await scheduler.status()
        assert status.state is SchedulerState.ERROR
        assert server.vram_free == 8 * 1024**3
        assert all(body == FREE_BODY for body in server.frees)
    finally:
        await scheduler.stop()
        await provider.aclose()


async def test_low_comfy_memory_blocks_the_next_provider_until_recover(
    settings: Settings,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.complete_after = 1
    provider = _provider(settings, workflows, server)
    monitor = Headroom(7000)
    llm = LlmStub()
    scheduler = _scheduler(provider, llm, monitor)
    await scheduler.start()
    try:
        finished = await _image_job(scheduler)
        assert finished.status is JobStatus.SUCCEEDED
        server.vram_free = 1000
        server.torch_vram_free = 1000
        blocked = await scheduler.submit(ProviderKind.LLM)
        outcome = await asyncio.wait_for(blocked.done, timeout=2)
        assert outcome.status is JobStatus.FAILED
        assert dict(outcome.diagnostics)["provider_released"] == "false"
        assert llm.prepare_calls == 0
        assert (await scheduler.status()).state is SchedulerState.ERROR

        server.vram_free = 8 * 1024**3
        server.torch_vram_free = 8 * 1024**3
        server.queue_running = []
        server.queue_pending = []
        await scheduler.recover()
        assert (await scheduler.status()).state is SchedulerState.IDLE
        started = await scheduler.submit(ProviderKind.LLM)
        await asyncio.wait_for(llm.started.wait(), timeout=2)
        assert llm.prepare_calls == 1
        llm.release.set()
        assert (await asyncio.wait_for(started.done, timeout=2)).status is JobStatus.SUCCEEDED
    finally:
        llm.release.set()
        await scheduler.stop()
        await provider.aclose()


async def test_discovery_launch_and_shutdown(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    portable = tmp_path / "ComfyUI_windows_portable"
    _touch_portable(portable)
    server = ComfyServer()
    server.healthy = False
    server.complete_after = 1
    child = FakeChild()
    launcher = RecordingLauncher(server, child)
    provider = _provider(
        settings,
        workflows,
        server,
        portable=portable,
        reserve=512,
        launcher=launcher,
        port=8190,
    )
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        first = await _image_job(scheduler)
        assert first.status is JobStatus.SUCCEEDED
        assert len(launcher.calls) == 1
        argv, cwd = launcher.calls[0]
        assert argv == build_argv(portable, 8190, 512)
        assert cwd == portable
        assert provider.prepare_calls == 1
        second = await _image_job(scheduler, "second-prompt")
        assert second.status is JobStatus.SUCCEEDED
        assert len(launcher.calls) == 1
        assert provider.prepare_calls == 1
        assert child.terminated is False
        server.free_status = 500
        with pytest.raises(ProviderError, match="free request failed"):
            await provider.release_resources()
        assert child.terminated is False
        assert child.killed is False
        server.free_status = 200
    finally:
        await scheduler.stop()
        await provider.aclose()
    assert child.terminated is True
    assert child.killed is False


async def test_missing_install_and_discovered_server_are_not_relaunched(
    settings: Settings,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.healthy = False
    empty = tmp_path / "empty-portable"
    empty.mkdir()
    launcher = RecordingLauncher(server, FakeChild())
    provider = _provider(settings, workflows, server, portable=empty, launcher=launcher)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        missing = await _image_job(scheduler)
        assert missing.status is JobStatus.FAILED
        assert missing.error is not None
        assert "portable installation was not found" in missing.error
        assert launcher.calls == []
        assert server.prompts == []
    finally:
        await scheduler.stop()
        await provider.aclose()

    server.healthy = False
    discovered = _provider(settings, workflows, server, portable=None, launcher=BoomLauncher())
    scheduler = _scheduler(discovered)
    await scheduler.start()
    try:
        down = await _image_job(scheduler)
        assert down.status is JobStatus.FAILED
        assert down.error is not None and "ComfyUI is not running" in down.error
    finally:
        await scheduler.stop()
        await discovered.aclose()

    server.healthy = True
    server.complete_after = 1
    server.history_mode = "success"
    running = _provider(settings, workflows, server, portable=None, launcher=BoomLauncher())
    scheduler = _scheduler(running)
    await scheduler.start()
    try:
        found = await _image_job(scheduler)
        assert found.status is JobStatus.SUCCEEDED
    finally:
        await scheduler.stop()
        await running.aclose()


async def test_unsafe_filename_is_not_stored(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.complete_after = 1
    server.images = [{"filename": "../secret.png", "subfolder": "", "type": "output"}]
    provider = _provider(settings, workflows, server)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
        assert failed.status is JobStatus.FAILED
        assert failed.error is not None and "unsafe image filename" in failed.error
        assert server.views == []
        assert not (tmp_path / "secret.png").exists()
    finally:
        await scheduler.stop()
        await provider.aclose()


def _stored_files(provider: ComfyUIProvider) -> list[Path]:
    if not provider.output_root.exists():
        return []
    return list(provider.output_root.rglob("*"))


async def test_startup_frees_running_comfyui_without_killing_it(
    settings: Settings,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.vram_free = 1000
    server.torch_vram_free = 1000
    server.release_on_free = True
    child = FakeChild()
    launcher = RecordingLauncher(server, child)
    provider = _provider(settings, workflows, server, launcher=launcher)
    scheduler = _scheduler(provider, monitor=Headroom(7000))
    await scheduler.start()
    try:
        assert scheduler.recovery_ok is True
        assert server.frees == [FREE_BODY]
        assert launcher.calls == []
        assert child.terminated is False
        assert child.killed is False
        assert (await scheduler.status()).state is SchedulerState.IDLE
    finally:
        await scheduler.stop()
        await provider.aclose()


async def test_shutdown_kills_a_child_that_ignores_terminate(
    settings: Settings,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    portable = tmp_path / "ComfyUI_windows_portable"
    _touch_portable(portable)
    server = ComfyServer()
    server.healthy = False
    server.complete_after = 1

    class StubbornChild(FakeChild):
        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int | None:
            del timeout
            if self.killed:
                self.exit_code = -9
                return -9
            return None

        def poll(self) -> int | None:
            return self.exit_code

    child = StubbornChild()
    launcher = RecordingLauncher(server, child)
    provider = _provider(settings, workflows, server, portable=portable, launcher=launcher)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        finished = await _image_job(scheduler)
        assert finished.status is JobStatus.SUCCEEDED
        assert launcher.calls
    finally:
        await scheduler.stop()
        await provider.aclose()
    assert child.terminated is True
    assert child.killed is True


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
    )


def _support_catalog(graph: dict[str, object]) -> dict[str, dict[str, object]]:
    classes: dict[str, dict[str, object]] = {}
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not isinstance(inputs, dict):
            continue
        spec = classes.setdefault(class_type, {"input": {"required": {}}, "output": ["OUTPUT"]})
        required = spec["input"]["required"]
        assert isinstance(required, dict)
        for name, value in inputs.items():
            if isinstance(value, str) and name in {
                "unet_name",
                "clip_name",
                "vae_name",
                "sampler_name",
                "weight_dtype",
                "type",
                "device",
            }:
                current = required.get(name)
                if not isinstance(current, list) or not current or not isinstance(current[0], list):
                    required[name] = [[value]]
                elif value not in current[0]:
                    current[0].append(value)
            elif isinstance(value, str):
                required.setdefault(name, ["STRING", {}])
            elif isinstance(value, bool):
                required.setdefault(name, ["BOOLEAN", {}])
            elif isinstance(value, float):
                required.setdefault(name, ["FLOAT", {}])
            elif isinstance(value, int):
                required.setdefault(name, ["INT", {}])
            elif isinstance(value, list):
                required.setdefault(name, ["*"])
    return classes


async def test_missing_node_class_is_rejected_before_submit(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.object_catalog = {}
    provider = _provider(settings, workflows, server)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
    finally:
        await scheduler.stop()
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None
    assert "FixtureSource" in failed.error
    assert server.prompts == []


async def test_startup_failure_does_not_submit_a_prompt(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.healthy = False
    portable = tmp_path / "ComfyUI_windows_portable"
    _touch_portable(portable)

    class ExitedChild(FakeChild):
        def __init__(self) -> None:
            super().__init__()
            self.exit_code = 1

    class ExitedLauncher:
        def __init__(self) -> None:
            self.calls = 0

        def start(self, argv: list[str], cwd: Path) -> ExitedChild:
            del argv, cwd
            self.calls += 1
            return ExitedChild()

    launcher = ExitedLauncher()
    provider = _provider(settings, workflows, server, portable=portable, launcher=launcher)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
    finally:
        await scheduler.stop()
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None
    assert "exited before it became healthy" in failed.error
    assert server.prompts == []
    assert launcher.calls == 1


async def test_cuda_oom_text_is_returned_on_the_job(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.history_mode = "error"
    server.complete_after = 1
    server.error_message = "CUDA out of memory. Tried to allocate 20.00 MiB."
    provider = _provider(settings, workflows, server)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
    finally:
        await scheduler.stop()
    assert failed.status is JobStatus.FAILED
    assert failed.error == (
        "ComfyUI execution failed: torch.OutOfMemoryError: "
        "CUDA out of memory. Tried to allocate 20.00 MiB."
    )
    assert server.prompts
    assert _stored_files(provider) == []


async def test_completed_job_without_an_image_stores_nothing(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.images = []
    server.complete_after = 1
    provider = _provider(settings, workflows, server)
    scheduler = _scheduler(provider)
    await scheduler.start()
    try:
        failed = await _image_job(scheduler)
    finally:
        await scheduler.stop()
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None
    assert "without an output image" in failed.error
    assert _stored_files(provider) == []


async def test_klein_job_uses_the_scheduler_and_gateway_download(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
    tmp_path: Path,
) -> None:
    from local_ai_switchboard.diagnostics.flux2_klein import DIAGNOSTIC_PROMPT
    from local_ai_switchboard.diagnostics.image_pipeline import png_dimensions
    from local_ai_switchboard.main import create_app

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    manifest = Path(__file__).resolve().parents[1] / "workflows" / "flux2-klein-t2i" / "manifest.json"
    (workflows / "flux2-klein-t2i.json").write_bytes(manifest.read_bytes())
    sample = packaged_workflows_dir() / "flux2-klein-4b.json"
    (workflows / sample.name).write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    graph = json.loads(manifest.read_text(encoding="utf-8"))["workflow"]
    server = ComfyServer()
    server.object_catalog = _support_catalog(graph)
    server.image_bytes = _png(768, 768)
    server.complete_after = 1
    provider = _provider(settings, workflows, server)
    providers = {
        ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
        ProviderKind.IMAGE: provider,
    }
    scheduler = GpuScheduler(
        config=SchedulerConfig(
            job_timeout_seconds=5,
            release_timeout_seconds=0.2,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers=providers,
        monitor=NvidiaGpuMonitor(lambda: unavailable_gpu),
    )
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers=providers,
        scheduler=scheduler,
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/assets",
            json={
                "workflow_id": "flux2-klein-t2i",
                "prompt": DIAGNOSTIC_PROMPT,
                "width": 768,
                "height": 768,
                "seed": 42,
                "steps": 4,
            },
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        body = _wait_job(client, job_id)
        assert body["status"] == "succeeded"
        download = client.get(f"/v1/jobs/{job_id}/download")
        assert download.status_code == 200
        assert download.content == server.image_bytes
        assert png_dimensions(download.content) == (768, 768)
    prompt = server.prompts[0]["prompt"]
    assert prompt["76"]["inputs"]["value"] == DIAGNOSTIC_PROMPT
    assert prompt["75:68"]["inputs"]["value"] == 768
    assert prompt["75:69"]["inputs"]["value"] == 768
    assert prompt["75:73"]["inputs"]["noise_seed"] == 42
    assert prompt["75:62"]["inputs"]["steps"] == 4
    assert prompt["75:63"]["inputs"]["cfg"] == 1.0
    assert prompt["75:70"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert prompt["75:71"]["inputs"]["clip_name"] == "qwen_3_4b_fp4_flux2.safetensors"
    assert prompt["75:74"]["inputs"]["text"] == ["76", 0]
    assert prompt["9"]["class_type"] == "SaveImage"
    object_info_at = next(index for index, call in enumerate(server.calls) if call[0] == "GET" and str(call[1]).startswith("/object_info/"))
    prompt_at = next(index for index, call in enumerate(server.calls) if call == ("POST", "/prompt"))
    assert object_info_at < prompt_at


async def test_llm_job_is_what_releases_comfyui(settings: Settings, tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    _write_registry(workflows)
    server = ComfyServer()
    server.release_on_free = True
    server.complete_after = 1
    provider = _provider(settings, workflows, server)
    llm = LlmStub()
    scheduler = _scheduler(provider, llm)
    await scheduler.start()
    try:
        finished = await _image_job(scheduler)
        assert finished.status is JobStatus.SUCCEEDED
        assert server.frees == []
        follow_up = await scheduler.submit(ProviderKind.LLM)
        await asyncio.wait_for(llm.started.wait(), timeout=2)
        assert server.frees == [FREE_BODY]
        llm.release.set()
        released = await asyncio.wait_for(follow_up.done, timeout=2)
        assert released.status is JobStatus.SUCCEEDED
    finally:
        await scheduler.stop()


def _replaced(provider: ComfyUIProvider, **changes: object) -> ComfyConfig:
    from dataclasses import replace

    return replace(provider._config, **changes)  # type: ignore[arg-type]
