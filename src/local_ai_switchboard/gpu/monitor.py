"""Async GPU monitor. Scheduling decisions stay in the scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot, assess_free_memory, query_nvidia_smi


class GpuMonitor(Protocol):
    """Supplies GPU snapshots. Tests inject a fake. Production reads nvidia-smi."""

    async def snapshot(self) -> GpuSnapshot:
        """Return the latest memory and compute-app snapshot."""


class NvidiaGpuMonitor:
    """Adapt the synchronous nvidia-smi probe to the scheduler's async monitor."""

    def __init__(self, probe: Callable[[], GpuSnapshot] | None = None) -> None:
        self._probe = probe or query_nvidia_smi

    async def snapshot(self) -> GpuSnapshot:
        return await asyncio.to_thread(self._probe)


def memory_is_sufficient(snapshot: GpuSnapshot, threshold_mb: int) -> tuple[bool, str]:
    """Return whether free memory meets the threshold.

    An unavailable monitor or an unknown reading is not sufficient. Global free
    memory is only one input; the scheduler also asks the provider whether its
    own model is still resident.
    """

    status, detail = assess_free_memory(snapshot, threshold_mb)
    return status == "pass", detail
