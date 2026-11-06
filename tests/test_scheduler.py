"""Deterministic scheduler tests. Providers and the GPU monitor are fakes."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_ai_switchboard.gpu.monitor import memory_is_sufficient
from local_ai_switchboard.gpu.nvidia_smi import GpuDevice, GpuSnapshot
from local_ai_switchboard.providers.base import JobContext, ProviderError, ProviderKind
from local_ai_switchboard.scheduler.coordinator import Clock, GpuScheduler, ScheduledJob
from local_ai_switchboard.scheduler.model import (
    JobStatus,
    QueueFull,
    SchedulerConfig,
    SchedulerState,
    SchedulerUnavailable,
)
from local_ai_switchboard.scheduler.process import (
    SchedulerAlreadyRunning,
    acquire_process_lock,
    enforce_single_worker,
    release_process_lock,
)


class ManualClock(Clock):
    """Advances only when the scheduler polls, so timeouts do not wait on the wall clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class ScriptedMonitor:
    """GPU monitor whose free memory is set by the test."""

    def __init__(self, free_mb: int = 7000) -> None:
        self.free_mb = free_mb
        self.calls = 0
        self.observed = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def snapshot(self) -> GpuSnapshot:
        self.calls += 1
        self.observed.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.free_mb < 0:
            return GpuSnapshot(False, (), (), "nvidia-smi was not found on PATH")
        device = GpuDevice(
            index=0,
            name="NVIDIA GeForce RTX 4060",
            memory_total_mb=8188,
            memory_used_mb=8188 - self.free_mb,
            memory_free_mb=self.free_mb,
            utilization_gpu=0,
        )
        return GpuSnapshot(True, (device,), ())


class FakeProvider:
    """In-memory provider. run() finishes only when the test or cancel() releases it."""

    def __init__(self, kind: ProviderKind, order: list[str]) -> None:
        self.kind = kind
        self.order = order
        self.prepare_calls = 0
        self.run_calls = 0
        self.release_calls = 0
        self.cancel_calls = 0
        self.released = True
        self.release_succeeds = True
        self.busy = False
        self.fail_run: ProviderError | None = None
        self.fail_with: Exception | None = None
        self.started = asyncio.Event()
        self.current_release: asyncio.Event | None = None

    async def prepare(self) -> None:
        self.order.append(f"{self.kind.value}.prepare")
        self.prepare_calls += 1
        self.released = False

    async def run(self, job: JobContext) -> None:
        self.order.append(f"{self.kind.value}.run")
        self.run_calls += 1
        release = asyncio.Event()
        self.current_release = release
        self.busy = True
        self.started.set()
        try:
            if job.cancel_requested:
                return
            if self.fail_run is not None:
                raise self.fail_run
            if self.fail_with is not None:
                raise self.fail_with
            await release.wait()
        finally:
            self.busy = False

    async def cancel(self, job: JobContext) -> None:
        self.order.append(f"{self.kind.value}.cancel")
        self.cancel_calls += 1
        if self.current_release is not None:
            self.current_release.set()

    async def release_resources(self) -> None:
        self.order.append(f"{self.kind.value}.release")
        self.release_calls += 1
        self.released = self.release_succeeds

    async def resources_released(self) -> bool:
        return self.released

    async def is_busy(self) -> bool:
        return self.busy

    def allow_finish(self) -> None:
        if self.current_release is not None:
            self.current_release.set()


def _config() -> SchedulerConfig:
    return SchedulerConfig(
        max_queue_depth=4,
        job_timeout_seconds=30,
        release_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
        gpu_memory_threshold_mb=1024,
    )


async def _wait(job: ScheduledJob):
    return await asyncio.wait_for(job.done, timeout=2)


async def _until_started(provider: FakeProvider) -> None:
    await asyncio.wait_for(provider.started.wait(), timeout=2)


