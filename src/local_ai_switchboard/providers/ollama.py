"""Ollama adapter. The gateway never exposes this as a generic reverse proxy.

Chat bytes are forwarded only after the scheduler grants the LLM lease.
Unload uses native POST /api/chat with empty messages and keep_alive 0.
GET /api/ps is the residency check. A 200 from unload is not proof.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Callable, Mapping
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
    prepare() runs on the worker immediately before the request is sent, so a
    later stage can attach a file that did not exist when the sequence was queued.
    It must not call the scheduler.
    """

    def __init__(
        self,
        body: Mapping[str, Any],
        prepare: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.body = dict(body)
        self.prepare = prepare
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
            if exchange.prepare is not None:
                prepared = exchange.prepare()
                if not isinstance(prepared, dict):
                    raise ProviderError("chat request could not be prepared")
                exchange.body = dict(prepared)
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
                if response.status_code == 400 and _body_has_image(exchange.body):
                    payload = await response.aread()
                    await self._close_upstream()
                    if _native_vision_rejected(payload):
                        logger.info(
                            "ollama vision fallback model=%s images=%s",
                            exchange.body.get("model"),
                            _image_count(exchange.body),
                        )
                        await self._forward_native_chat(job, exchange)
                        return
                    exchange.open(response.status_code, "application/json")
                    if payload and not job.cancel_requested:
                        await exchange.chunks.put(payload)
                    return
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

    async def _forward_native_chat(self, job: JobContext, exchange: ChatExchange) -> None:
        """Send image parts through Ollama's native chat API and return an OpenAI body.

        This runs only after /v1/chat/completions rejects a content array.
        The images field is not logged.
        """

        try:
            native = native_chat_body(exchange.body)
        except ValueError as exc:
            raise ProviderError("image input could not be sent to Ollama") from exc
        if job.cancel_requested:
            return
        response = await self._request("POST", "/api/chat", native)
        if job.cancel_requested:
            return
        if response.status_code != 200:
            raise ProviderError(
                "Ollama chat request failed",
                diagnostics={"status": str(response.status_code)},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Ollama chat request failed") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Ollama chat request failed")
        await self._reply(exchange, 200, openai_from_native(payload))

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


def build_chat_body(
    model: str,
    messages: list[dict[str, Any]],
    *,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Build an OpenAI chat body. Image parts use a base64 data URL, never a remote URL."""

    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def user_content(text: str, images: list[tuple[str, bytes]] | None = None) -> dict[str, Any]:
    """One user turn. Without images the content stays a string."""

    if not images:
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for media_type, data in images:
        encoded = base64.b64encode(data).decode("ascii")
        parts.append({"type": "image_url", "image_url": f"data:{media_type};base64,{encoded}"})
    return {"role": "user", "content": parts}


def native_chat_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI content array into Ollama's native messages[].images form."""

    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages are missing")
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("message is not an object")
        role = message.get("role")
        if not isinstance(role, str):
            raise ValueError("message role is missing")
        content = message.get("content")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("message content is not supported")
        texts: list[str] = []
        images: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
                continue
            url = _image_url(part)
            if url is None:
                continue
            bare = _bare_base64(url)
            if bare is None:
                raise ValueError("image part is not a base64 data URL")
            images.append(bare)
        item: dict[str, Any] = {"role": role, "content": "\n".join(texts)}
        if images:
            item["images"] = images
        converted.append(item)
    native: dict[str, Any] = {"model": body.get("model"), "messages": converted, "stream": False}
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        native["format"] = "json"
    return native


def openai_from_native(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map a native chat response onto the OpenAI completion shape the gateway already returns."""

    message = payload.get("message")
    content = ""
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        content = message["content"]
    return {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _body_has_image(body: Mapping[str, Any]) -> bool:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    return _image_count(body) > 0


def _image_count(body: Mapping[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if isinstance(part, dict) and _image_url(part) is not None:
                count += 1
    return count


def _native_vision_rejected(payload: bytes) -> bool:
    text = payload.decode("utf-8", "replace").lower()
    return "cannot unmarshal array" in text


def _bare_base64(url: str) -> str | None:
    stripped = url.strip()
    if not stripped.lower().startswith("data:") or "base64," not in stripped.lower():
        return None
    payload = stripped.split("base64,", 1)[1].strip()
    if not payload or len(payload) > 20_000_000:
        return None
    return payload


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
