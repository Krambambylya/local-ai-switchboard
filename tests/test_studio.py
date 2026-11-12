"""Static Local AI Studio routes. Chat and health behaviour stay on their own tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_ai_switchboard.api.studio import confined_studio_file, packaged_studio_dir
from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.unavailable import unavailable_providers


def _client(settings: Settings, gpu: GpuSnapshot, studio_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        settings,
        gpu_probe=lambda: gpu,
        providers=unavailable_providers(),
        studio_dir=studio_dir,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def studio_client(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
    tmp_path: Path,
) -> Iterator[TestClient]:
    studio = tmp_path / "studio-files"
    assets = studio / "assets"
    assets.mkdir(parents=True)
    (studio / "index.html").write_text(
        "<!DOCTYPE html><title>Local AI Studio</title><p>studio-shell</p>",
        encoding="utf-8",
    )
    (assets / "app-abcdefgh.js").write_text("console.log('studio')", encoding="utf-8")
    (assets / "plain.js").write_text("console.log('plain')", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
    (assets / "leak.txt").symlink_to(secret)
    yield from _client(settings, unavailable_gpu, studio)


def test_studio_serves_index_asset_and_leaves_api_routes_alone(studio_client: TestClient) -> None:
    page = studio_client.get("/studio")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-cache"
    assert "studio-shell" in page.text

    slash = studio_client.get("/studio/")
    assert slash.status_code == 200
    assert "studio-shell" in slash.text

    script = studio_client.get("/studio/assets/app-abcdefgh.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"].startswith("public")
    assert "console.log('studio')" in script.text

    plain = studio_client.get("/studio/assets/plain.js")
    assert plain.status_code == 200
    assert plain.headers["cache-control"] == "no-cache"

    missing = studio_client.get("/studio/assets/missing.js")
    assert missing.status_code == 404
    assert "SECRET-OUTSIDE" not in missing.text

    spa = studio_client.get("/studio/chat/1")
    assert spa.status_code == 200
    assert "studio-shell" in spa.text

    models = studio_client.get("/v1/models")
    assert models.status_code == 503
    assert models.headers["content-type"].startswith("application/json")
    assert "<!DOCTYPE" not in models.text

    health = studio_client.get("/health/live")
    assert health.status_code == 200
    assert health.json() == {"status": "live"}


def test_studio_path_cannot_escape_the_build_directory(
    studio_client: TestClient,
    tmp_path: Path,
) -> None:
    studio = tmp_path / "studio-files"
    assert confined_studio_file(studio, "../secret.txt") is None
    assert confined_studio_file(studio, "assets/../../secret.txt") is None
    assert confined_studio_file(studio, "assets/leak.txt") is None
    assert confined_studio_file(studio, "assets/app-abcdefgh.js") is not None

    for path in (
        "/studio/../secret.txt",
        "/studio/assets/../../secret.txt",
        "/studio/assets/leak.txt",
    ):
        response = studio_client.get(path)
        assert "SECRET-OUTSIDE" not in response.text
        assert str(tmp_path) not in response.text


def test_missing_studio_build_does_not_break_the_api(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-studio"
    empty.mkdir()
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers=unavailable_providers(),
        studio_dir=empty,
    )
    with TestClient(app) as client:
        missing = client.get("/studio")
        assert missing.status_code == 503
        assert "text/html" in missing.headers["content-type"]
        assert "not built" in missing.text.lower()
        assert str(empty) not in missing.text
        assert client.get("/health/live").json() == {"status": "live"}
        models = client.get("/v1/models")
        assert models.headers["content-type"].startswith("application/json")
        assert "<!DOCTYPE" not in models.text


def test_packaged_studio_index_is_shipped() -> None:
    index = packaged_studio_dir() / "index.html"
    assert index.is_file()
    text = index.read_text(encoding="utf-8")
    assert "Local AI Studio" in text
    assert "/studio/" in text
