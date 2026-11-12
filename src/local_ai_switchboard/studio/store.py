"""SQLite metadata for studio tasks. Image bytes are files, not columns."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_ai_switchboard.storage.jobs import confined_file

INTERRUPTED_REASON = "Gateway restarted before the studio task finished"
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True)
class StudioFile:
    id: str
    task_id: str
    role: str
    relative_path: str
    media_type: str
    width: int | None
    height: int | None
    byte_size: int
    label: str | None
    asset_id: str | None
    created_at: str


@dataclass
class StudioTask:
    id: str
    conversation_id: str | None
    status: str
    stage: str | None
    prompt: str
    operation: str | None
    route: str | None
    plan: dict[str, Any] | None
    summary: str | None
    error: str | None
    models: dict[str, str]
    stages: list[dict[str, Any]]
    switch_result: str | None
    scheduler_job_id: str | None
    approved: bool
    created_at: str
    updated_at: str


class StudioStore:
    """Task rows and file ids in the gateway database."""

    def __init__(self, path: Path, root: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.root = root
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS studio_tasks (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                status TEXT NOT NULL,
                stage TEXT,
                prompt TEXT NOT NULL,
                operation TEXT,
                route TEXT,
                plan_json TEXT,
                summary TEXT,
                error TEXT,
                models_json TEXT NOT NULL DEFAULT '{}',
                stages_json TEXT NOT NULL DEFAULT '[]',
                switch_result TEXT,
                scheduler_job_id TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS studio_files (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                byte_size INTEGER NOT NULL,
                label TEXT,
                asset_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def recover_interrupted(self) -> int:
        """Mark tasks that were still on the GPU as interrupted. Do not queue them again."""

        now = _timestamp()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE studio_tasks
                SET status = 'interrupted', error = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (INTERRUPTED_REASON, now),
            )
            self._conn.commit()
            return cursor.rowcount

    def create_task(
        self,
        task_id: str,
        *,
        prompt: str,
        conversation_id: str | None,
        operation: str | None,
    ) -> StudioTask:
        now = _timestamp()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO studio_tasks (
                    id, conversation_id, status, stage, prompt, operation,
                    models_json, stages_json, approved, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'uploading', ?, ?, '{}', '[]', 0, ?, ?)
                """,
                (task_id, conversation_id, prompt, operation, now, now),
            )
            self._conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("studio task was not stored")
        return task

    def get_task(self, task_id: str) -> StudioTask | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM studio_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return _task(row)

    def update_task(self, task_id: str, **fields: Any) -> StudioTask | None:
        allowed = {
            "status",
            "stage",
            "operation",
            "route",
            "plan",
            "summary",
            "error",
            "models",
            "stages",
            "switch_result",
            "scheduler_job_id",
            "approved",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unsupported task field {key}")
            column = {
                "plan": "plan_json",
                "models": "models_json",
                "stages": "stages_json",
            }.get(key, key)
            if key in {"plan", "models", "stages"}:
                value = json.dumps(value, ensure_ascii=False)
            if key == "approved":
                value = 1 if value else 0
            assignments.append(f"{column} = ?")
            values.append(value)
        if not assignments:
            return self.get_task(task_id)
        assignments.append("updated_at = ?")
        values.append(_timestamp())
        values.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE studio_tasks SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            self._conn.commit()
        return self.get_task(task_id)

    def latest_summary(self, conversation_id: str | None, *, exclude: str) -> str | None:
        if not conversation_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT summary FROM studio_tasks
                WHERE conversation_id = ? AND id != ? AND status = 'succeeded' AND summary IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (conversation_id, exclude),
            ).fetchone()
        if row is None:
            return None
        text = row["summary"]
        return text if isinstance(text, str) and text.strip() else None

    def add_file(
        self,
        file_id: str,
        *,
        task_id: str,
        role: str,
        relative_path: str,
        media_type: str,
        width: int | None,
        height: int | None,
        byte_size: int,
        label: str | None = None,
        asset_id: str | None = None,
    ) -> StudioFile:
        now = _timestamp()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO studio_files (
                    id, task_id, role, relative_path, media_type, width, height,
                    byte_size, label, asset_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    task_id,
                    role,
                    relative_path,
                    media_type,
                    width,
                    height,
                    byte_size,
                    label,
                    asset_id,
                    now,
                ),
            )
            self._conn.commit()
        stored = self.get_file(file_id)
        if stored is None:
            raise RuntimeError("studio file was not stored")
        return stored

    def list_files(self, task_id: str, role: str | None = None) -> list[StudioFile]:
        query = "SELECT * FROM studio_files WHERE task_id = ?"
        values: list[Any] = [task_id]
        if role is not None:
            query += " AND role = ?"
            values.append(role)
        query += " ORDER BY created_at, id"
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [_file(row) for row in rows]

    def get_file(self, file_id: str) -> StudioFile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM studio_files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return None
        return _file(row)

    def resolve(self, relative_path: str) -> Path | None:
        return confined_file(self.root, relative_path)

    def job_dir(self, task_id: str) -> Path:
        directory = (self.root / "jobs" / task_id).resolve()
        root = self.root.resolve()
        if not directory.is_relative_to(root):
            raise ValueError("task directory escapes storage")
        return directory


def task_is_terminal(status: str) -> bool:
    return status in _TERMINAL


def _task(row: sqlite3.Row) -> StudioTask:
    plan = _object(row["plan_json"])
    models = _object(row["models_json"])
    stages = _list(row["stages_json"])
    return StudioTask(
        id=row["id"],
        conversation_id=row["conversation_id"],
        status=row["status"],
        stage=row["stage"],
        prompt=row["prompt"],
        operation=row["operation"],
        route=row["route"],
        plan=plan if isinstance(plan, dict) else None,
        summary=row["summary"],
        error=row["error"],
        models=models if isinstance(models, dict) else {},
        stages=stages,
        switch_result=row["switch_result"],
        scheduler_job_id=row["scheduler_job_id"],
        approved=bool(row["approved"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _file(row: sqlite3.Row) -> StudioFile:
    return StudioFile(
        id=row["id"],
        task_id=row["task_id"],
        role=row["role"],
        relative_path=row["relative_path"],
        media_type=row["media_type"],
        width=row["width"],
        height=row["height"],
        byte_size=row["byte_size"],
        label=row["label"],
        asset_id=row["asset_id"],
        created_at=row["created_at"],
    )


def _object(value: object) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _list(value: object) -> list[dict[str, Any]]:
    parsed = _object(value)
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
