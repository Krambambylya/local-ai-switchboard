"""Provider contract used by the GPU scheduler.

Implementations must not be called except by the scheduler. This module does not
open network connections.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class ProviderKind(StrEnum):
    """The two GPU workloads that share one device."""

    LLM = "llm"
    IMAGE = "image"


class ProviderError(Exception):
    """A provider failed. diagnostics are safe to log."""

    def __init__(self, message: str, *, diagnostics: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class ResourceNotReleased(ProviderError):
    """The previous provider still holds GPU resources after the wait expired."""


class JobContext(Protocol):
    """The slice of a queued job a provider is allowed to see."""

    id: str
    kind: ProviderKind

    @property
    def cancel_requested(self) -> bool:
        """True after the caller has asked the scheduler to cancel the job."""


class ProviderProtocol(Protocol):
    """Async GPU provider. run() returns only when the work has actually finished."""

    kind: ProviderKind

    async def prepare(self) -> None:
        """Load or wake this provider after the memory gate has passed."""

    async def run(self, job: JobContext) -> None:
        """Hold the caller until the stream ends or the image job completes.

        Raise ProviderError on failure. Let asyncio.CancelledError propagate.
        """

    async def cancel(self, job: JobContext) -> None:
        """Stop an in-flight job. This must be safe to call more than once.

        Must not call back into the scheduler. The scheduler may be waiting on this.
        """

    async def release_resources(self) -> None:
        """Request model unload. Returning does not prove memory is free."""

    async def resources_released(self) -> bool:
        """True when this provider no longer has a resident model or workflow."""

    async def is_busy(self) -> bool:
        """True when a stream or workflow is still running."""