@pytest.fixture
async def world():
    order: list[str] = []
    llm = FakeProvider(ProviderKind.LLM, order)
    image = FakeProvider(ProviderKind.IMAGE, order)
    monitor = ScriptedMonitor()
    clock = ManualClock()
    scheduler = GpuScheduler(
        config=_config(),
        providers={ProviderKind.LLM: llm, ProviderKind.IMAGE: image},
        monitor=monitor,
        clock=clock,
    )
    await scheduler.start()
    try:
        yield scheduler, llm, image, monitor, order
    finally:
        llm.allow_finish()
        image.allow_finish()
        await scheduler.stop()


def test_insufficient_or_unknown_memory_is_not_headroom() -> None:
    device = GpuDevice(0, "NVIDIA GeForce RTX 4060", 8188, 7600, 588, 10)
    short = GpuSnapshot(True, (device,), ())
    enough, detail = memory_is_sufficient(short, 1024)
    assert enough is False
    assert "588" in detail
    unknown, unknown_detail = memory_is_sufficient(
        GpuSnapshot(False, (), (), "nvidia-smi was not found on PATH"),
        1024,
    )
    assert unknown is False
    assert "not found" in unknown_detail


async def test_llm_then_llm_does_not_reload(world) -> None:
    scheduler, llm, _image, _monitor, _order = world
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    llm.allow_finish()
    assert (await _wait(first)).status is JobStatus.SUCCEEDED

    llm.started.clear()
    second = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    assert llm.prepare_calls == 1
    assert llm.release_calls == 0
    assert llm.run_calls == 2
    history = await scheduler.state_history()
    assert SchedulerState.SWITCH_TO_IMAGE not in history
    assert SchedulerState.SWITCH_TO_LLM not in history
    llm.allow_finish()
    assert (await _wait(second)).status is JobStatus.SUCCEEDED
    status = await scheduler.status()
    assert status.active_provider is ProviderKind.LLM
    assert status.current_job_id is None
    assert status.queue_length == 0


async def test_llm_then_image_switches_only_after_release(world) -> None:
    scheduler, llm, image, monitor, order = world
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    second = await scheduler.submit(ProviderKind.IMAGE)
    llm.allow_finish()
    await _until_started(image)
    assert order.index("llm.release") < order.index("image.prepare")
    assert order.index("image.prepare") < order.index("image.run")
    assert monitor.calls >= 1
    history = await scheduler.state_history()
    assert history.index(SchedulerState.SWITCH_TO_IMAGE) < history.index(SchedulerState.IMAGE_ACTIVE)
    image.allow_finish()
    assert (await _wait(first)).status is JobStatus.SUCCEEDED
    assert (await _wait(second)).status is JobStatus.SUCCEEDED
    assert (await scheduler.status()).active_provider is ProviderKind.IMAGE


async def test_image_then_llm_switches_only_after_release(world) -> None:
    scheduler, llm, image, monitor, order = world
    first = await scheduler.submit(ProviderKind.IMAGE)
    await _until_started(image)
    image.allow_finish()
    assert (await _wait(first)).status is JobStatus.SUCCEEDED

    image.started.clear()
    second = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    assert order.index("image.release") < order.index("llm.prepare")
    assert monitor.calls >= 1
    history = await scheduler.state_history()
    assert history.index(SchedulerState.SWITCH_TO_LLM) < history.index(SchedulerState.LLM_ACTIVE)
    llm.allow_finish()
    assert (await _wait(second)).status is JobStatus.SUCCEEDED


async def test_simultaneous_submissions_run_one_at_a_time(world) -> None:
    scheduler, llm, image, _monitor, order = world
    first = await scheduler.submit(ProviderKind.LLM)
    second = await scheduler.submit(ProviderKind.IMAGE)
    await _until_started(llm)
    while_active = await scheduler.status()
    assert while_active.current_job_id == first.id
    assert while_active.active_provider is ProviderKind.LLM
    assert while_active.queue_length == 1
    assert image.run_calls == 0
    assert while_active.state is SchedulerState.LLM_ACTIVE

    llm.allow_finish()
    await _until_started(image)
    assert order == ["llm.prepare", "llm.run", "llm.release", "image.prepare", "image.run"]
    during_image = await scheduler.status()
    assert during_image.current_job_id == second.id
    assert during_image.queue_length == 0
    image.allow_finish()
    assert (await _wait(first)).status is JobStatus.SUCCEEDED
    assert (await _wait(second)).status is JobStatus.SUCCEEDED


