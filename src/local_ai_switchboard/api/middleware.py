"""Request ids for logs. Authorization headers are never written to the log."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from local_ai_switchboard.services.logging import request_id_var

logger = logging.getLogger("local_ai_switchboard.api")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and log the method, path, and status code."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get("x-request-id", "").strip()
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request failed")
            raise
        else:
            response.headers["X-Request-ID"] = request_id
            logger.info("%s %s %s", request.method, request.url.path, response.status_code)
            return response
        finally:
            request_id_var.reset(token)
