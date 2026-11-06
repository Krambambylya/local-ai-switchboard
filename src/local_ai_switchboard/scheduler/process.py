"""Guards that keep a single scheduler in the Gateway process."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import IO, TextIO

_WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "LAS_WORKERS")


class SchedulerAlreadyRunning(RuntimeError):
    """Another Gateway process already holds the scheduler lock."""


def enforce_single_worker(
    environ: Mapping[str, str] | None = None,
    output: TextIO | None = None,
) -> None:
    """Refuse environment settings that would start multiple Gateway processes.

    Uvicorn worker processes would each create a scheduler and race for the GPU.
    """

    source = os.environ if environ is None else environ
    sink = sys.stderr if output is None else output
    for name in _WORKER_ENV_VARS:
        raw = source.get(name)
        if raw is None or raw.strip() == "":
            continue
        try:
            count = int(raw)
        except ValueError as exc:
            raise SystemExit(f"{name} must be 1") from exc
        if count != 1:
            message = (
                f"{name}={count}. The GPU scheduler runs once per machine and "
                "cannot be deployed with multiple worker processes."
            )
            print(message, file=sink)
            raise SystemExit(2)


def acquire_process_lock(path: Path) -> IO[bytes]:
    """Take an exclusive lock so a second Gateway process fails at startup.

    On Unix this uses flock. On Windows it uses msvcrt byte-range locking.
    The Windows path is implemented and has not been executed on Windows.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise SchedulerAlreadyRunning(f"scheduler lock is held: {path}") from exc
    return handle


def release_process_lock(handle: IO[bytes]) -> None:
    """Release a lock acquired by acquire_process_lock."""

    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
