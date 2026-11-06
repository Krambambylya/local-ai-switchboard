"""Shared fixtures. LAS_ environment variables are cleared so tests stay isolated."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.unavailable import unavailable_providers


@pytest.fixture(autouse=True)
def clear_switchboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("LAS_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        install_root=tmp_path,
        storage_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def unavailable_gpu() -> GpuSnapshot:
    return GpuSnapshot(
        available=False,
        devices=(),
        compute_apps=(),
        error="nvidia-smi was not found on PATH",
    )


@pytest.fixture
def client(settings: Settings, unavailable_gpu: GpuSnapshot) -> Iterator[TestClient]:
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers=unavailable_providers(),
    )
    with TestClient(app) as test_client:
        yield test_client
