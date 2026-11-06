"""Read NVIDIA GPU memory and compute apps through nvidia-smi.

The parser is platform-independent. The command is invoked without a shell.
On Windows the process is started with CREATE_NO_WINDOW when that flag exists.
This module does not start or stop Ollama or ComfyUI.
"""

from __future__ import annotations

import csv
import locale
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from local_ai_switchboard.services.redaction import redact

GPU_QUERY = (
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu",
    "--format=csv,noheader,nounits",
)
APPS_QUERY = (
    "nvidia-smi",
    "--query-compute-apps=pid,process_name,used_gpu_memory",
    "--format=csv,noheader,nounits",
)

# subprocess.CREATE_NO_WINDOW exists only on Windows. The numeric value is part
# of the Windows API and is checked in tests without launching a process.
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class CommandResult:
    """Stdout and stderr captured from one nvidia-smi invocation."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GpuDevice:
    """One GPU row from nvidia-smi, with memory values in MiB."""

    index: int
    name: str
    memory_total_mb: int
    memory_used_mb: int
    memory_free_mb: int
    utilization_gpu: int


@dataclass(frozen=True)
class ComputeProcess:
    """One compute-app row from nvidia-smi."""

    pid: int
    process_name: str
    used_memory_mb: int


@dataclass(frozen=True)
class GpuSnapshot:
    """Diagnostic snapshot. available is false when nvidia-smi cannot be read."""

    available: bool
    devices: tuple[GpuDevice, ...]
    compute_apps: tuple[ComputeProcess, ...]
    error: str | None = None


CommandRunner = Callable[[Sequence[str], float], CommandResult]


def hidden_window_flag(platform: str) -> int | None:
    """Return the Windows creation flag that suppresses a console window."""

    if platform == "win32":
        return _CREATE_NO_WINDOW
    return None


def decode_output(data: bytes) -> str:
    """Decode nvidia-smi bytes, preferring UTF-8 and falling back to the locale."""

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False)
        return data.decode(encoding, errors="replace")


def default_runner(args: Sequence[str], timeout: float) -> CommandResult:
    """Run nvidia-smi as a direct argv list. shell is never enabled."""

    kwargs: dict[str, object] = {
        "capture_output": True,
        "timeout": timeout,
        "check": False,
        "shell": False,
    }
    flag = hidden_window_flag(sys.platform)
    if flag is not None:
        kwargs["creationflags"] = flag
    completed = subprocess.run(list(args), **kwargs)
    stdout_raw = completed.stdout if completed.stdout is not None else b""
    stderr_raw = completed.stderr if completed.stderr is not None else b""
    stdout = stdout_raw if isinstance(stdout_raw, str) else decode_output(stdout_raw)
    stderr = stderr_raw if isinstance(stderr_raw, str) else decode_output(stderr_raw)
    return CommandResult(completed.returncode, stdout, stderr)


def parse_gpu_csv(stdout: str) -> tuple[GpuDevice, ...]:
    """Parse `nvidia-smi --query-gpu` CSV without a header or units."""

    devices: list[GpuDevice] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = [cell.strip() for cell in next(csv.reader([stripped]))]
        if len(row) != 6:
            raise ValueError(f"expected 6 GPU columns, found {len(row)}")
        devices.append(
            GpuDevice(
                index=int(row[0]),
                name=row[1],
                memory_total_mb=_integer_field(row[2], "memory.total"),
                memory_used_mb=_integer_field(row[3], "memory.used"),
                memory_free_mb=_integer_field(row[4], "memory.free"),
                utilization_gpu=_integer_field(row[5], "utilization.gpu"),
            )
        )
    return tuple(devices)


def parse_apps_csv(stdout: str) -> tuple[ComputeProcess, ...]:
    """Parse `nvidia-smi --query-compute-apps` CSV.

    An empty result and the driver's "no running processes" notice both mean
    there are no compute apps.
    """

    apps: list[ComputeProcess] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or "no running processes" in stripped.lower():
            continue
        row = [cell.strip() for cell in next(csv.reader([stripped]))]
        if len(row) != 3:
            raise ValueError(f"expected 3 compute-app columns, found {len(row)}")
        apps.append(
            ComputeProcess(
                pid=int(row[0]),
                process_name=row[1],
                used_memory_mb=_integer_field(row[2], "used_gpu_memory"),
            )
        )
    return tuple(apps)


def assess_free_memory(snapshot: GpuSnapshot, threshold_mb: int) -> tuple[str, str]:
    """Compare free GPU memory with the configured threshold.

    Returns a preflight status of pass, warn, or unknown, plus a short detail
    that does not include process command lines.
    """

    if not snapshot.available:
        detail = snapshot.error or "nvidia-smi is unavailable"
        return "unknown", detail
    if not snapshot.devices:
        return "unknown", "nvidia-smi returned no GPU devices"
    short = [device for device in snapshot.devices if device.memory_free_mb < threshold_mb]
    if short:
        parts = ", ".join(
            f"gpu {device.index} free {device.memory_free_mb} MB" for device in short
        )
        return "warn", f"{parts} is below threshold {threshold_mb} MB"
    return "pass", f"free GPU memory is at or above {threshold_mb} MB"


def query_nvidia_smi(
    runner: CommandRunner | None = None,
    *,
    timeout: float = 5.0,
) -> GpuSnapshot:
    """Query GPU memory and compute apps.

    FileNotFoundError and timeouts become an unavailable snapshot. They are not
    raised to the caller.
    """

    command = runner or default_runner
    try:
        gpu_result = command(GPU_QUERY, timeout)
    except FileNotFoundError:
        return GpuSnapshot(False, (), (), "nvidia-smi was not found on PATH")
    except subprocess.TimeoutExpired:
        return GpuSnapshot(False, (), (), "nvidia-smi timed out")

    try:
        devices = parse_gpu_csv(gpu_result.stdout)
    except ValueError as exc:
        return GpuSnapshot(False, (), (), redact(f"could not parse nvidia-smi output: {exc}"))

    if not devices:
        message = (gpu_result.stderr or gpu_result.stdout or "nvidia-smi returned no devices").strip()
        return GpuSnapshot(False, (), (), redact(message)[:300] or "nvidia-smi returned no devices")

    note = _gpu_warning(gpu_result)
    try:
        apps_result = command(APPS_QUERY, timeout)
    except FileNotFoundError:
        return GpuSnapshot(True, devices, (), note or "nvidia-smi was not found while listing compute apps")
    except subprocess.TimeoutExpired:
        return GpuSnapshot(True, devices, (), note or "nvidia-smi timed out while listing compute apps")

    try:
        apps = parse_apps_csv(apps_result.stdout)
    except ValueError as exc:
        warning = redact(f"could not parse compute apps: {exc}")
        return GpuSnapshot(True, devices, (), _join_notes(note, warning))

    if apps_result.returncode != 0 and apps_result.stdout.strip() and not apps:
        warning = redact((apps_result.stderr or "nvidia-smi could not list compute apps").strip())[:300]
        return GpuSnapshot(True, devices, (), _join_notes(note, warning))

    return GpuSnapshot(True, devices, apps, note)


def _integer_field(value: str, field_name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not an integer") from exc


def _gpu_warning(result: CommandResult) -> str | None:
    if result.returncode == 0 or not result.stderr.strip():
        return None
    return redact(result.stderr.strip())[:300]


def _join_notes(first: str | None, second: str | None) -> str | None:
    parts = [part for part in (first, second) if part]
    if not parts:
        return None
    return " ".join(parts)
