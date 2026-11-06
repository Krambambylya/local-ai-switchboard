"""nvidia-smi parsing and failure handling. No real GPU is required."""

from __future__ import annotations

import subprocess
import sys

import pytest

from local_ai_switchboard.gpu.nvidia_smi import (
    CommandResult,
    assess_free_memory,
    decode_output,
    default_runner,
    hidden_window_flag,
    parse_apps_csv,
    parse_gpu_csv,
    query_nvidia_smi,
)

RTX_4060 = "0, NVIDIA GeForce RTX 4060, 8188, 1536, 6652, 12\n"


def test_parse_rtx_4060_gpu_row() -> None:
    devices = parse_gpu_csv(RTX_4060)
    assert len(devices) == 1
    device = devices[0]
    assert device.name == "NVIDIA GeForce RTX 4060"
    assert device.memory_total_mb == 8188
    assert device.memory_used_mb == 1536
    assert device.memory_free_mb == 6652
    assert device.utilization_gpu == 12


def test_parse_windows_process_paths_with_spaces_and_unicode() -> None:
    stdout = "\n".join(
        [
            r"4242, C:\Program Files\Ollama\ollama.exe, 4096",
            r"4243, C:\Users\Алина\AppData\Local\Programs\Python\python.exe, 2048",
            "No running processes found",
        ]
    )
    apps = parse_apps_csv(stdout)
    assert apps[0].process_name == r"C:\Program Files\Ollama\ollama.exe"
    assert apps[0].used_memory_mb == 4096
    assert "Алина" in apps[1].process_name
    assert len(apps) == 2


def test_empty_and_notice_mean_no_compute_apps() -> None:
    assert parse_apps_csv("") == ()
    assert parse_apps_csv("No running processes found\n") == ()


def test_non_integer_memory_is_rejected() -> None:
    with pytest.raises(ValueError, match="memory.free"):
        parse_gpu_csv("0, NVIDIA GeForce RTX 4060, 8188, 1536, [N/A], 0\n")


def test_query_uses_runner_and_memory_threshold() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(args: tuple[str, ...] | list[str], timeout: float) -> CommandResult:
        calls.append(tuple(args))
        assert timeout == 5.0
        if "--query-gpu=" in args[1]:
            return CommandResult(0, RTX_4060, "")
        return CommandResult(0, r"77, C:\Program Files\Ollama\ollama.exe, 1400" + "\n", "")

    snapshot = query_nvidia_smi(runner)
    assert snapshot.available
    assert snapshot.devices[0].memory_free_mb == 6652
    assert snapshot.compute_apps[0].pid == 77
    assert len(calls) == 2
    status, detail = assess_free_memory(snapshot, 1024)
    assert status == "pass"
    assert "1024" in detail
    status, detail = assess_free_memory(snapshot, 7000)
    assert status == "warn"
    assert "6652" in detail
    assert "ollama.exe" not in detail


def test_missing_binary_and_timeout_are_unavailable() -> None:
    def missing(args: list[str], timeout: float) -> CommandResult:
        raise FileNotFoundError(args[0])

    snapshot = query_nvidia_smi(missing)
    assert snapshot.available is False
    assert snapshot.error == "nvidia-smi was not found on PATH"

    def timed_out(args: list[str], timeout: float) -> CommandResult:
        raise subprocess.TimeoutExpired(args, timeout)

    snapshot = query_nvidia_smi(timed_out)
    assert snapshot.error == "nvidia-smi timed out"


def test_unreadable_gpu_output_does_not_raise() -> None:
    def runner(args: list[str], timeout: float) -> CommandResult:
        return CommandResult(1, "not,csv\n", "NVIDIA-SMI has failed because it couldn't communicate")

    snapshot = query_nvidia_smi(runner)
    assert snapshot.available is False
    assert snapshot.error is not None
    assert "could not parse" in snapshot.error


def test_compute_app_parse_error_keeps_the_gpu_snapshot() -> None:
    def runner(args: list[str], timeout: float) -> CommandResult:
        if "--query-gpu=" in args[1]:
            return CommandResult(0, RTX_4060, "")
        return CommandResult(0, "not-a-process-row\n", "")

    snapshot = query_nvidia_smi(runner)
    assert snapshot.available is True
    assert snapshot.devices[0].name == "NVIDIA GeForce RTX 4060"
    assert snapshot.error is not None
    assert "compute apps" in snapshot.error


def test_default_runner_does_not_use_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs

        class Completed:
            returncode = 0
            stdout = "данные".encode()
            stderr = b""

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = default_runner(["nvidia-smi", "--version"], 1.5)
    assert result.stdout == "данные"
    assert result.returncode == 0
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert isinstance(captured["args"], list)
    if sys.platform == "win32":
        assert kwargs["creationflags"] == 0x08000000
    else:
        assert "creationflags" not in kwargs


def test_hidden_window_flag_is_windows_only() -> None:
    assert hidden_window_flag("win32") == 0x08000000
    assert hidden_window_flag("darwin") is None


def test_decode_output_prefers_utf8_and_replaces_invalid_bytes() -> None:
    assert decode_output("RTX 4060 данные".encode()) == "RTX 4060 данные"
    assert decode_output(b"\xff") != ""
