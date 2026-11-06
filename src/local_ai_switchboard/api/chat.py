"""OpenAI-compatible model list and chat completions.

Chat execution goes through the GPU scheduler. This module does not call Ollama.
Model listing is a read of the local catalog and does not take the GPU lease.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from local_ai_switchboard.providers.base import ProviderError, ProviderKind
from local_ai_switchboard.providers.ollama import (
    ChatExchange,
    OllamaProvider,
    chat_rejection,
    error_payload,
)
from local_ai_switchboard.scheduler.coordinator import GpuScheduler, ScheduledJob
from local_ai_switchboard.scheduler.model import JobResult, QueueFull, SchedulerUnavailable

logger = logging.getLogger("local_ai_switchboard.api")
router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
_POLL_SECONDS = 0.05


class DisconnectProbe(Protocol):
    """The slice of a request used to notice that the client has gone away."""

    async def is_disconnected(self) -> bool:
        """True when the client has disconnected."""


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    """List models installed in local Ollama."""

    provider = _ollama(request)
    if provider is None:
        return _provider_error("Ollama is not connected")
    try:
        payload = await provider.list_models()
    except ProviderError:
        return _provider_error("Ollama is not reachable")
    return JSONResponse(payload)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Queue one chat completion on the GPU scheduler and stream the upstream bytes."""

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            error_payload("Request body must be a JSON object", "invalid_request_error"),
            status_code=400,
        )
    rejection = chat_rejection(body)
    if rejection is not None:
        return JSONResponse(error_payload(rejection, "invalid_request_error"), status_code=400)
    scheduler: GpuScheduler = request.app.state.scheduler
    exchange = ChatExchange(body)
    try:
        job = await scheduler.submit(ProviderKind.LLM, payload=exchange)
    except QueueFull:
        return JSONResponse(error_payload("GPU queue is full", "rate_limit_error"), status_code=429)
    except SchedulerUnavailable:
        return _provider_error("GPU scheduler is unavailable")
    try:
        failure = await _wait_ready(request, job, exchange)
    except asyncio.CancelledError:
        await _cancel_job(scheduler, job)
        raise
    if failure == "disconnect":
        await _cancel_job(scheduler, job)
        return JSONResponse(error_payload("Client disconnected", "cancelled"), status_code=499)
    if isinstance(failure, JobResult):
        return _provider_error(failure.error or "LLM request failed")
    if exchange.body.get("stream") is True:
        return StreamingResponse(
            _stream(request, scheduler, job, exchange),
            status_code=exchange.status_code,
            media_type=exchange.media_type,
            headers=_SSE_HEADERS,
        )
    try:
        payload, stopped = await _collect(request, job, exchange)
    except asyncio.CancelledError:
        await _cancel_job(scheduler, job)
        raise
    if stopped:
        await _cancel_job(scheduler, job)
        return JSONResponse(error_payload("Client disconnected", "cancelled"), status_code=499)
    if not job.done.done():
        await job.done
    return Response(payload, status_code=exchange.status_code, media_type=exchange.media_type)


def _ollama(request: Request) -> OllamaProvider | None:
    provider = getattr(request.app.state, "ollama_provider", None)
    if isinstance(provider, OllamaProvider):
        return provider
    return None


def _provider_error(message: str) -> JSONResponse:
    return JSONResponse(error_payload(message, "provider_error"), status_code=503)


async def _wait_ready(
    request: DisconnectProbe,
    job: ScheduledJob,
    exchange: ChatExchange,
) -> JobResult | str | None:
    """Wait until Ollama headers are available, the job fails, or the client leaves."""

    while not exchange.ready.is_set():
        if await _disconnected(request):
            return "disconnect"
        if job.done.done():
            return job.done.result()
        ready_task = asyncio.create_task(exchange.ready.wait())
        await asyncio.wait(
            {ready_task, job.done},
            timeout=_POLL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        await _drain(ready_task)
    return None


async def _collect(
    request: DisconnectProbe,
    job: ScheduledJob,
    exchange: ChatExchange,
) -> tuple[bytes, bool]:
    parts: list[bytes] = []
    while True:
        item = await _next_chunk(request, job, exchange)
        if item is None:
            return b"".join(parts), False
        if item is _DISCONNECT:
            return b"".join(parts), True
        parts.append(item)


async def _stream(
    request: DisconnectProbe,
    scheduler: GpuScheduler,
    job: ScheduledJob,
    exchange: ChatExchange,
) -> AsyncIterator[bytes]:
    """Yield upstream SSE bytes. Disconnect or cancellation closes the upstream."""

    finished = False
    try:
        while True:
            item = await _next_chunk(request, job, exchange)
            if item is None:
                finished = True
                break
            if item is _DISCONNECT:
                break
            yield item
    finally:
        if not finished:
            await _cancel_job(scheduler, job)
        elif not job.done.done():
            await job.done


class _Disconnect:
    """Sentinel for a client that left before the upstream finished."""


_DISCONNECT = _Disconnect()


async def _next_chunk(
    request: DisconnectProbe,
    job: ScheduledJob,
    exchange: ChatExchange,
) -> bytes | _Disconnect | None:
    while True:
        if await _disconnected(request):
            return _DISCONNECT
        if job.done.done() and exchange.chunks.empty():
            return None
        get_task = asyncio.create_task(exchange.chunks.get())
        finished, _pending = await asyncio.wait(
            {get_task, job.done},
            timeout=_POLL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if get_task in finished:
            return get_task.result()
        await _drain(get_task)
        if job.done.done() and exchange.chunks.empty():
            return None


async def _disconnected(request: DisconnectProbe) -> bool:
    try:
        return await request.is_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("disconnect probe failed")
        return False


async def _cancel_job(scheduler: GpuScheduler, job: ScheduledJob) -> None:
    if job.done.done():
        return
    await scheduler.cancel(job.id)
    if not job.done.done():
        await job.done


async def _drain(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
