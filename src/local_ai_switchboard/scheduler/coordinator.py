"""Single-worker FIFO coordinator for LLM and image GPU jobs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from local_ai_switchboard.gpu.monitor import GpuMonitor, memory_is_sufficient
from local_ai_switchboard.providers.base import (
    ProviderError,
    ProviderKind,
    ProviderProtocol,
    ResourceNotReleased,
)
from local_ai_switchboard.scheduler.model import (
    JobResult,
    JobStatus,
    QueueFull,
    SchedulerConfig,
    SchedulerState,
    SchedulerStatus,
    SchedulerUnavailable,
    active_state,
    queued_state,
    starting_state,
    switch_state,
)

logger = logging.getLogger("local_ai_switchboard.scheduler")

_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
_HISTORY_LIMIT = 256


class _ReleaseAborted(Exception):
    """The memory wait stopped because the job was cancelled or the scheduler is stopping."""


class Clock:
    """Time source. Tests supply a clock that advances without waiting on the wall clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class Lease:
    """One grant of the GPU. A newer generation ignores a late release."""

    generation: int
    job_id: str
    provider: ProviderKind


class ScheduledJob:
    """One admission. The provider receives this object as its job context."""

    def __init__(
        self,
        job_id: str,
        kind: ProviderKind,
        enqueued_at: float,
        payload: object | None = None,
    ) -> None:
        self.id = job_id
        self.kind = kind
        self.payload = payload
        self.status = JobStatus.QUEUED
        self.enqueued_at = enqueued_at
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.diagnostics: tuple[tuple[str, str], ...] = ()
        self.cancel_requested = False
        self.persist_as_interrupted = False
        self.claimed = False
        self.task: asyncio.Task[None] | None = None
        self.done: asyncio.Future[JobResult] = asyncio.get_running_loop().create_future()

    def result(self) -> JobResult:
        wait = 0.0
        if self.started_at is not None:
            wait = max(0.0, self.started_at - self.enqueued_at)
        elif self.finished_at is not None:
            wait = max(0.0, self.finished_at - self.enqueued_at)
        return JobResult(
            id=self.id,
            kind=self.kind,
            status=self.status,
            error=self.error,
            wait_seconds=wait,
            diagnostics=self.diagnostics,
        )


