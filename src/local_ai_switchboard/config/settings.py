"""Pydantic settings for the gateway process.

Provider URLs are rejected unless they target loopback. The gateway itself may
bind a LAN address. Paths use pathlib so the same code runs on macOS and Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePath

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def install_root_for(platform: str, home: PurePath) -> PurePath:
    """Return the default install root for a platform and home directory."""

    if platform == "win32":
        return home / "AI" / "local-ai-switchboard"
    return home / ".local" / "share" / "local-ai-switchboard"


def default_install_root() -> Path:
    """Return the user-scoped install root for this machine."""

    return Path(install_root_for(sys.platform, Path.home()))


def absolute_path(value: Path) -> Path:
    """Expand a user path and make it absolute without requiring it to exist."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def assert_loopback_url(url: AnyHttpUrl) -> None:
    """Raise ValueError when a provider URL is not confined to this machine."""

    if url.username or url.password:
        raise ValueError("credentials are not allowed in provider URLs")
    host = (url.host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            "Ollama and ComfyUI must use a loopback host "
            "(localhost, 127.0.0.1, or ::1)"
        )


class Settings(BaseSettings):
    """Gateway configuration loaded from the environment and an optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="LAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    install_root: Path = Field(default_factory=default_install_root)
    storage_dir: Path | None = None
    log_dir: Path | None = None
    ollama_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:11434")
    comfyui_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8188")
    comfyui_portable_dir: Path | None = None
    workflows_dir: Path | None = None
    comfyui_reserve_vram_mb: int | None = Field(default=None, ge=0, le=65536)
    comfyui_startup_timeout_seconds: float = Field(default=60, gt=0, le=600)
    gpu_memory_threshold_mb: int = Field(default=1024, gt=0, le=65536)
    max_queue_depth: int = Field(default=8, ge=1, le=1000)
    job_timeout_seconds: float = Field(default=300, gt=0, le=86400)
    release_timeout_seconds: float = Field(default=120, gt=0, le=86400)
    memory_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    output_retention_days: int = Field(default=14, ge=0, le=3650)
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8787, ge=1, le=65535)
    log_level: str = "INFO"

    @field_validator("ollama_url", "comfyui_url")
    @classmethod
    def provider_urls_are_loopback(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        assert_loopback_url(value)
        return value

    @field_validator("api_host")
    @classmethod
    def api_host_is_a_bind_address(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate or any(character.isspace() for character in candidate):
            raise ValueError("api_host must be a host or bind address")
        if "://" in candidate:
            raise ValueError("api_host must be a host, not a URL")
        return candidate

    @field_validator("comfyui_portable_dir", "workflows_dir", mode="before")
    @classmethod
    def blank_optional_path_is_unset(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("comfyui_reserve_vram_mb", mode="before")
    @classmethod
    def blank_reserve_vram_is_unset(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def log_level_is_known(cls, value: str) -> str:
        name = value.upper()
        if name not in _LOG_LEVELS:
            raise ValueError("log_level must be a standard logging level")
        return name

    @model_validator(mode="after")
    def derive_paths(self) -> Settings:
        root = absolute_path(self.install_root)
        self.install_root = root
        self.storage_dir = absolute_path(self.storage_dir or root / "data")
        self.log_dir = absolute_path(self.log_dir or root / "logs")
        if self.comfyui_portable_dir is not None:
            self.comfyui_portable_dir = absolute_path(self.comfyui_portable_dir)
        if self.workflows_dir is not None:
            self.workflows_dir = absolute_path(self.workflows_dir)
        return self

    def resolved_storage_dir(self) -> Path:
        """Return the configured storage directory."""

        if self.storage_dir is None:
            raise RuntimeError("storage directory was not resolved")
        return self.storage_dir

    def resolved_log_dir(self) -> Path:
        """Return the configured log directory."""

        if self.log_dir is None:
            raise RuntimeError("log directory was not resolved")
        return self.log_dir

    def __repr__(self) -> str:
        return (
            "Settings("
            f"install_root={self.install_root!r}, "
            f"storage_dir={self.storage_dir!r}, "
            f"api_host={self.api_host!r}, "
            f"api_port={self.api_port!r}, "
            f"ollama_url={self.ollama_url!r}, "
            f"comfyui_url={self.comfyui_url!r}, "
            f"gpu_memory_threshold_mb={self.gpu_memory_threshold_mb!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()
