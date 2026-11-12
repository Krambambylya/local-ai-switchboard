"""Persist image job metadata. A restart does not resume an image workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_ai_switchboard.providers.base import ProviderKind
from local_ai_switchboard.providers.comfyui import ImageExchange
from local_ai_switchboard.scheduler.coordinator import ScheduledJob
from local_ai_switchboard.scheduler.model import JobStatus

logger = logging.getLogger("local_ai_switchboard.storage")

INTERRUPTED_REASON = "Gateway restarted before the image job finished"
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_TERMINAL = frozenset(
    {
        JobStatus.SUCCEEDED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.INTERRUPTED.value,
    }
)
_RANK = {
    JobStatus.QUEUED.value: 1,
    JobStatus.RUNNING.value: 2,
    JobStatus.SUCCEEDED.value: 3,
    JobStatus.FAILED.value: 3,
    JobStatus.CANCELLED.value: 3,
    JobStatus.INTERRUPTED.value: 3,
}


@dataclass(frozen=True)
class ImageJobRecord:
    """One persisted image job. Paths are relative to the output root."""

    id: str
    status: str
    workflow_id: str
    prompt_id: str | None
    error: str | None
    parameters: dict[str, Any]
    output_path: str | None
    media_type: str | None
    created_at: str
    updated_at: str


class JobStore:
    """SQLite store for image jobs. Callers keep image bytes outside the database."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                prompt_id TEXT,
                error TEXT,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                output_path TEXT,
                media_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, record: ImageJobRecord) -> None:
        """Insert or advance a job. An older status cannot replace a newer one."""

        with self._lock:
            current = self._get_unlocked(record.id)
            if current is not None and _RANK.get(record.status, 0) < _RANK.get(current.status, 0):
                return
            parameters = json.dumps(record.parameters, ensure_ascii=False, separators=(",", ":"))
            if current is None:
                self._conn.execute(
                    """
                    INSERT INTO image_jobs (
                        id, status, workflow_id, prompt_id, error, parameters_json,
                        output_path, media_type, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.status,
                        record.workflow_id,
                        record.prompt_id,
                        record.error,
                        parameters,
                        record.output_path,
                        record.media_type,
                        record.created_at,
                        record.updated_at,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE image_jobs
                    SET status = ?, workflow_id = ?, prompt_id = ?, error = ?,
                        parameters_json = ?, output_path = ?, media_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.status,
                        record.workflow_id,
                        record.prompt_id or current.prompt_id,
                        record.error,
                        parameters,
                        record.output_path or current.output_path,
                        record.media_type or current.media_type,
                        record.updated_at,
                        record.id,
                    ),
                )
            self._conn.commit()

    def get(self, job_id: str) -> ImageJobRecord | None:
        with self._lock:
            return self._get_unlocked(job_id)

    def reconcile_completed_outputs(self, output_root: Path) -> int:
        """Attach files whose completion marker exists but whose success row does not.

        The marker is written after the image bytes and before the SQLite commit.
        A crash or a failed commit leaves the marker. This does not treat a
        directory without that marker as a finished image.
        """

        if not output_root.is_dir():
            return 0
        fixed = 0
        for directory in output_root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            if _JOB_ID.fullmatch(directory.name) is None:
                continue
            marker = directory / ".complete"
            if not marker.is_file() or marker.is_symlink():
                continue
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            relative = payload.get("output_path")
            media_type = payload.get("media_type")
            workflow_id = payload.get("workflow_id")
            if not isinstance(relative, str) or confined_file(output_root, relative) is None:
                continue
            if not isinstance(media_type, str) or not media_type:
                media_type = "application/octet-stream"
            current = self.get(directory.name)
            if (
                current is not None
                and current.status == JobStatus.SUCCEEDED.value
                and current.output_path == relative
            ):
                continue
            now = _timestamp()
            workflow = current.workflow_id if current is not None else ""
            if isinstance(workflow_id, str) and workflow_id:
                workflow = workflow or workflow_id
            if not workflow:
                workflow = "reconciled"
            record = ImageJobRecord(
                id=directory.name,
                status=JobStatus.SUCCEEDED.value,
                workflow_id=workflow,
                prompt_id=current.prompt_id if current is not None else None,
                error=None,
                parameters=dict(current.parameters) if current is not None else {},
                output_path=relative,
                media_type=media_type,
                created_at=current.created_at if current is not None else now,
                updated_at=now,
            )
            self.save(record)
            fixed += 1
        if fixed:
            logger.info("reconciled %s completed image jobs from output files", fixed)
        return fixed

    def recover_interrupted(self) -> int:
        """Mark queued and running image jobs interrupted. Do not queue them again."""

        now = _timestamp()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE image_jobs
                SET status = ?, error = ?, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    INTERRUPTED_REASON,
                    now,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )
            self._conn.commit()
            count = cursor.rowcount
        if count:
            logger.info("marked %s image jobs interrupted after restart", count)
        return count

    def sweep(self, output_root: Path, cutoff: datetime) -> int:
        """Delete finished jobs older than cutoff, and their files inside output_root."""

        cutoff_text = _timestamp(cutoff)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, output_path FROM image_jobs
                WHERE status IN (?, ?, ?, ?) AND updated_at < ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.INTERRUPTED.value,
                    cutoff_text,
                ),
            ).fetchall()
            removed = 0
            for row in rows:
                delete_job_files(output_root, row["id"])
                self._conn.execute("DELETE FROM image_jobs WHERE id = ?", (row["id"],))
                removed += 1
            self._conn.commit()
        if removed:
            logger.info("removed %s expired image jobs", removed)
        return removed

    def _get_unlocked(self, job_id: str) -> ImageJobRecord | None:
        row = self._conn.execute("SELECT * FROM image_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        try:
            parameters = json.loads(row["parameters_json"])
        except json.JSONDecodeError:
            parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        return ImageJobRecord(
            id=row["id"],
            status=row["status"],
            workflow_id=row["workflow_id"],
            prompt_id=row["prompt_id"],
            error=row["error"],
            parameters=parameters,
            output_path=row["output_path"],
            media_type=row["media_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def image_job_listener(store: JobStore, output_root: Path):
    """Return a scheduler listener that writes image jobs and ignores prompts in logs."""

    async def persist(job: ScheduledJob) -> None:
        if not getattr(job, "persist_image", True):
            return
        if job.kind is not ProviderKind.IMAGE:
            return
        record = record_from_job(job, output_root)
        if record is None:
            return
        await asyncio.to_thread(store.save, record)

    return persist


def record_from_job(job: ScheduledJob, output_root: Path) -> ImageJobRecord | None:
    """Copy the image fields that are safe to store. The prompt is not logged."""

    exchange = job.payload
    if not isinstance(exchange, ImageExchange):
        return None
    status = job.status.value
    error = job.error
    if job.persist_as_interrupted and status in {
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
        JobStatus.CANCELLED.value,
    }:
        status = JobStatus.INTERRUPTED.value
        error = INTERRUPTED_REASON
    output_path = None
    media_type = None
    if status == JobStatus.SUCCEEDED.value and exchange.outputs:
        stored = exchange.outputs[0]
        output_path = relative_output(output_root, stored.path)
        media_type = stored.media_type
    now = _timestamp()
    return ImageJobRecord(
        id=job.id,
        status=status,
        workflow_id=exchange.workflow_id,
        prompt_id=exchange.prompt_id,
        error=error,
        parameters=dict(exchange.parameters),
        output_path=output_path,
        media_type=media_type,
        created_at=now,
        updated_at=now,
    )


def relative_output(root: Path, path: Path) -> str | None:
    """Return a relative POSIX path, or None when the file is outside the output root."""

    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    text = relative.as_posix()
    if confined_file(root, text) is None:
        return None
    return text


def confined_file(root: Path, relative: str) -> Path | None:
    """Resolve a stored path and require it to be a file inside root."""

    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    if relative.startswith(("/", "\\")) or "\\" in relative or ":" in relative:
        return None
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
        return None
    return candidate


def delete_job_files(root: Path, job_id: str) -> None:
    """Remove one job directory when it stays inside the output root."""

    if _JOB_ID.fullmatch(job_id) is None:
        return
    root_resolved = root.resolve()
    directory = root_resolved / job_id
    if directory.is_symlink() or not directory.is_dir():
        return
    if any(path.is_symlink() for path in directory.rglob("*")):
        return
    resolved = directory.resolve()
    if not resolved.is_relative_to(root_resolved):
        return
    shutil.rmtree(resolved)


def _timestamp(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