class GpuScheduler:
    """Fair FIFO GPU lease. One running instance is allowed in the process."""

    _running: GpuScheduler | None = None

    def __init__(
        self,
        *,
        config: SchedulerConfig,
        providers: Mapping[ProviderKind, ProviderProtocol],
        monitor: GpuMonitor,
        clock: Clock | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        missing = [kind for kind in ProviderKind if kind not in providers]
        if missing:
            names = ", ".join(kind.value for kind in missing)
            raise ValueError(f"scheduler requires a provider for: {names}")
        self._config = config
        self._providers = dict(providers)
        self._monitor = monitor
        self._clock = clock or Clock()
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._queue: deque[ScheduledJob] = deque()
        self._jobs: dict[str, ScheduledJob] = {}
        self._state = SchedulerState.IDLE
        self._history: list[SchedulerState] = [SchedulerState.IDLE]
        self._resident: ProviderKind | None = None
        self._current_job_id: str | None = None
        self._lease: Lease | None = None
        self._generation = 0
        self._queue_length = 0
        self._last_error: str | None = None
        self._last_diagnostics: tuple[tuple[str, str], ...] = ()
        self._started = False
        self._stopping = False
        self._accepting = False
        self._recovery_ok = False
        self._recover_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._claimed: ScheduledJob | None = None
        self._listener: Callable[[ScheduledJob], Awaitable[None]] | None = None

    @property
    def recovery_ok(self) -> bool:
        """True after startup recovery confirms no managed provider still holds the GPU."""

        return self._recovery_ok

    async def start(self) -> None:
        """Recover residual GPU use, then start the single worker.

        Jobs are refused until recovery finishes. A second live scheduler in
        this process is refused.
        """

        async with self._lock:
            if self._started:
                return
            running = GpuScheduler._running
            if running is not None and running is not self and running._started:
                raise RuntimeError("A GPU scheduler is already running in this process")
            self._stopping = False
            self._started = True
            self._accepting = False
            GpuScheduler._running = self
        ok = False
        try:
            ok = await self._release_occupied_providers()
        except Exception:
            logger.exception("startup GPU recovery failed")
            ok = False
        async with self._lock:
            self._recovery_ok = ok
            self._accepting = ok
            if not ok:
                self._last_error = self._last_error or "startup GPU recovery did not finish"
                self._assign_state(SchedulerState.ERROR)
            else:
                # A clean start is already IDLE, and _assign_state does not log an
                # unchanged state. The log must still record this process.
                logger.info("scheduler state %s", self._state.value)
            self._worker = asyncio.create_task(self._worker_loop(), name="gpu-scheduler")

    async def stop(self) -> None:
        """Cancel queued work, drop the active lease, and stop the worker."""

        async with self._lock:
            if not self._started:
                return
            self._stopping = True
            self._assign_state(SchedulerState.STOPPING)
            waiting = list(self._queue)
            self._queue.clear()
            self._queue_length = 0
            claimed = self._claimed
            for job in waiting:
                job.cancel_requested = True
                if job.kind is ProviderKind.IMAGE:
                    job.persist_as_interrupted = True
                self._complete_locked(job, JobStatus.CANCELLED, "scheduler is stopping")
            if claimed is not None and claimed.status not in _TERMINAL:
                claimed.cancel_requested = True
                if claimed.kind is ProviderKind.IMAGE:
                    claimed.persist_as_interrupted = True
            self._wake.set()
        for job in waiting:
            await self._emit(job)
        if claimed is not None and claimed.status not in _TERMINAL:
            await self._safe_cancel(self._providers[claimed.kind], claimed)
        worker = self._worker
        if worker is not None:
            await worker
        resident = self._resident
        if resident is not None:
            await self._release_on_shutdown(self._providers[resident])
        async with self._lock:
            self._resident = None
            self._started = False
            self._stopping = False
            self._worker = None
            if self._state is not SchedulerState.ERROR:
                self._assign_state(SchedulerState.IDLE)
            if GpuScheduler._running is self:
                GpuScheduler._running = None

    async def submit(self, kind: ProviderKind, *, payload: object | None = None) -> ScheduledJob:
        """Enqueue a job and return it. Await job.done for the terminal result.

        payload is handed to the provider with the job. Chat routes put the
        exchange object here. The scheduler does not inspect it.
        """

        async with self._lock:
            if not self._started or self._stopping:
                raise SchedulerUnavailable("scheduler is not running")
            errored = self._state is SchedulerState.ERROR
        if errored:
            recovered = await self.recover()
            if not recovered:
                async with self._lock:
                    detail = self._last_error or "scheduler is in ERROR"
                raise SchedulerUnavailable(detail)
        async with self._lock:
            if not self._started or self._stopping or not self._accepting:
                if self._state is SchedulerState.ERROR:
                    detail = self._last_error or "scheduler is in ERROR"
                    raise SchedulerUnavailable(detail)
                raise SchedulerUnavailable("scheduler is not running")
            if self._state is SchedulerState.ERROR:
                detail = self._last_error or "scheduler is in ERROR"
                raise SchedulerUnavailable(detail)
            if self._queue_length >= self._config.max_queue_depth:
                raise QueueFull(f"GPU queue is full ({self._config.max_queue_depth})")
            job = ScheduledJob(self._id_factory(), kind, self._clock.monotonic(), payload)
            self._jobs[job.id] = job
            self._queue.append(job)
            self._queue_length = len(self._queue)
            if self._state is SchedulerState.IDLE and self._current_job_id is None:
                self._assign_state(queued_state(kind))
            self._wake.set()
            logger.info("queued %s job %s depth=%s", kind.value, job.id, self._queue_length)
        await self._emit(job)
        return job

    async def get_job(self, job_id: str) -> ScheduledJob | None:
        """Return one admitted job. The caller must not mutate scheduler state."""

        async with self._lock:
            return self._jobs.get(job_id)

    def set_listener(self, listener: Callable[[ScheduledJob], Awaitable[None]] | None) -> None:
        """Record image job changes. The listener runs outside the scheduler lock."""

        self._listener = listener

    async def cancel(self, job_id: str) -> None:
        """Cancel a queued job immediately, or request cancellation of the active job."""

        immediate: ScheduledJob | None = None
        kind: ProviderKind | None = None
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in _TERMINAL:
                return
            job.cancel_requested = True
            if job.status is JobStatus.QUEUED and not job.claimed:
                self._remove_queued(job)
                immediate = job
            else:
                kind = job.kind
        if immediate is not None:
            await self._finish(immediate, JobStatus.CANCELLED, None)
            return
        if kind is not None:
            await self._providers[kind].cancel(job)

    async def recover(self) -> bool:
        """Leave ERROR after provider residency and nvidia-smi both show the GPU is free.

        An HTTP 200 from unload is not enough. Returns False when the wait
        expires or the scheduler is stopping. One recovery runs at a time.
        """

        async with self._recover_lock:
            async with self._lock:
                if self._stopping:
                    return False
                if self._state is not SchedulerState.ERROR:
                    return self._recovery_ok
                resident = self._resident
            try:
                if resident is not None:
                    await self._await_provider_release(self._providers[resident])
                else:
                    released = await self._release_occupied_providers()
                    if not released:
                        return False
            except ResourceNotReleased as exc:
                self._last_error = str(exc)
                self._last_diagnostics = _pairs(exc.diagnostics)
                return False
            except ProviderError as exc:
                self._last_error = str(exc)
                self._last_diagnostics = _pairs(exc.diagnostics)
                return False
            except _ReleaseAborted:
                return False
            async with self._lock:
                if self._stopping or self._state is not SchedulerState.ERROR:
                    return self._state is not SchedulerState.ERROR
                self._resident = None
                self._last_error = None
                self._last_diagnostics = ()
                self._recovery_ok = True
                self._accepting = True
                self._assign_state(SchedulerState.IDLE)
                self._wake.set()
            logger.info("scheduler recovered to IDLE")
            return True

    async def status(self) -> SchedulerStatus:
        async with self._lock:
            return self._status_locked()

    async def state_history(self) -> tuple[SchedulerState, ...]:
        async with self._lock:
            return tuple(self._history)

    async def _worker_loop(self) -> None:
        while True:
            job = await self._next_job()
            if job is None:
                return
            task = asyncio.create_task(self._execute(job), name=f"gpu-job-{job.id}")
            async with self._lock:
                self._active_task = task
                job.task = task
            try:
                await task
            finally:
                async with self._lock:
                    if self._active_task is task:
                        self._active_task = None

    async def _next_job(self) -> ScheduledJob | None:
        while True:
            async with self._lock:
                stopping = self._stopping
                blocked = self._state is SchedulerState.ERROR
                if self._queue and not blocked:
                    job = self._queue.popleft()
                    job.claimed = True
                    self._claimed = job
                    self._queue_length = len(self._queue)
                    return job
                if stopping:
                    return None
                waiting_on_error = blocked and bool(self._queue)
                if not waiting_on_error:
                    self._wake.clear()
            if waiting_on_error:
                recovered = await self.recover()
                if not recovered:
                    await self._clock.sleep(self._config.poll_interval_seconds)
                continue
            await self._wake.wait()

    async def _release_occupied_providers(self) -> bool:
        """Unload providers that still report occupancy, then require real headroom.

        A provider that is not running is absent and is left alone. An occupied
        provider is released and must then report itself clear while nvidia-smi
        shows enough free memory. Unknown occupancy fails the recovery.
        """

        occupied: list[ProviderProtocol] = []
        for provider in self._providers.values():
            state = await _occupancy(provider)
            if state == "unknown":
                self._last_error = f"{provider.kind.value} occupancy could not be inspected"
                return False
            if state == "occupied":
                occupied.append(provider)
        for provider in occupied:
            try:
                await self._await_provider_release(provider)
            except ResourceNotReleased as exc:
                self._last_error = str(exc)
                self._last_diagnostics = _pairs(exc.diagnostics)
                return False
            except ProviderError as exc:
                self._last_error = str(exc)
                self._last_diagnostics = _pairs(exc.diagnostics)
                return False
            except _ReleaseAborted:
                return False
        async with self._lock:
            self._resident = None
        return True

    async def _execute(self, job: ScheduledJob) -> None:
        provider = self._providers[job.kind]
        generation: int | None = None
        try:
            if job.cancel_requested:
                await self._finish(job, JobStatus.CANCELLED, None)
                return
            ready = await self._prepare_provider(job, provider)
            if not ready:
                if job.status not in _TERMINAL:
                    if job.cancel_requested or self._stopping:
                        await self._finish(job, JobStatus.CANCELLED, None)
                    else:
                        await self._finish(
                            job,
                            JobStatus.FAILED,
                            self._last_error,
                            self._last_diagnostics,
                        )
                return
            generation = await self._grant_lease(job)
            await self._emit(job)
            try:
                await asyncio.wait_for(provider.run(job), self._config.job_timeout_seconds)
            except asyncio.TimeoutError:
                await self._safe_cancel(provider, job)
                await self._finish(job, JobStatus.FAILED, "job timed out")
                return
            if job.cancel_requested:
                await self._finish(job, JobStatus.CANCELLED, None)
                return
            await self._finish(job, JobStatus.SUCCEEDED, None)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup_cancelled(provider, job))
        except ProviderError as exc:
            await self._finish(job, JobStatus.FAILED, str(exc), _pairs(exc.diagnostics))
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            await self._finish(job, JobStatus.FAILED, str(exc))
        finally:
            if generation is not None:
                await self._release_lease(generation)
            async with self._lock:
                if self._claimed is job:
                    self._claimed = None

    async def _prepare_provider(self, job: ScheduledJob, provider: ProviderProtocol) -> bool:
        """Switch providers when required, then prepare the destination.

        Returns False when the job was cancelled or the scheduler entered ERROR.
        The destination provider is not prepared until the source has released
        enough GPU memory.
        """

        resident = self._resident
        if resident is not None and resident is not job.kind:
            await self._mark(switch_state(job.kind))
            source = self._providers[resident]
            if await source.is_busy():
                await self._enter_error(f"{resident.value} still has work running")
                return False
            try:
                await self._await_provider_release(source, job)
            except ResourceNotReleased as exc:
                await self._enter_error(str(exc), _pairs(exc.diagnostics))
                return False
            except _ReleaseAborted:
                return False
            async with self._lock:
                if self._resident is resident:
                    self._resident = None
        if job.cancel_requested:
            await self._finish(job, JobStatus.CANCELLED, None)
            return False
        await self._mark(queued_state(job.kind))
        if self._resident is not job.kind:
            await self._mark(starting_state(job.kind))
            try:
                await provider.prepare()
            except ProviderError as exc:
                await self._enter_error(str(exc), _pairs(exc.diagnostics))
                return False
            async with self._lock:
                self._resident = job.kind
        return True

    async def _await_provider_release(
        self,
        provider: ProviderProtocol,
        job: ScheduledJob | None = None,
    ) -> None:
        """Ask the provider to unload, then poll until residency and memory agree."""

        await provider.release_resources()
        deadline = self._clock.monotonic() + self._config.release_timeout_seconds
        last_detail = "release has not been confirmed"
        while True:
            if self._stopping or (job is not None and job.cancel_requested):
                raise _ReleaseAborted()
            released = await provider.resources_released()
            snapshot = await self._monitor.snapshot()
            enough, detail = memory_is_sufficient(snapshot, self._config.gpu_memory_threshold_mb)
            last_detail = detail
            if self._stopping or (job is not None and job.cancel_requested):
                raise _ReleaseAborted()
            if released and enough:
                logger.info("provider %s released GPU resources: %s", provider.kind.value, detail)
                return
            if self._clock.monotonic() >= deadline:
                diagnostics = {
                    "provider": provider.kind.value,
                    "provider_released": str(released).lower(),
                    "memory": detail,
                }
                raise ResourceNotReleased(
                    "GPU resources were not released before the timeout",
                    diagnostics=diagnostics,
                )
            logger.info(
                "waiting for %s release released=%s memory=%s",
                provider.kind.value,
                released,
                last_detail,
            )
            await self._clock.sleep(self._config.poll_interval_seconds)

    async def _grant_lease(self, job: ScheduledJob) -> int:
        async with self._lock:
            self._generation += 1
            self._lease = Lease(self._generation, job.id, job.kind)
            job.status = JobStatus.RUNNING
            job.started_at = self._clock.monotonic()
            self._current_job_id = job.id
            generation = self._generation
            self._assign_state(active_state(job.kind))
            return generation

    async def _release_lease(self, generation: int) -> None:
        async with self._lock:
            lease = self._lease
            if lease is None or lease.generation != generation:
                return
            if self._current_job_id == lease.job_id:
                self._current_job_id = None
            self._lease = None

    async def _finish(
        self,
        job: ScheduledJob,
        status: JobStatus,
        error: str | None,
        diagnostics: tuple[tuple[str, str], ...] = (),
    ) -> None:
        async with self._lock:
            self._complete_locked(job, status, error, diagnostics, resolve=False)
        try:
            await self._emit(job)
        except Exception:
            logger.exception("job persistence failed for %s", job.id)
            if job.status is JobStatus.SUCCEEDED:
                async with self._lock:
                    job.status = JobStatus.FAILED
                    job.error = "completion was not stored"
                try:
                    await self._emit(job)
                except Exception:
                    logger.exception("failed job persistence also failed for %s", job.id)
        async with self._lock:
            if not job.done.done():
                job.done.set_result(job.result())

    async def _emit(self, job: ScheduledJob) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            await listener(job)
        except Exception:
            logger.exception("job persistence failed for %s", job.id)
            if job.status is JobStatus.SUCCEEDED:
                raise

    async def _enter_error(
        self,
        message: str,
        diagnostics: tuple[tuple[str, str], ...] = (),
    ) -> None:
        async with self._lock:
            self._last_error = message
            self._last_diagnostics = diagnostics
            self._assign_state(SchedulerState.ERROR)
        logger.error("scheduler error: %s", message)
        if diagnostics:
            logger.error("scheduler diagnostics: %s", dict(diagnostics))

    async def _mark(self, state: SchedulerState) -> None:
        async with self._lock:
            self._assign_state(state)

    async def _cleanup_cancelled(self, provider: ProviderProtocol, job: ScheduledJob) -> None:
        await self._safe_cancel(provider, job)
        await self._finish(job, JobStatus.CANCELLED, None)

    async def _safe_cancel(self, provider: ProviderProtocol, job: ScheduledJob) -> None:
        try:
            await asyncio.shield(provider.cancel(job))
        except Exception:
            logger.exception("provider cancel failed for job %s", job.id)

    async def _release_on_shutdown(self, provider: ProviderProtocol) -> None:
        try:
            await provider.release_resources()
        except Exception:
            logger.exception("provider release failed during shutdown")

    def _complete_locked(
        self,
        job: ScheduledJob,
        status: JobStatus,
        error: str | None,
        diagnostics: tuple[tuple[str, str], ...] = (),
        *,
        resolve: bool = True,
    ) -> None:
        if job.status in _TERMINAL:
            return
        job.status = status
        job.error = error
        job.diagnostics = diagnostics
        job.finished_at = self._clock.monotonic()
        if self._current_job_id == job.id:
            self._current_job_id = None
        if self._state not in {SchedulerState.ERROR, SchedulerState.STOPPING}:
            self._assign_follow_up_state()
        if resolve and not job.done.done():
            job.done.set_result(job.result())
        logger.info("job %s %s", job.id, status.value)

    def _assign_follow_up_state(self) -> None:
        if self._queue:
            self._assign_state(queued_state(self._queue[0].kind))
            return
        self._assign_state(SchedulerState.IDLE)

    def _remove_queued(self, job: ScheduledJob) -> None:
        self._queue = deque(item for item in self._queue if item.id != job.id)
        self._queue_length = len(self._queue)

    def _assign_state(self, state: SchedulerState) -> None:
        if self._state is state:
            return
        self._state = state
        self._history.append(state)
        if len(self._history) > _HISTORY_LIMIT:
            self._history = self._history[-_HISTORY_LIMIT :]
        logger.info("scheduler state %s", state.value)

    def _status_locked(self) -> SchedulerStatus:
        return SchedulerStatus(
            state=self._state,
            active_provider=self._resident,
            current_job_id=self._current_job_id,
            queue_length=self._queue_length,
            lease_generation=self._generation,
            last_error=self._last_error,
        )


def _pairs(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


async def _occupancy(provider: ProviderProtocol) -> str:
    """Return clear, occupied, absent, or unknown without treating HTTP 200 as free VRAM."""

    inspector = getattr(provider, "occupancy", None)
    if inspector is None:
        try:
            released = await provider.resources_released()
        except Exception:
            return "unknown"
        return "clear" if released else "occupied"
    try:
        state = await inspector()
    except Exception:
        return "unknown"
    if state not in {"clear", "occupied", "absent", "unknown"}:
        return "unknown"
    return state