async def test_cancellation_releases_the_lease(world) -> None:
    scheduler, llm, image, _monitor, _order = world
    active = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    waiting = await scheduler.submit(ProviderKind.IMAGE)
    await scheduler.cancel(waiting.id)
    assert (await _wait(waiting)).status is JobStatus.CANCELLED
    assert image.run_calls == 0

    await scheduler.cancel(active.id)
    assert (await _wait(active)).status is JobStatus.CANCELLED
    assert llm.cancel_calls == 1
    released = await scheduler.status()
    assert released.current_job_id is None
    assert released.state is SchedulerState.IDLE
    assert released.queue_length == 0

    llm.started.clear()
    follow_up = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    assert llm.run_calls == 2
    llm.allow_finish()
    assert (await _wait(follow_up)).status is JobStatus.SUCCEEDED


async def test_provider_error_keeps_the_scheduler_usable(world) -> None:
    scheduler, llm, image, _monitor, _order = world
    llm.fail_run = ProviderError("ollama stopped")
    failed = await scheduler.submit(ProviderKind.LLM)
    result = await _wait(failed)
    assert result.status is JobStatus.FAILED
    assert result.error == "ollama stopped"
    assert image.prepare_calls == 0
    status = await scheduler.status()
    assert status.state is SchedulerState.IDLE
    assert status.current_job_id is None

    llm.fail_run = None
    llm.started.clear()
    recovered = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    assert llm.release_calls == 0
    llm.allow_finish()
    assert (await _wait(recovered)).status is JobStatus.SUCCEEDED


async def test_timeout_while_releasing_gpu_memory_does_not_start_the_next_provider(world) -> None:
    scheduler, llm, image, monitor, _order = world
    monitor.free_mb = 588
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    second = await scheduler.submit(ProviderKind.IMAGE)
    llm.allow_finish()
    result = await _wait(second)
    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert "not released" in result.error
    diagnostics = dict(result.diagnostics)
    assert diagnostics["provider_released"] == "true"
    assert "588" in diagnostics["memory"]
    assert image.prepare_calls == 0
    assert image.run_calls == 0
    assert (await _wait(first)).status is JobStatus.SUCCEEDED
    status = await scheduler.status()
    assert status.state is SchedulerState.ERROR
    with pytest.raises(SchedulerUnavailable):
        await scheduler.submit(ProviderKind.LLM)

    monitor.free_mb = 7000
    await scheduler.recover()
    assert (await scheduler.status()).state is SchedulerState.IDLE
    llm.started.clear()
    resumed = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    llm.allow_finish()
    assert (await _wait(resumed)).status is JobStatus.SUCCEEDED


async def test_cancellation_during_memory_release_does_not_start_the_next_provider(world) -> None:
    scheduler, llm, image, monitor, _order = world
    monitor.free_mb = 588
    monitor.gate = asyncio.Event()
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    waiting = await scheduler.submit(ProviderKind.IMAGE)
    llm.allow_finish()
    await asyncio.wait_for(monitor.observed.wait(), timeout=2)
    await scheduler.cancel(waiting.id)
    monitor.gate.set()
    assert (await _wait(waiting)).status is JobStatus.CANCELLED
    assert image.prepare_calls == 0
    assert image.run_calls == 0
    assert (await scheduler.status()).state is not SchedulerState.ERROR
    assert (await _wait(first)).status is JobStatus.SUCCEEDED

    monitor.gate = None
    monitor.free_mb = 7000
    image.started.clear()
    follow_up = await scheduler.submit(ProviderKind.IMAGE)
    await _until_started(image)
    image.allow_finish()
    assert (await _wait(follow_up)).status is JobStatus.SUCCEEDED


async def test_unexpected_provider_exception_leaves_the_queue_usable(world) -> None:
    scheduler, llm, _image, _monitor, _order = world
    llm.fail_with = RuntimeError("provider crashed")
    failed = await scheduler.submit(ProviderKind.LLM)
    result = await _wait(failed)
    assert result.status is JobStatus.FAILED
    assert result.error == "provider crashed"
    assert (await scheduler.status()).state is SchedulerState.IDLE

    llm.fail_with = None
    llm.started.clear()
    follow_up = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    llm.allow_finish()
    assert (await _wait(follow_up)).status is JobStatus.SUCCEEDED


