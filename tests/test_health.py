"""Health endpoints stay free of secrets and GPU details."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.unavailable import unavailable_providers
from local_ai_switchboard.services.logging import flush_logs


def test_live_is_open_and_echoes_a_safe_request_id(client: TestClient, settings: Settings) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "req-123"})
    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert response.headers["X-Request-ID"] == "req-123"
    flush_logs()
    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert "req-123" in text
    assert "GET /health/live 200" in text


def test_live_rejects_request_ids_that_could_break_logs(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "bad\nid"})
    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert "\n" not in request_id
    assert request_id != "bad\nid"


def test_ready_is_ok_without_leaking_details(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "127.0.0.1" not in response.text
    assert "nvidia" not in response.text.lower()


def test_authorization_header_is_not_written_to_the_log(client: TestClient, settings: Settings) -> None:
    secret = "header-secret-value-0123456789"
    client.get("/health/live", headers={"Authorization": f"Bearer {secret}"})
    logging.getLogger("local_ai_switchboard").info("saw Bearer %s", secret)
    flush_logs()
    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert secret not in text
    assert "Bearer [REDACTED]" in text


def test_ready_is_ok_without_a_credential_file(tmp_path: Path) -> None:
    settings = Settings(install_root=tmp_path, log_dir=tmp_path / "logs")
    unavailable = GpuSnapshot(False, (), (), "nvidia-smi was not found on PATH")
    app = create_app(settings, gpu_probe=lambda: unavailable, providers=unavailable_providers())
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready", headers={"Authorization": "Bearer not-a-credential"})
    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}
    assert "not-a-credential" not in ready.text


def test_ready_stays_ok_when_gpu_memory_is_low(settings: Settings) -> None:
    from local_ai_switchboard.gpu.nvidia_smi import GpuDevice

    snapshot = GpuSnapshot(
        True,
        (GpuDevice(0, "NVIDIA GeForce RTX 4060", 8188, 7900, 288, 95),),
        (),
        None,
    )
    app = create_app(settings, gpu_probe=lambda: snapshot, providers=unavailable_providers())
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_schema_and_cors_are_not_published(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    response = client.get("/health/live", headers={"Origin": "https://example.invalid"})
    assert "access-control-allow-origin" not in {name.lower() for name in response.headers}
