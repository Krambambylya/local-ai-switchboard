"""Ollama adapter. The gateway never exposes this as a generic reverse proxy.

Chat bytes are forwarded only after the scheduler grants the LLM lease.
Unload uses native POST /api/chat with empty messages and keep_alive 0.
GET /api/ps is the residency check. A 200 from unload is not proof.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from local_ai_switchboard.providers.base import JobContext, ProviderError, ProviderKind

logger = logging.getLogger("local_ai_switchboard.ollama")

_ALLOWED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "tools",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "response_format",
    }
)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ChatExchange:
    """Bridge between a chat route and the provider while the lease is held.

    The route reads chunks. The provider writes them and closes with None.
    """

    def __init__(self, body: Mapping[str, Any]) -> None:
        self.body = dict(body)
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.status_code = 200
        self.media_type = "application/json"

    def open(self, status_code: int, media_type: str) -> None:
        self.status_code = status_code
        self.media_type = media_type
        if not self.ready.is_set():
            self.ready.set()


def error_payload(message: str, error_type: str) -> dict[str, Any]:
    """OpenAI-shaped error object. The message must not include secrets or prompts."""

    return {"error": {"message": message, "type": error_type}}


def chat_rejection(body: object) -> str | None:
    """Return a client error when the body is not a supported chat request.

    Unsupported fields are named and left in place for the caller to reject.
    They are not removed.
    """

    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    unsupported = sorted(key for key in body if key not in _ALLOWED_FIELDS)
    if unsupported:
        names = ", ".join(unsupported)
        return f"Unsupported fields: {names}"
    model = body.get("model")
    if not isinstance(model, str) or _MODEL_NAME.fullmatch(model) is None:
        return "model must be a local Ollama model name"
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty list"
    for message in messages:
        if not isinstance(message, dict):
            return "each message must be an object"
    if "stream" in body and not isinstance(body["stream"], bool):
        return "stream must be a boolean"
    if "tools" in body and not isinstance(body["tools"], list):
        return "tools must be a list"
    if _has_remote_image(messages):
        return "Remote image URLs are not supported; send a base64 data URL"
    return None


class OllamaProvider:
    """Localhost Ollama. One instance is shared by the scheduler and model listing."""

    kind = ProviderKind.LLM

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._client = client
        self._owned = client is None
        self._busy = False
        self._upstream: httpx.Response | None = None
        self._resident_model: str | None = None
        self.prepare_calls = 0
        self.chat_calls = 0

    async def aclose(self) -> None:
        """Close the HTTP client when this provider created it."""

        await self._close_upstream()
        client = self._client
        self._client = None
        if client is not None and self._owned:
            await client.aclose()

    async def health(self) -> bool:
        """True when GET /api/version returns a version string."""

        try:
            payload = await self._get_json("/api/version")
        except ProviderError:
            return False
        version = payload.get("version")
        return isinstance(version, str) and bool(version.strip())

    async def list_models(self) -> dict[str, Any]:
        """Return local models in the OpenAI list shape. This does not load a model."""

        payload = await self._get_json("/api/tags")
        models = payload.get("models")
        data: list[dict[str, Any]] = []
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("model")
                if not isinstance(name, str) or not name:
                    continue
                data.append(
                    {
                        "id": name,
                        "object": "model",
                        "created": _created(item.get("modified_at")),
                        "owned_by": "ollama",
                    }
                )
        return {"object": "list", "data": data}

    async def prepare(self) -> None:
        """Mark Ollama eligible to run. A down daemon fails the job inside run().

        Raising here would latch the scheduler in ERROR with no recover route.
        The model itself loads when the chat request reaches Ollama.
        """

        self.prepare_calls += 1

    async def run(self, job: JobContext) -> None:
        """Forward one chat completion. Return when the upstream body ends."""

        exchange = getattr(job, "payload", None)
        if not isinstance(exchange, ChatExchange):
            raise ProviderError("LLM job is missing a chat request")
        self._busy = True
        try:
            rejection = chat_rejection(exchange.body)
            if rejection is not None:
                await self._reply(exchange, 400, error_payload(rejection, "invalid_request_error"))
                return
            if job.cancel_requested:
                return
            if not await self.health():
                raise ProviderError("Ollama is not reachable")
            if job.cancel_requested:
                return
            model = str(exchange.body["model"])
            if not await self._model_is_local(model):
                await self._reply(
                    exchange,
                    404,
                    error_payload(f"Model {model} is not available", "not_found"),
                )
                return
            if job.cancel_requested:
                return
            self._resident_model = model
            self.chat_calls += 1
            await self._forward_chat(job, exchange)
        except asyncio.CancelledError:
            await self._close_upstream()
            raise
        finally:
            self._busy = False
            if exchange.ready.is_set():
                await asyncio.shield(exchange.chunks.put(None))

    async def cancel(self, job: JobContext) -> None:
        """Close the upstream stream. Safe to call more than once."""

        del job
        await self._close_upstream()

    async def release_resources(self) -> None:
        """Ask Ollama to unload resident models. The caller must still poll /api/ps."""

        names = set(await self._running_models())
        if self._resident_model:
            names.add(self._resident_model)
        for name in sorted(names):
            response = await self._request(
                "POST",
                "/api/chat",
                {"model": name, "messages": [], "keep_alive": 0},
            )
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                raise ProviderError(
                    "Ollama unload failed",
                    diagnostics={"status": str(response.status_code)},
                )
            logger.info("ollama unload requested model=%s", name)

    async def resources_released(self) -> bool:
        """True only when /api/ps lists no models. Errors stay resident."""

        try:
            names = await self._running_models()
        except ProviderError:
            return False
        return not names

    async def occupancy(self) -> str:
        """Inspect /api/ps. A closed port is absent. A bad response is unknown."""

        try:
            response = await self._http().request("GET", f"{self._base_url}/api/ps")
        except httpx.HTTPError:
            return "absent"
        if response.status_code != 200:
            return "unknown"
        try:
            payload = response.json()
        except ValueError:
            return "unknown"
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            return "unknown"
        return "occupied" if payload["models"] else "clear"

    async def is_busy(self) -> bool:
        return self._busy

    async def _forward_chat(self, job: JobContext, exchange: ChatExchange) -> None:
        """Publish upstream headers, then copy the body until it ends or the job is cancelled."""

        client = self._http()
        url = f"{self._base_url}/v1/chat/completions"
        stream = exchange.body.get("stream") is True
        logger.info("ollama chat model=%s stream=%s", exchange.body.get("model"), stream)
        try:
            async with client.stream("POST", url, json=exchange.body) as response:
                self._upstream = response
                if not stream:
                    payload = await response.aread()
                    if job.cancel_requested:
                        return
                    exchange.open(
                        response.status_code,
                        _media_type(response.headers.get("content-type"), False),
                    )
                    if payload:
                        await exchange.chunks.put(payload)
                    return
                exchange.open(
                    response.status_code,
                    _media_type(response.headers.get("content-type"), True),
                )
                async for chunk in response.aiter_bytes():
                    if job.cancel_requested:
                        break
                    if chunk:
                        await exchange.chunks.put(chunk)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            await self._close_upstream()
            if job.cancel_requested:
                return
            logger.warning("ollama chat stream failed: %s", type(exc).__name__)
            raise ProviderError("Ollama chat request failed") from exc
        else:
            await self._close_upstream()

    async def _reply(self, exchange: ChatExchange, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        exchange.open(status_code, "application/json")
        await exchange.chunks.put(body)

    async def _model_is_local(self, model: str) -> bool:
        payload = await self._get_json("/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProviderError("Ollama model list failed")
        for item in models:
            if not isinstance(item, dict):
                continue
            if item.get("name") == model or item.get("model") == model:
                return True
        return False

    async def _running_models(self) -> list[str]:
        payload = await self._get_json("/api/ps")
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProviderError("Ollama process status failed")
        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            for key in ("model", "name"):
                value = item.get(key)
                if isinstance(value, str) and value and value not in names:
                    names.append(value)
        return names

    async def _get_json(self, path: str) -> Mapping[str, Any]:
        response = await self._request("GET", path, None)
        if response.status_code != 200:
            raise ProviderError(
                "Ollama request failed",
                diagnostics={"path": path, "status": str(response.status_code)},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Ollama request failed", diagnostics={"path": path}) from exc
        if not isinstance(payload, dict):
            raise ProviderError("Ollama request failed", diagnostics={"path": path})
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
    ) -> httpx.Response:
        try:
            response = await self._http().request(method, f"{self._base_url}{path}", json=body)
        except httpx.HTTPError as exc:
            logger.warning("ollama %s %s failed: %s", method, path, type(exc).__name__)
            raise ProviderError("Ollama is not reachable") from exc
        return response

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
            )
            self._owned = True
        return self._client

    async def _close_upstream(self) -> None:
        response = self._upstream
        self._upstream = None
        if response is not None and not response.is_closed:
            await response.aclose()


def _has_remote_image(messages: list[object]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            url = _image_url(part)
            if url is None:
                continue
            if not url.strip().lower().startswith("data:"):
                return True
    return False


def _image_url(part: Mapping[str, Any]) -> str | None:
    image = part.get("image_url")
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        url = image.get("url")
        return url if isinstance(url, str) else ""
    if part.get("type") == "image_url":
        return ""
    return None


def _created(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp())


def _media_type(header: str | None, stream: bool) -> str:
    if header:
        media_type = header.split(";", 1)[0].strip().lower()
        if media_type:
            return media_type
    if stream:
        return "text/event-stream"
    return "application/json"
