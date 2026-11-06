"""Build the fixed ComfyUI portable command. A bare --listen is rejected."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Protocol

LISTEN_HOST = "127.0.0.1"


class ProcessHandle(Protocol):
    """A ComfyUI process this gateway started."""

    def poll(self) -> int | None:
        """Return the exit code, or None while the process is running."""

    def terminate(self) -> None:
        """Ask the process to exit. Do not force-kill it."""

    def kill(self) -> None:
        """Stop this process and the children it spawned. Do not touch other PIDs."""

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for exit. Return None when the timeout expires."""


class Launcher(Protocol):
    """Starts ComfyUI from an argument list. shell must stay off."""

    def start(self, argv: list[str], cwd: Path) -> ProcessHandle:
        """Start the process and return a handle."""


def portable_python(portable: Path) -> Path:
    """Return the Windows portable embedded interpreter path."""

    return portable / "python_embeded" / "python.exe"


def portable_main(portable: Path) -> Path:
    """Return the portable ComfyUI entry script."""

    return portable / "ComfyUI" / "main.py"


def build_argv(portable: Path, port: int, reserve_vram_mb: int | None) -> list[str]:
    """Return the ComfyUI command. --listen is always followed by 127.0.0.1."""

    if not 1 <= port <= 65535:
        raise RuntimeError("ComfyUI port is invalid")
    argv = [
        str(portable_python(portable)),
        "-s",
        str(portable_main(portable)),
        "--listen",
        LISTEN_HOST,
        "--port",
        str(port),
        "--disable-auto-launch",
        "--windows-standalone-build",
    ]
    if reserve_vram_mb is not None:
        argv.extend(["--reserve-vram", str(int(reserve_vram_mb))])
    assert_explicit_listen(argv)
    return argv


def assert_explicit_listen(argv: list[str]) -> None:
    """Raise when --listen is missing, repeated, or has no loopback value."""

    indexes = [index for index, part in enumerate(argv) if part == "--listen"]
    if len(indexes) != 1:
        raise RuntimeError("ComfyUI launch must contain one --listen 127.0.0.1")
    index = indexes[0]
    if index + 1 >= len(argv) or argv[index + 1] != LISTEN_HOST:
        raise RuntimeError("ComfyUI --listen must be 127.0.0.1")


class SpawnedProcess:
    """Adapter around a Popen handle."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process.poll() is None:
            terminate_process_tree(self._process.pid)

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None


class SubprocessLauncher:
    """Start ComfyUI with subprocess and shell disabled."""

    def start(self, argv: list[str], cwd: Path) -> SpawnedProcess:
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            raise TypeError("ComfyUI argv must be a list of strings")
        assert_explicit_listen(argv)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return SpawnedProcess(process)


def terminate_process_tree(pid: int) -> None:
    """Stop one recorded process and its children. Other PIDs are not signaled."""

    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return
