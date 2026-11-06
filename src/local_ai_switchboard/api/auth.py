"""Bearer authentication for every route except the health checks."""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from local_ai_switchboard.providers.ollama import error_payload


def _matches(provided: str, expected: str) -> bool:
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


def authorized(request: Request) -> bool:
    """True when the Authorization header carries the configured bearer token."""

    expected = request.app.state.settings.access_token()
    if expected is None:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token.strip():
        return False
    return _matches(token.strip(), expected)


def unauthorized() -> JSONResponse:
    """Authentication failure. The body does not echo the token."""

    return JSONResponse(
        error_payload("Unauthorized", "authentication_error"),
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
