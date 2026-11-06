"""SQLite image jobs survive restart. Unfinished jobs are not submitted again."""

from __future__ import annotations

import importlib.util
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.base import ProviderKind
from local_ai_switchboard.providers.unavailable import UnavailableProvider
from local_ai_switchboard.scheduler.model import JobStatus, SchedulerState
from local_ai_switchboard.services.logging import flush_logs
from local_ai_switchboard.storage.jobs import (
    INTERRUPTED_REASON,
    ImageJobRecord,
    JobStore,
    confined_file,
    delete_job_files,
)

PNG = b"\x89PNG\r\n\x1a\nfake-image"


def _helpers():
    path = Path(__file__).with_name("test_comfyui.py")
    spec = importlib.util.spec_from_file_location("comfy_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(
    job_id: str,
    status: str,
    *,
    output_path: str | None = None,
    updated_at: str = "2026-11-05T12:00:00Z",
) -> ImageJobRecord:
    return ImageJobRecord(
        id=job_id,
        status=status,
        workflow_id="fixture-image",
        prompt_id="prompt-1" if status != JobStatus.QUEUED.value else None,
        error=None,
        parameters={"prompt": "stored-prompt"},
        output_path=output_path,
        media_type="image/png" if output_path else None,
        created_at="2026-11-05T12:00:00Z",
        updated_at=updated_at,
    )


def test_restart_marks_unfinished_jobs_interrupted_and_keeps_files(tmp_path: Path) -> None:
    output = tmp_path / "outputs" / "images"
    finished = output / "done-job"
    finished.mkdir(parents=True)
    (finished / "fixture.png").write_bytes(PNG)
    store = JobStore(tmp_path / "switchboard.db")
    store.save(_record("queued-job", JobStatus.QUEUED.value))
    store.save(_record("running-job", JobStatus.RUNNING.value))
    store.save(_record("done-job", JobStatus.SUCCEEDED.value, output_path="done-job/fixture.png"))
    assert store.recover_interrupted() == 2
    assert store.get("queued-job").status == JobStatus.INTERRUPTED.value
    assert store.get("queued-job").error == INTERRUPTED_REASON
    assert store.get("running-job").status == JobStatus.INTERRUPTED.value
    assert store.get("done-job").status == JobStatus.SUCCEEDED.value
    assert (finished / "fixture.png").read_bytes() == PNG
    assert store.recover_interrupted() == 0
    store.close()


def test_path_traversal_is_rejected_and_unicode_names_stay_inside_the_root(
    tmp_path: Path,
    unavailable_gpu: GpuSnapshot,
) -> None:
    storage = tmp_path / "AI Projects" / "данные"
    output = storage / "outputs" / "images"
    secret = storage / "outputs" / "secret.png"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"secret")
    job_dir = output / "job-1"
    job_dir.mkdir(parents=True)
    filename = "мой файл.png"
    (job_dir / filename).write_bytes(PNG)
    store = JobStore(storage / "switchboard.db")
    store.save(_record("escape", JobStatus.SUCCEEDED.value, output_path="../secret.png"))
    store.save(_record("job-1", JobStatus.SUCCEEDED.value, output_path=f"job-1/{filename}"))
    store.close()

    assert confined_file(output, "../secret.png") is None
    assert confined_file(output, "job-1/../../secret.png") is None
    assert confined_file(output, "C:/Windows/secret.png") is None
    assert confined_file(output, f"job-1/{filename}") == (job_dir / filename).resolve()

    settings = Settings(
        install_root=tmp_path,
        storage_dir=storage,
        log_dir=tmp_path / "logs",
        output_retention_days=0,
    )
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
            ProviderKind.IMAGE: UnavailableProvider(ProviderKind.IMAGE, "ComfyUI"),
        },
    )
    with TestClient(app) as client:
        denied = client.get("/v1/jobs/escape/download")
        assert denied.status_code == 404
        saved = client.get("/v1/jobs/job-1/download")
        assert saved.status_code == 200
        assert saved.content == PNG
        listed = client.get("/v1/jobs/job-1")
        assert listed.status_code == 200
        assert listed.json()["outputs"] == [{"filename": filename, "media_type": "image/png"}]
        assert str(storage) not in listed.text
        assert secret.read_bytes() == b"secret"

    store = JobStore(storage / "switchboard.db")
    old = _record(
        "old-job",
        JobStatus.SUCCEEDED.value,
        output_path="old-job/fixture.png",
        updated_at="2000-01-01T00:00:00Z",
    )
    store.save(old)
    (output / "old-job").mkdir()
    (output / "old-job" / "fixture.png").write_bytes(PNG)
    removed = store.sweep(output, datetime.now(timezone.utc) - timedelta(days=1))
    assert removed >= 1
    assert not (output / "old-job").exists()
    assert secret.read_bytes() == b"secret"
    assert (job_dir / filename).read_bytes() == PNG
    store.close()
    delete_job_files(output, "..")
    assert secret.read_bytes() == b"secret"


