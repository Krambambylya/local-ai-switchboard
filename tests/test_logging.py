"""Structured logs hide bearer tokens, raw token values, and token request ids."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.services.logging import configure_logging, flush_logs, request_id_var
from local_ai_switchboard.services.redaction import redact

TOKEN = "test-token-value-0123456789abcdef"


def _settings(tmp_path: Path, token_file: Path | None = None) -> Settings:
    return Settings(
        install_root=tmp_path,
        log_dir=tmp_path / "logs",
        token_file=token_file or (tmp_path / "secrets" / "token.txt"),
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


def test_log_file_omits_bearer_token_and_configured_secret(tmp_path: Path) -> None:
    token_file = tmp_path / "secrets" / "token.txt"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(TOKEN, encoding="utf-8")
    settings = _settings(tmp_path, token_file)
    configure_logging(settings)
    logger = logging.getLogger("local_ai_switchboard")
    logger.info("header Bearer %s", "header-secret-value-0123456789")
    logger.info("loaded %s", TOKEN)
    flush_logs()

    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert "header-secret-value-0123456789" not in text
    assert TOKEN not in text
    assert "Bearer [REDACTED]" in text
    payload = json.loads(text.splitlines()[0])
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "-"


def test_request_id_equal_to_token_is_redacted(tmp_path: Path) -> None:
    token_file = tmp_path / "secrets" / "token.txt"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(TOKEN, encoding="utf-8")
    settings = _settings(tmp_path, token_file)
    configure_logging(settings)
    token = request_id_var.set(TOKEN)
    try:
        logging.getLogger("local_ai_switchboard").info("request started")
    finally:
        request_id_var.reset(token)
    flush_logs()
    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert '"request_id": "[REDACTED]"' in text


def test_log_rotation_creates_a_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings, max_bytes=300, backup_count=2)
    logger = logging.getLogger("local_ai_switchboard")
    for index in range(40):
        logger.info("rotation line %s %s", index, "x" * 40)
    flush_logs()
    logs = list(settings.resolved_log_dir().glob("switchboard.log*"))
    assert len(logs) >= 2
