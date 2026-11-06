"""Structured logs hide bearer tokens, raw token values, and token request ids."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.services.logging import configure_logging, flush_logs, request_id_var
from local_ai_switchboard.services.redaction import redact


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        install_root=tmp_path,
        log_dir=tmp_path / "logs",
    )


def test_redact_bearer_json_and_assignment() -> None:
    text = redact(
        'Authorization: Bearer super-secret-token {"token": "another-secret", "ok": true} api_key=third-secret'
    )
    assert "super-secret-token" not in text
    assert "another-secret" not in text
    assert "third-secret" not in text
    assert "Bearer [REDACTED]" in text
    assert "[REDACTED]" in text
    assert '"ok": true' in text


def test_log_file_omits_bearer_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    logger = logging.getLogger("local_ai_switchboard")
    logger.info("header Bearer %s", "header-secret-value-0123456789")
    flush_logs()

    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert "header-secret-value-0123456789" not in text
    assert "Bearer [REDACTED]" in text
    payload = json.loads(text.splitlines()[0])
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "-"


def test_request_id_is_preserved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    context = request_id_var.set("req-123")
    try:
        logging.getLogger("local_ai_switchboard").info("request started")
    finally:
        request_id_var.reset(context)
    flush_logs()
    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert '"request_id": "req-123"' in text


def test_log_rotation_creates_a_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings, max_bytes=300, backup_count=2)
    logger = logging.getLogger("local_ai_switchboard")
    for index in range(40):
        logger.info("rotation line %s %s", index, "x" * 40)
    flush_logs()
    logs = list(settings.resolved_log_dir().glob("switchboard.log*"))
    assert len(logs) >= 2