def test_running_image_is_interrupted_on_restart_and_is_not_resubmitted(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
) -> None:
    helpers = _helpers()
    workflows = settings.resolved_storage_dir() / "workflows"
    helpers._write_registry(workflows)
    server = helpers.ComfyServer()
    server.history_mode = "never"
    provider = helpers._provider(settings, workflows, server, job_timeout=30, poll=5)
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
            ProviderKind.IMAGE: provider,
        },
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/assets",
            json={"workflow_id": "fixture-image", "prompt": helpers.PROMPT},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        deadline = time.monotonic() + 2
        status = None
        while time.monotonic() < deadline:
            record = app.state.job_store.get(job_id)
            status = None if record is None else record.status
            if status == JobStatus.RUNNING.value:
                break
            time.sleep(0.01)
        assert status == JobStatus.RUNNING.value
        assert server.prompts

    restarted = helpers.ComfyServer()
    restarted.history_mode = "never"
    provider = helpers._provider(settings, workflows, restarted, job_timeout=1, poll=0.01)
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
            ProviderKind.IMAGE: provider,
        },
    )
    with TestClient(app) as client:
        assert app.state.scheduler._state is SchedulerState.IDLE
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        assert response.json()["status"] == JobStatus.INTERRUPTED.value
        assert response.json()["error"] == INTERRUPTED_REASON
        assert helpers.PROMPT not in response.text
        assert restarted.prompts == []
    flush_logs()
    log_text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert helpers.PROMPT not in log_text


def test_completed_image_is_downloaded_after_restart(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
) -> None:
    helpers = _helpers()
    workflows = settings.resolved_storage_dir() / "workflows"
    helpers._write_registry(workflows)
    server = helpers.ComfyServer()
    server.complete_after = 1
    provider = helpers._provider(settings, workflows, server, poll=0.01)
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
            ProviderKind.IMAGE: provider,
        },
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/assets",
            json={"workflow_id": "fixture-image", "prompt": helpers.PROMPT, "width": 768, "height": 768},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        deadline = time.monotonic() + 3
        body: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/v1/jobs/{job_id}")
            body = response.json()
            if body["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.01)
        assert body["status"] == "succeeded"
        record = app.state.job_store.get(job_id)
        assert record is not None
        assert record.output_path == f"{job_id}/fixture.png"
        assert record.parameters["prompt"] == helpers.PROMPT
        assert helpers.PROMPT not in response.text

    fresh = helpers.ComfyServer()
    provider = helpers._provider(settings, workflows, fresh)
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: UnavailableProvider(ProviderKind.LLM, "Ollama"),
            ProviderKind.IMAGE: provider,
        },
    )
    with TestClient(app) as client:
        downloaded = client.get(f"/v1/jobs/{job_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == helpers.PNG
        assert fresh.prompts == []
