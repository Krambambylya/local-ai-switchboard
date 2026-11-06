"""Configuration defaults, Windows path layout, and loopback enforcement."""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from pydantic import ValidationError

from local_ai_switchboard.config.settings import Settings, absolute_path, install_root_for


def _host(value: str | None) -> str:
    return (value or "").strip().strip("[]").lower()


def test_windows_default_layout_preserves_spaces_and_unicode() -> None:
    home = PureWindowsPath(r"C:\Users\Алина")
    root = install_root_for("win32", home)
    storage = root / "data" / "my images"
    assert root == PureWindowsPath(r"C:\Users\Алина\AI\local-ai-switchboard")
    assert storage == PureWindowsPath(r"C:\Users\Алина\AI\local-ai-switchboard\data\my images")
    assert " " in str(storage)
    assert "Алина" in str(storage)


def test_posix_default_layout() -> None:
    home = PurePosixPath("/Users/dev")
    root = install_root_for("darwin", home)
    assert root == PurePosixPath("/Users/dev/.local/share/local-ai-switchboard")


def test_derived_directories_follow_install_root(tmp_path: Path) -> None:
    settings = Settings(install_root=tmp_path)
    assert settings.resolved_storage_dir() == (tmp_path / "data").absolute()
    assert settings.resolved_log_dir() == (tmp_path / "logs").absolute()
    assert settings.resolved_token_file() == (tmp_path / "secrets" / "token.txt").absolute()


def test_storage_dir_override_keeps_spaces_and_unicode(tmp_path: Path) -> None:
    storage = tmp_path / "my models" / "данные"
    settings = Settings(install_root=tmp_path, storage_dir=storage)
    assert settings.resolved_storage_dir() == storage.absolute()


def test_relative_storage_dir_becomes_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(install_root=tmp_path, storage_dir=Path("custom data"))
    assert settings.resolved_storage_dir() == (tmp_path / "custom data").absolute()
    assert settings.resolved_storage_dir().is_absolute()


def test_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "custom store"
    monkeypatch.setenv("LAS_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("LAS_STORAGE_DIR", str(storage))
    monkeypatch.setenv("LAS_API_PORT", "8790")
    monkeypatch.setenv("LAS_GPU_MEMORY_THRESHOLD_MB", "2048")
    monkeypatch.setenv("LAS_OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("LAS_COMFYUI_URL", "http://[::1]:8188")
    settings = Settings()
    assert settings.api_port == 8790
    assert settings.gpu_memory_threshold_mb == 2048
    assert settings.resolved_storage_dir() == storage.absolute()
    assert _host(settings.ollama_url.host) == "localhost"
    assert _host(settings.comfyui_url.host) == "::1"


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("ollama_url", "http://0.0.0.0:11434"),
        ("comfyui_url", "http://192.168.1.20:8188"),
        ("ollama_url", "http://example.com:11434"),
        ("comfyui_url", "http://user:secret@127.0.0.1:8188"),
    ],
)
def test_provider_urls_must_be_loopback(field: str, url: str) -> None:
    with pytest.raises(ValidationError, match="loopback|credentials"):
        Settings(**{field: url})


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"])
def test_loopback_provider_urls_are_accepted(url: str) -> None:
    settings = Settings(ollama_url=url, comfyui_url=url)  # type: ignore[arg-type]
    assert _host(settings.ollama_url.host) in {"127.0.0.1", "localhost", "::1"}


def test_api_port_and_host_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(api_port=0)
    with pytest.raises(ValidationError):
        Settings(api_port=70000)
    with pytest.raises(ValidationError):
        Settings(api_host="http://0.0.0.0")
    with pytest.raises(ValidationError):
        Settings(gpu_memory_threshold_mb=0)


def test_absolute_path_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expanded = absolute_path(Path("~/AI data"))
    assert expanded == (tmp_path / "AI data").absolute()


def test_repr_does_not_include_token_file_contents(tmp_path: Path, token_file: Path) -> None:
    settings = Settings(install_root=tmp_path, token_file=token_file)
    rendered = repr(settings)
    assert "test-token-value-0123456789abcdef" not in rendered
    if sys.platform == "win32":
        assert settings.install_root.drive
