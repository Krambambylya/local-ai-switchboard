"""Providers that fail clearly until a real adapter is installed."""

from __future__ import annotations

from local_ai_switchboard.providers.base import JobContext, ProviderError, ProviderKind


class UnavailableProvider:
    """Placeholder used before the Ollama or ComfyUI adapter exists.

    Jobs fail instead of pretending that a model ran.
    """

    def __init__(self, kind: ProviderKind, name: str) -> None:
        self.kind = kind
        self._name = name

    async def prepare(self) -> None:
        raise ProviderError(f"{self._name} is not connected")

    async def run(self, job: JobContext) -> None:
        raise ProviderError(f"{self._name} is not connected")

    async def cancel(self, job: JobContext) -> None:
        return None

    async def release_resources(self) -> None:
        return None

    async def resources_released(self) -> bool:
        return True

    async def is_busy(self) -> bool:
        return False


def unavailable_providers() -> dict[ProviderKind, UnavailableProvider]:
    """Return both providers in the not-connected state."""

    return {
        ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
        ProviderKind.IMAGE: UnavailableProvider(ProviderKind.IMAGE, "ComfyUI"),
    }