async def test_provider_that_stays_resident_blocks_the_next_provider(world) -> None:
    scheduler, llm, image, monitor, _order = world
    llm.release_succeeds = False
    monitor.free_mb = 7000
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    second = await scheduler.submit(ProviderKind.IMAGE)
    llm.allow_finish()
    result = await _wait(second)
    assert result.status is JobStatus.FAILED
    assert dict(result.diagnostics)["provider_released"] == "false"
    assert image.prepare_calls == 0
    assert image.run_calls == 0
    assert (await scheduler.status()).state is SchedulerState.ERROR
    llm.allow_finish()
    assert (await _wait(first)).status is JobStatus.SUCCEEDED


async def test_second_scheduler_in_one_process_is_rejected() -> None:
    order: list[str] = []
    first = GpuScheduler(
        config=_config(),
        providers={
            ProviderKind.LLM: FakeProvider(ProviderKind.LLM, order),
            ProviderKind.IMAGE: FakeProvider(ProviderKind.IMAGE, order),
        },
        monitor=ScriptedMonitor(),
        clock=ManualClock(),
    )
    second = GpuScheduler(
        config=_config(),
        providers={
            ProviderKind.LLM: FakeProvider(ProviderKind.LLM, order),
            ProviderKind.IMAGE: FakeProvider(ProviderKind.IMAGE, order),
        },
        monitor=ScriptedMonitor(),
        clock=ManualClock(),
    )
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await second.start()
    finally:
        await first.stop()


def test_multiple_worker_settings_are_rejected() -> None:
    with pytest.raises(SystemExit):
        enforce_single_worker({"WEB_CONCURRENCY": "4"})
    with pytest.raises(SystemExit):
        enforce_single_worker({"UVICORN_WORKERS": "2"})
    enforce_single_worker({"WEB_CONCURRENCY": "1", "LAS_WORKERS": "1"})


def test_queue_rejects_work_past_the_configured_depth() -> None:
    async def scenario() -> None:
        order: list[str] = []
        llm = FakeProvider(ProviderKind.LLM, order)
        scheduler = GpuScheduler(
            config=SchedulerConfig(
                max_queue_depth=1,
                job_timeout_seconds=30,
                release_timeout_seconds=0.05,
                poll_interval_seconds=0.01,
                gpu_memory_threshold_mb=1024,
            ),
            providers={
                ProviderKind.LLM: llm,
                ProviderKind.IMAGE: FakeProvider(ProviderKind.IMAGE, order),
            },
            monitor=ScriptedMonitor(),
            clock=ManualClock(),
        )
        await scheduler.start()
        try:
            first = await scheduler.submit(ProviderKind.LLM)
            await _until_started(llm)
            await scheduler.submit(ProviderKind.LLM)
            with pytest.raises(QueueFull):
                await scheduler.submit(ProviderKind.LLM)
            llm.allow_finish()
            await _wait(first)
        finally:
            llm.allow_finish()
            await scheduler.stop()

    asyncio.run(scenario())


def test_second_process_cannot_take_the_scheduler_lock(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.lock"
    code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from local_ai_switchboard.scheduler.process import acquire_process_lock\n"
            "handle = acquire_process_lock(Path(sys.argv[1]))\n"
            "print('locked', flush=True)\n"
            "sys.stdin.readline()\n"
            "handle.close()\n"
        )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    line = process.stdout.readline().strip()
    if line != "locked":
        stderr = process.stderr.read() if process.stderr is not None else ""
        process.kill()
        raise AssertionError(stderr or line)
    try:
        with pytest.raises(SchedulerAlreadyRunning):
            acquire_process_lock(path)
    finally:
        process.stdin.close()
        process.wait(timeout=5)


