"""Preflight reports configuration problems without contacting providers."""

from __future__ import annotations

import socket
import stat
import sys
from pathlib import Path

import pytest

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import ComputeProcess, GpuDevice, GpuSnapshot
from local_ai_switchboard.main import main
from local_ai_switchboard.services.preflight import run_preflight

TOKEN = "test-token-value-0123456789abcdef"


def _unavailable() -> GpuSnapshot:
    return GpuSnapshot(False, (), (), "nvidia-smi was not found on PATH")


def test_ready_when_token_exists_and_gpu_is_absent(settings: Settings) -> None:
    report = run_preflight(settings, gpu_probe=_unavailable)
    assert report.ready is True
    by_name = {check.name: check for check in report.checks}
    assert by_name["ollama_url"].status == "pass"
    assert by_name["comfyui_url"].status == "pass"
    assert by_name["api_bind"].detail == "bind 0.0.0.0:8787"
    assert by_name["token_file"].status == "pass"
    assert by_name["gpu"].status == "warn"
    assert by_name["storage_dir"].status == "warn"
    assert TOKEN not in report.to_json()


def test_missing_token_fails_readiness(tmp_path: Path) -> None:
    settings = Settings(install_root=tmp_path)
    report = run_preflight(settings, gpu_probe=_unavailable)
    assert report.ready is False
    token_check = next(check for check in report.checks if check.name == "token_file")
    assert token_check.status == "fail"
    assert token_check.detail.startswith("token file is missing:")


def test_low_free_memory_warns_without_blocking_readiness(settings: Settings) -> None:
    snapshot = GpuSnapshot(
        available=True,
        devices=(GpuDevice(0, "NVIDIA GeForce RTX 4060", 8188, 7600, 588, 91),),
        compute_apps=(ComputeProcess(10, r"C:\Program Files\Ollama\ollama.exe", 7000),),
        error=None,
    )
    report = run_preflight(settings, gpu_probe=lambda: snapshot)
    assert report.ready is True
    memory = next(check for check in report.checks if check.name == "gpu_memory")
    assert memory.status == "warn"
    assert "588" in memory.detail
    assert "below threshold 1024 MB" in memory.detail
    gpu = next(check for check in report.checks if check.name == "gpu")
    assert "RTX 4060" in gpu.detail


def test_storage_path_that_is_a_file_fails(settings: Settings, tmp_path: Path) -> None:
    storage = tmp_path / "not-a-directory"
    storage.write_text("blocked", encoding="utf-8")
    configured = Settings(
        install_root=tmp_path,
        storage_dir=storage,
        token_file=settings.resolved_token_file(),
        log_dir=tmp_path / "logs",
    )
    report = run_preflight(configured, gpu_probe=_unavailable)
    assert report.ready is False
    storage_check = next(check for check in report.checks if check.name == "storage_dir")
    assert storage_check.status == "fail"


def test_preflight_does_not_open_sockets(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("preflight opened a socket")

    monkeypatch.setattr(socket, "create_connection", fail)
    report = run_preflight(settings, gpu_probe=_unavailable)
    assert report.ready is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits are not Windows ACLs")
def test_group_readable_token_file_warns(tmp_path: Path) -> None:
    token_file = tmp_path / "secrets" / "token.txt"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    settings = Settings(install_root=tmp_path, token_file=token_file, log_dir=tmp_path / "logs")
    report = run_preflight(settings, gpu_probe=_unavailable)
    token_check = next(check for check in report.checks if check.name == "token_file")
    assert token_check.status == "warn"
    assert report.ready is True
    assert stat.S_IRGRP & token_file.stat().st_mode


def test_cli_preflight_hides_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / "secrets" / "token.txt"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("LAS_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("LAS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        "local_ai_switchboard.services.preflight.query_nvidia_smi",
        _unavailable,
    )
    assert main(["preflight"]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert '"ready": true' in captured.out


def test_cli_preflight_fails_when_token_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LAS_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("LAS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        "local_ai_switchboard.services.preflight.query_nvidia_smi",
        _unavailable,
    )
    assert main(["preflight"]) == 1
    captured = capsys.readouterr()
    assert '"ready": false' in captured.out
