"""Studio task API. Files stay on disk; SQLite keeps metadata."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuDevice, GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.base import JobContext, ProviderError, ProviderKind
from local_ai_switchboard.providers.ollama import ChatExchange
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import SchedulerConfig
from local_ai_switchboard.studio.plan import parse_plan


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class ImmediateLlm:
    kind = ProviderKind.LLM

    def __init__(self, content: str) -> None:
        self.content = content
        self.bodies: list[dict] = []

    async def prepare(self) -> None:
        return None

    async def run(self, job: JobContext) -> None:
        exchange = job.payload
        assert isinstance(exchange, ChatExchange)
        if exchange.prepare is not None:
            exchange.body = dict(exchange.prepare())
        self.bodies.append(dict(exchange.body))
        body = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.content},
                    "finish_reason": "stop",
                }
            ],
        }
        exchange.open(200, "application/json")
        await exchange.chunks.put(json.dumps(body).encode("utf-8"))
        await exchange.chunks.put(None)

    async def cancel(self, job: JobContext) -> None:
        del job

    async def release_resources(self) -> None:
        return None

    async def resources_released(self) -> bool:
        return True

    async def is_busy(self) -> bool:
        return False


class ImmediateImage:
    kind = ProviderKind.IMAGE

    async def prepare(self) -> None:
        return None

    async def run(self, job: JobContext) -> None:
        raise ProviderError("image stage should not run for analyze")

    async def cancel(self, job: JobContext) -> None:
        del job

    async def release_resources(self) -> None:
        return None

    async def resources_released(self) -> bool:
        return True

    async def is_busy(self) -> bool:
        return False


class ReadyMonitor:
    async def snapshot(self) -> GpuSnapshot:
        return GpuSnapshot(True, (GpuDevice(0, "RTX", 8188, 1000, 7000, 0),), ())


@pytest.fixture
def studio_client(tmp_path: Path):
    settings = Settings(
        install_root=tmp_path,
        storage_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        studio_max_image_bytes=1024 * 1024,
        studio_max_images=2,
    )
    plan = {
        "summary": "A red square",
        "description": "A solid red square fills the frame.",
        "operations": ["analyze"],
        "assets": [],
    }
    llm = ImmediateLlm(json.dumps(plan))
    image = ImmediateImage()
    scheduler = GpuScheduler(
        config=SchedulerConfig(
            max_queue_depth=4,
            job_timeout_seconds=30,
            release_timeout_seconds=1,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers={ProviderKind.LLM: llm, ProviderKind.IMAGE: image},
        monitor=ReadyMonitor(),
    )
    app = create_app(
        settings,
        gpu_probe=lambda: GpuSnapshot(True, (GpuDevice(0, "RTX", 8188, 1000, 7000, 0),), ()),
        providers={ProviderKind.LLM: llm, ProviderKind.IMAGE: image},
        scheduler=scheduler,
    )
    with TestClient(app) as client:
        yield client, llm, settings


def _wait_task(client: TestClient, task_id: str) -> dict:
    body = {}
    for _ in range(80):
        body = client.get(f"/v1/studio/tasks/{task_id}").json()
        if body["status"] in {"succeeded", "failed", "cancelled", "interrupted", "awaiting_approval"}:
            return body
        time.sleep(0.05)
    return body


def test_create_analyze_task_and_download_file(studio_client) -> None:
    client, llm, settings = studio_client
    response = client.post(
        "/v1/studio/tasks",
        data={"prompt": "Опиши содержимое прикреплённого изображения", "conversation_id": "chat1"},
        files=[("attachments", ("board.png", _png(), "image/png"))],
    )
    assert response.status_code == 202
    task_id = response.json()["id"]
    body = _wait_task(client, task_id)
    assert body["status"] == "succeeded"
    assert body["summary"]
    assert body["files"]
    assert any(item["role"] == "original" for item in body["files"])
    assert llm.bodies
    content = llm.bodies[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert str(content[1]["image_url"]).startswith("data:image/")
    file_id = body["files"][0]["id"]
    download = client.get(f"/v1/studio/files/{file_id}")
    assert download.status_code == 200
    assert download.content[:8] == b"\x89PNG\r\n\x1a\n"
    absolute = str(settings.resolved_storage_dir())
    assert absolute not in response.text
    assert absolute not in client.get(f"/v1/studio/tasks/{task_id}").text


def test_rejects_oversized_and_corrupt_uploads(studio_client) -> None:
    client, _llm, _settings = studio_client
    huge = client.post(
        "/v1/studio/tasks",
        data={"prompt": "look"},
        files=[("attachments", ("big.png", b"x" * (2 * 1024 * 1024), "image/png"))],
    )
    assert huge.status_code == 400
    bad = client.post(
        "/v1/studio/tasks",
        data={"prompt": "look"},
        files=[("attachments", ("bad.png", b"not-png", "image/png"))],
    )
    assert bad.status_code == 400


def test_regions_require_approval_state(studio_client) -> None:
    client, _llm, _settings = studio_client
    created = client.post(
        "/v1/studio/tasks",
        data={"prompt": "analyze only", "operation": "analyze"},
        files=[("attachments", ("board.png", _png(), "image/png"))],
    )
    task_id = created.json()["id"]
    body = _wait_task(client, task_id)
    assert body["status"] == "succeeded"
    patched = client.patch(
        f"/v1/studio/tasks/{task_id}/regions",
        json={
            "regions": [
                {
                    "id": "a",
                    "operation": "extract",
                    "bbox": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                    "confidence": 1,
                }
            ]
        },
    )
    assert patched.status_code == 400
    assert parse_plan('{"summary":"ok","operations":["analyze"],"assets":[]}').summary == "ok"
