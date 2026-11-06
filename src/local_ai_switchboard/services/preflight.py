"""Local configuration and GPU checks. No provider HTTP calls are made."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from local_ai_switchboard.config.settings import Settings, assert_loopback_url
from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot, assess_free_memory, query_nvidia_smi
from local_ai_switchboard.services.redaction import redact

CheckStatus = Literal["pass", "fail", "warn"]
GpuProbe = Callable[[], GpuSnapshot]


@dataclass(frozen=True)
class Check:
    """One preflight result. detail must not contain secrets."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    """Collected preflight checks."""

    checks: tuple[Check, ...]

    @property
    def ready(self) -> bool:
        """True when no check failed. Warnings do not block readiness."""

        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def run_preflight(settings: Settings, gpu_probe: GpuProbe | None = None) -> PreflightReport:
    """Check settings, storage, and nvidia-smi diagnostics."""

    probe = gpu_probe or query_nvidia_smi
    snapshot = probe()
    checks = (
        _url_check("ollama_url", settings),
        _url_check("comfyui_url", settings),
        Check(
            "api_bind",
            "pass",
            f"bind {settings.api_host}:{settings.api_port}",
        ),
        _storage_check(settings),
        _gpu_check(snapshot),
        _memory_check(snapshot, settings.gpu_memory_threshold_mb),
    )
    return PreflightReport(tuple(_redacted(check) for check in checks))


def _url_check(name: str, settings: Settings) -> Check:
    url = settings.ollama_url if name == "ollama_url" else settings.comfyui_url
    try:
        assert_loopback_url(url)
    except ValueError as exc:
        return Check(name, "fail", str(exc))
    return Check(name, "pass", "host is loopback")


def _storage_check(settings: Settings) -> Check:
    path = settings.resolved_storage_dir()
    if path.exists() and not path.is_dir():
        return Check("storage_dir", "fail", f"storage path is not a directory: {path}")
    if not path.exists():
        return Check("storage_dir", "warn", f"storage directory does not exist yet: {path}")
    return Check("storage_dir", "pass", f"storage directory exists: {path}")


def _gpu_check(snapshot: GpuSnapshot) -> Check:
    if not snapshot.available:
        detail = snapshot.error or "nvidia-smi is unavailable"
        return Check("gpu", "warn", detail)
    names = ", ".join(
        f"gpu {device.index} {device.name} free {device.memory_free_mb} MB"
        for device in snapshot.devices
    )
    detail = names or "nvidia-smi returned no GPU devices"
    if snapshot.error:
        detail = f"{detail}. {snapshot.error}"
    return Check("gpu", "pass", detail)


def _memory_check(snapshot: GpuSnapshot, threshold_mb: int) -> Check:
    status, detail = assess_free_memory(snapshot, threshold_mb)
    if status == "unknown":
        return Check("gpu_memory", "warn", detail)
    if status == "warn":
        return Check("gpu_memory", "warn", detail)
    return Check("gpu_memory", "pass", detail)


def _redacted(check: Check) -> Check:
    return Check(check.name, check.status, redact(check.detail))
