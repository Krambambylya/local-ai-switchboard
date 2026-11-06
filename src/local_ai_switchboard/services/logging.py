"""JSON logs with rotation, request ids, and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TextIO

from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.services.redaction import redact

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_LOGGER_NAME = "local_ai_switchboard"


class RedactionFilter(logging.Filter):
    """Redact secrets on a log record before any handler formats it."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info), self._secrets)
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage(), self._secrets),
            "request_id": redact(request_id_var.get(), self._secrets),
        }
        if record.exc_text:
            payload["exception"] = redact(record.exc_text, self._secrets)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    settings: Settings,
    *,
    max_bytes: int = 1_048_576,
    backup_count: int = 5,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Attach a rotating JSON file handler and a stderr handler.

    Calling this again replaces the previous handlers so tests can retarget the
    log directory.
    """

    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    logger.setLevel(settings.log_level)
    logger.propagate = False

    log_dir = settings.resolved_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "switchboard.log"

    secrets: tuple[str, ...] = ()
    redaction_filter = RedactionFilter(secrets)
    formatter = JsonFormatter(secrets)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.addFilter(redaction_filter)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    stream_handler.addFilter(redaction_filter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def flush_logs() -> None:
    """Flush the package logger so tests can read the log file immediately."""

    logger = logging.getLogger(_LOGGER_NAME)
    for handler in logger.handlers:
        handler.flush()
