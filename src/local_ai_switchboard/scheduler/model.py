"""Scheduler states, results, and configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.providers.base import ProviderKind


class SchedulerState(StrEnum):
    """Global phase of the single GPU coordinator."""

    IDLE = "IDLE"
    LLM_QUEUED = "LLM_QUEUED"
    LLM_STARTING = "LLM_STARTING"
    LLM_ACTIVE = "LLM_ACTIVE"
    SWITCH_TO_IMAGE = "SWITCH_TO_IMAGE"
    IMAGE_QUEUED = "IMAGE_QUEUED"
    IMAGE_STARTING = "IMAGE_STARTING"
    IMAGE_ACTIVE = "IMAGE_ACTIVE"
    SWITCH_TO_LLM = "SWITCH_TO_LLM"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


class JobStatus(StrEnum):
    """Status of one queued GPU job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class SchedulerError(Exception):
    """Base error for admission failures."""


class QueueFull(SchedulerError):
    """The waiting queue is at its configured limit."""


class SchedulerUnavailable(SchedulerError):
    """The scheduler is stopping or waiting for a failed release to be recovered."""


@dataclass(frozen=True)
class SchedulerConfig:
    """Queue, timeout, and memory-gate settings for one scheduler."""

    max_queue_depth: int = 8
    job_timeout_seconds: float = 300.0
    release_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 0.5
    gpu_memory_threshold_mb: int = 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerConfig:
        return cls(
            max_queue_depth=settings.max_queue_depth,
            job_timeout_seconds=settings.job_timeout_seconds,
            release_timeout_seconds=settings.release_timeout_seconds,
            poll_interval_seconds=settings.memory_poll_interval_seconds,
            gpu_memory_threshold_mb=settings.gpu_memory_threshold_mb,
        )


@dataclass(frozen=True)
class SchedulerStatus:
    """Point-in-time view of the coordinator."""

    state: SchedulerState
    active_provider: ProviderKind | None
    current_job_id: str | None
    queue_length: int
    lease_generation: int
    last_error: str | None


@dataclass(frozen=True)
class JobResult:
    """Terminal outcome of one GPU job."""

    id: str
    kind: ProviderKind
    status: JobStatus
    error: str | None
    wait_seconds: float
    diagnostics: tuple[tuple[str, str], ...] = ()


_QUEUED_FOR = {
    ProviderKind.LLM: SchedulerState.LLM_QUEUED,
    ProviderKind.IMAGE: SchedulerState.IMAGE_QUEUED,
}
_STARTING_FOR = {
    ProviderKind.LLM: SchedulerState.LLM_STARTING,
    ProviderKind.IMAGE: SchedulerState.IMAGE_STARTING,
}
_ACTIVE_FOR = {
    ProviderKind.LLM: SchedulerState.LLM_ACTIVE,
    ProviderKind.IMAGE: SchedulerState.IMAGE_ACTIVE,
}
_SWITCH_TO = {
    ProviderKind.LLM: SchedulerState.SWITCH_TO_LLM,
    ProviderKind.IMAGE: SchedulerState.SWITCH_TO_IMAGE,
}


def queued_state(kind: ProviderKind) -> SchedulerState:
    return _QUEUED_FOR[kind]


def starting_state(kind: ProviderKind) -> SchedulerState:
    return _STARTING_FOR[kind]


def active_state(kind: ProviderKind) -> SchedulerState:
    return _ACTIVE_FOR[kind]


def switch_state(destination: ProviderKind) -> SchedulerState:
    return _SWITCH_TO[destination]
