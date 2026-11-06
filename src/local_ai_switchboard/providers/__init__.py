"""Provider interfaces. Ollama and ComfyUI speak HTTP only through the scheduler."""

from local_ai_switchboard.providers.base import (
    JobContext,
    ProviderError,
    ProviderKind,
    ProviderProtocol,
    ResourceNotReleased,
)

__all__ = [
    "JobContext",
    "ProviderError",
    "ProviderKind",
    "ProviderProtocol",
    "ResourceNotReleased",
]