def test_process_lock_can_be_released(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.lock"
    handle = acquire_process_lock(path)
    release_process_lock(handle)
    handle = acquire_process_lock(path)
    release_process_lock(handle)


async def test_startup_unloads_a_resident_ollama_model() -> None:
    import httpx

    from local_ai_switchboard.providers.ollama import OllamaProvider

    class Library:
        def __init__(self) -> None:
            self.models: list[dict[str, str]] = [{"name": "qwen3.5:4b-ctx8192"}]
            self.unloads = 0

        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps" and request.method == "GET":
                return httpx.Response(200, json={"models": self.models})
            if request.url.path == "/api/chat" and request.method == "POST":
                body = json.loads(request.content.decode("utf-8"))
                assert body["keep_alive"] == 0
                self.unloads += 1
                self.models = []
                return httpx.Response(200, json={"done": True})
            return httpx.Response(404)

    library = Library()
    order: list[str] = []
    image = FakeProvider(ProviderKind.IMAGE, order)
    scheduler = GpuScheduler(
        config=_config(),
        providers={
            ProviderKind.LLM: OllamaProvider(
                "http://127.0.0.1:11434",
                transport=httpx.MockTransport(library.handler),
            ),
            ProviderKind.IMAGE: image,
        },
        monitor=ScriptedMonitor(7000),
        clock=ManualClock(),
    )
    await scheduler.start()
    try:
        assert library.unloads == 1
        assert library.models == []
        assert scheduler.recovery_ok is True
        assert image.release_calls == 0
        assert (await scheduler.status()).state is SchedulerState.IDLE
    finally:
        await scheduler.stop()


async def test_startup_recovery_timeout_refuses_new_work() -> None:
    order: list[str] = []
    llm = FakeProvider(ProviderKind.LLM, order)
    llm.released = False
    llm.release_succeeds = False
    scheduler = GpuScheduler(
        config=_config(),
        providers={
            ProviderKind.LLM: llm,
            ProviderKind.IMAGE: FakeProvider(ProviderKind.IMAGE, order),
        },
        monitor=ScriptedMonitor(588),
        clock=ManualClock(),
    )
    await scheduler.start()
    try:
        assert scheduler.recovery_ok is False
        assert (await scheduler.status()).state is SchedulerState.ERROR
        with pytest.raises(SchedulerUnavailable):
            await scheduler.submit(ProviderKind.LLM)
        assert llm.release_calls >= 1
    finally:
        await scheduler.stop()


async def test_third_job_waits_until_failed_release_recovers(world) -> None:
    scheduler, llm, image, monitor, _order = world
    monitor.free_mb = 588
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    second = await scheduler.submit(ProviderKind.IMAGE)
    third = await scheduler.submit(ProviderKind.LLM)
    llm.allow_finish()
    failed = await _wait(second)
    assert failed.status is JobStatus.FAILED
    assert image.prepare_calls == 0
    assert image.run_calls == 0
    assert llm.run_calls == 1
    assert third.status is JobStatus.QUEUED
    assert (await scheduler.status()).state is SchedulerState.ERROR
    llm.started.clear()
    monitor.free_mb = 7000
    await _until_started(llm)
    assert llm.run_calls == 2
    llm.allow_finish()
    assert (await _wait(third)).status is JobStatus.SUCCEEDED
    assert (await _wait(first)).status is JobStatus.SUCCEEDED
    assert (await scheduler.status()).state is SchedulerState.IDLE


async def test_submit_retries_recovery_after_an_empty_error(world) -> None:
    scheduler, llm, image, monitor, _order = world
    monitor.free_mb = 588
    first = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    second = await scheduler.submit(ProviderKind.IMAGE)
    llm.allow_finish()
    failed = await _wait(second)
    assert failed.status is JobStatus.FAILED
    assert image.prepare_calls == 0
    assert (await _wait(first)).status is JobStatus.SUCCEEDED
    assert (await scheduler.status()).state is SchedulerState.ERROR

    monitor.free_mb = 7000
    llm.started.clear()
    resumed = await scheduler.submit(ProviderKind.LLM)
    await _until_started(llm)
    llm.allow_finish()
    assert (await _wait(resumed)).status is JobStatus.SUCCEEDED
    assert (await scheduler.status()).state is SchedulerState.IDLE
