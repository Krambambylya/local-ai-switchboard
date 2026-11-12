"""Remove bearer tokens and secret fields from text before it is logged."""

from __future__ import annotations

import re
from collections.abc import Sequence

_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_JSON_SECRET = re.compile(
    r'(?i)("(?:token|access_token|api_key|authorization|password|secret)"\s*:\s*")([^"]*)(")'
)
_FIELD_SECRET = re.compile(
    r"(?i)\b(token|access_token|api_key|authorization|password|secret)\s*[=:]\s*(?!bearer\b)\S+"
)
_DATA_URL = re.compile(r"data:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+")
_IMAGES_FIELD = re.compile(r'(?i)("images"\s*:\s*)\[[^\]]*\]')


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Replace credentials in text with a fixed marker."""

    redacted = _BEARER.sub("Bearer [REDACTED]", text)
    redacted = _JSON_SECRET.sub(r"\1[REDACTED]\3", redacted)
    redacted = _FIELD_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _DATA_URL.sub("data:[REDACTED]", redacted)
    redacted = _IMAGES_FIELD.sub(r'\1["[REDACTED]"]', redacted)
    for secret in secrets:
        if len(secret) >= 16 and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
