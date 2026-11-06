"""Ollama adapter tests. The HTTP transport is mocked. No daemon is contacted."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterator
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from local_ai_switchboard.api.chat import _stream
from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.nvidia_smi import GpuDevice, GpuSnapshot
from local_ai_switchboard.main import create_app
from local_ai_switchboard.providers.base import ProviderKind
from local_ai_switchboard.providers.ollama import ChatExchange, OllamaProvider
from local_ai_switchboard.providers.unavailable import UnavailableProvider
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import (
    JobStatus,
    SchedulerConfig,
    SchedulerState,
    SchedulerUnavailable,
)
from local_ai_switchboard.services.logging import flush_logs

MODEL = "qwen3.5:4b"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Find a thing",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]
SSE = (
    b'data: {"choices":[{"delta":{"content":"Hi","reasoning_content":"thinking"}}]}\n\n'
    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
    b'"function":{"name":"lookup","arguments":"{\\"q\\":\\"x\\"}"}}]}}]}\n\n'
    b"data: [DONE]\n\n"
)
NON_STREAM = json.dumps(
    {
        "id": "chatcmpl-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "thinking",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
).encode()


class GateStream(httpx.AsyncByteStream):
    """Yields one SSE chunk, then waits until the test releases or cancels it."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self._sent = False
        self.released = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        if not self._sent:
            self._sent = True
            yield self._first
        await self.released.wait()

    async def aclose(self) -> None:
        self.closed.set()
        self.released.set()


class OllamaServer:
    """Scripted localhost Ollama."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.completion_bodies: list[dict[str, object]] = []
        self.unloads: list[dict[str, object]] = []
        self.running: list[dict[str, object]] = []
        self.pending_streams: list[httpx.AsyncByteStream] = []
        self.completion = NON_STREAM
        self.fail_version = False
        self.stick = False
        self.models: list[dict[str, object]] = [
            {
                "name": MODEL,
                "model": MODEL,
                "modified_at": "2026-01-02T03:04:05Z",
                "digest": "should-not-leak",
                "size": 4000000000,
            }
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/version":
            if self.fail_version:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, json={"version": "0.9.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": self.models})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": list(self.running)})
        if path == "/api/chat":
            assert isinstance(body, dict)
            self.unloads.append(body)
            if not self.stick:
                name = body.get("model")
                self.running = [
                    item
                    for item in self.running
                    if item.get("name") != name and item.get("model") != name
                ]
            return httpx.Response(
                200,
                json={"model": body.get("model"), "done": True, "done_reason": "unload"},
            )
        if path == "/v1/chat/completions":
            assert isinstance(body, dict)
            self.completion_bodies.append(body)
            model = str(body["model"])
            self.running = [{"name": model, "model": model, "size_vram": 3_500_000_000}]
            if body.get("stream") is True:
                headers = {"content-type": "text/event-stream"}
                if self.pending_streams:
                    return httpx.Response(200, stream=self.pending_streams.pop(0), headers=headers)
                return httpx.Response(200, content=self.completion, headers=headers)
            return httpx.Response(
                200,
                content=self.completion,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, json={"error": "unexpected path"})


class Headroom:
    def __init__(self, free_mb: int = 7000) -> None:
        self.free_mb = free_mb
        self.calls = 0

    async def snapshot(self) -> GpuSnapshot:
        self.calls += 1
        device = GpuDevice(
            index=0,
            name="NVIDIA GeForce RTX 4060",
            memory_total_mb=8188,
            memory_used_mb=max(0, 8188 - self.free_mb),
            memory_free_mb=self.free_mb,
            utilization_gpu=0,
        )
        return GpuSnapshot(True, (device,), ())


class ImageStub:
    kind = ProviderKind.IMAGE

    def __init__(self, monitor: Headroom) -> None:
        self.monitor = monitor
        self.prepare_calls = 0
        self.monitor_calls_at_prepare = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare(self) -> None:
        self.prepare_calls += 1
        self.monitor_calls_at_prepare = self.monitor.calls

    async def run(self, job: object) -> None:
        del job
        self.started.set()
        await self.release.wait()

    async def cancel(self, job: object) -> None:
        del job
        self.release.set()

    async def release_resources(self) -> None:
        return None

    async def resources_released(self) -> bool:
        return True

    async def is_busy(self) -> bool:
        return False


class DisconnectFlag:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _chat(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "UNIQUE-PROMPT-DO-NOT-LOG"}],
    }
    body.update(extra)
    return body


@pytest.fixture
def gateway(settings: Settings, unavailable_gpu: GpuSnapshot) -> Iterator[tuple[TestClient, OllamaServer]]:
    server = OllamaServer()
    ollama = OllamaProvider(
        str(settings.ollama_url),
        transport=httpx.MockTransport(server.handler),
    )
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: ollama,
            ProviderKind.IMAGE: UnavailableProvider(ProviderKind.IMAGE, "ComfyUI"),
        },
    )
    with TestClient(app) as client:
        yield client, server


def test_routes_do_not_call_ollama_directly() -> None:
    from local_ai_switchboard.api import chat as chat_module

    source = inspect.getsource(chat_module)
    assert "httpx" not in source
    assert "/api/chat" not in source
    assert "11434" not in source


def test_gateway_is_not_an_ollama_reverse_proxy(gateway: tuple[TestClient, OllamaServer]) -> None:
    client, server = gateway
    server.calls.clear()
    assert client.get("/api/ps").status_code == 404
    assert client.post("/api/chat", json={"model": MODEL}).status_code == 404
    assert client.post("/v1/completions", json={}).status_code == 404
    assert server.calls == []


def test_model_discovery_does_not_require_authorization(
    gateway: tuple[TestClient, OllamaServer],
) -> None:
    client, server = gateway
    response = client.get("/v1/models")
    ignored = client.get("/v1/models", headers={"Authorization": "Bearer not-a-credential"})
    assert response.status_code == 200
    assert ignored.status_code == 200
    assert ignored.json() == response.json()
    assert "not-a-credential" not in response.text
    assert "should-not-leak" not in response.text
    assert "4000000000" not in response.text
    created = int(datetime.fromisoformat("2026-01-02T03:04:05+00:00").timestamp())
    assert response.json() == {
        "object": "list",
        "data": [{"id": MODEL, "object": "model", "created": created, "owned_by": "ollama"}],
    }
    assert ("GET", "/api/tags") in server.calls
    assert ("GET", "/v1/models") not in server.calls


def test_chat_does_not_require_authorization(gateway: tuple[TestClient, OllamaServer]) -> None:
    client, server = gateway
    response = client.post("/v1/chat/completions", json=_chat())
    ignored = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-credential"},
        json=_chat(),
    )
    assert response.status_code == 200
    assert ignored.status_code == 200
    assert server.calls


def test_unsupported_fields_are_named_and_not_forwarded(
    gateway: tuple[TestClient, OllamaServer],
) -> None:
    client, server = gateway
    server.calls.clear()
    response = client.post(
        "/v1/chat/completions",
        json=_chat(tool_choice="auto", n=1, logit_bias={"1": 1}, user="alice", keep_alive=0),
    )
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    for name in ("keep_alive", "logit_bias", "n", "tool_choice", "user"):
        assert name in message
    assert server.calls == []


def test_remote_image_is_rejected_and_base64_is_forwarded(
    gateway: tuple[TestClient, OllamaServer],
) -> None:
    client, server = gateway
    remote = client.post(
        "/v1/chat/completions",
        json=_chat(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}
                    ],
                }
            ]
        ),
    )
    assert remote.status_code == 400
    assert "base64" in remote.json()["error"]["message"]
    assert server.completion_bodies == []

    data_url = "data:image/png;base64,aaaa"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    accepted = client.post(
        "/v1/chat/completions",
        json=_chat(messages=messages),
    )
    assert accepted.status_code == 200
    assert server.completion_bodies[-1]["messages"] == messages


def test_streaming_preserves_sse_tool_calls_and_reasoning(
    gateway: tuple[TestClient, OllamaServer],
    settings: Settings,
) -> None:
    client, server = gateway
    server.completion = SSE
    response = client.post(
        "/v1/chat/completions",
        json=_chat(stream=True, tools=TOOLS),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == SSE
    assert server.completion_bodies[-1]["tools"] == TOOLS
    assert "keep_alive" not in server.completion_bodies[-1]
    flush_logs()
    text = (settings.resolved_log_dir() / "switchboard.log").read_text(encoding="utf-8")
    assert "UNIQUE-PROMPT-DO-NOT-LOG" not in text


def test_non_streaming_preserves_tool_calls(gateway: tuple[TestClient, OllamaServer]) -> None:
    client, server = gateway
    response = client.post(
        "/v1/chat/completions",
        json=_chat(tools=TOOLS),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == NON_STREAM
    tool_call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_1"
    assert response.json()["choices"][0]["message"]["reasoning_content"] == "thinking"
    assert server.completion_bodies[-1]["tools"] == TOOLS


def test_unreachable_ollama_fails_the_job_and_stays_retryable(
    gateway: tuple[TestClient, OllamaServer],
) -> None:
    client, server = gateway
    server.fail_version = True
    failed = client.post("/v1/chat/completions", json=_chat())
    assert failed.status_code == 503
    assert failed.json()["error"]["message"] == "Ollama is not reachable"
    assert server.completion_bodies == []
    server.fail_version = False
    recovered = client.post("/v1/chat/completions", json=_chat())
    assert recovered.status_code == 200


def test_missing_model_does_not_start_a_completion(
    gateway: tuple[TestClient, OllamaServer],
) -> None:
    client, server = gateway
    response = client.post(
        "/v1/chat/completions",
        json=_chat(model="missing:model"),
    )
    assert response.status_code == 404
    assert server.completion_bodies == []


def test_second_chat_does_not_unload(gateway: tuple[TestClient, OllamaServer]) -> None:
    client, server = gateway
    provider = client.app.state.ollama_provider
    first = client.post("/v1/chat/completions", json=_chat())
    second = client.post("/v1/chat/completions", json=_chat())
    assert first.status_code == 200
    assert second.status_code == 200
    assert provider.prepare_calls == 1
    assert provider.chat_calls == 2
    assert not any(path == "/api/chat" for _method, path in server.calls)


def test_process_exit_sends_native_unload(
    settings: Settings,
    unavailable_gpu: GpuSnapshot,
) -> None:
    server = OllamaServer()
    ollama = OllamaProvider(
        str(settings.ollama_url),
        transport=httpx.MockTransport(server.handler),
    )
    app = create_app(
        settings,
        gpu_probe=lambda: unavailable_gpu,
        providers={
            ProviderKind.LLM: ollama,
            ProviderKind.IMAGE: UnavailableProvider(ProviderKind.IMAGE, "ComfyUI"),
        },
    )
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=_chat()).status_code == 200
        assert server.unloads == []
    assert server.unloads == [{"model": MODEL, "messages": [], "keep_alive": 0}]
    assert any(path == "/api/ps" for _method, path in server.calls)


async def test_streaming_holds_the_lease_until_cancel_and_the_next_job_runs() -> None:
    server = OllamaServer()
    gate = GateStream(b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n')
    server.pending_streams.append(gate)
    server.completion = SSE
    provider = OllamaProvider(
        "http://127.0.0.1:11434",
        transport=httpx.MockTransport(server.handler),
    )
    image = UnavailableProvider(ProviderKind.IMAGE, "ComfyUI")
    scheduler = GpuScheduler(
        config=SchedulerConfig(job_timeout_seconds=5, release_timeout_seconds=0.05),
        providers={ProviderKind.LLM: provider, ProviderKind.IMAGE: image},
        monitor=Headroom(),
    )
    await scheduler.start()
    flag = DisconnectFlag()
    try:
        first = await scheduler.submit(ProviderKind.LLM, payload=ChatExchange(_chat(stream=True)))
        generator = _stream(flag, scheduler, first, first.payload)
        chunk = await asyncio.wait_for(generator.__anext__(), timeout=2)
        assert chunk.startswith(b"data: ")
        status = await scheduler.status()
        assert status.current_job_id == first.id
        assert status.state is SchedulerState.LLM_ACTIVE
        assert provider.chat_calls == 1
        flag.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(generator.__anext__(), timeout=2)
        assert (await asyncio.wait_for(first.done, timeout=2)).status is JobStatus.CANCELLED
        assert gate.closed.is_set()

        follow_up = await scheduler.submit(ProviderKind.LLM, payload=ChatExchange(_chat(stream=True)))
        result = await asyncio.wait_for(follow_up.done, timeout=2)
        assert result.status is JobStatus.SUCCEEDED
        assert provider.chat_calls == 2
    finally:
        gate.released.set()
        await scheduler.stop()
        await provider.aclose()


async def test_loaded_model_blocks_the_next_provider_until_ps_is_empty() -> None:
    server = OllamaServer()
    server.stick = True
    provider = OllamaProvider(
        "http://127.0.0.1:11434",
        transport=httpx.MockTransport(server.handler),
    )
    monitor = Headroom(7000)
    image = ImageStub(monitor)
    scheduler = GpuScheduler(
        config=SchedulerConfig(
            job_timeout_seconds=5,
            release_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers={ProviderKind.LLM: provider, ProviderKind.IMAGE: image},
        monitor=monitor,
    )
    await scheduler.start()
    try:
        chat = await scheduler.submit(ProviderKind.LLM, payload=ChatExchange(_chat()))
        assert (await asyncio.wait_for(chat.done, timeout=2)).status is JobStatus.SUCCEEDED
        assert server.running
        assert await provider.resources_released() is False
        image_job = await scheduler.submit(ProviderKind.IMAGE)
        result = await asyncio.wait_for(image_job.done, timeout=2)
        assert result.status is JobStatus.FAILED
        assert dict(result.diagnostics)["provider_released"] == "false"
        assert image.prepare_calls == 0
        assert server.unloads
        assert server.unloads[0] == {"model": MODEL, "messages": [], "keep_alive": 0}
        unload_at = next(i for i, (_method, path) in enumerate(server.calls) if path == "/api/chat")
        assert any(path == "/api/ps" for _method, path in server.calls[unload_at + 1 :])
        assert (await scheduler.status()).state is SchedulerState.ERROR
    finally:
        image.release.set()
        await scheduler.stop()
        await provider.aclose()


async def test_gpu_headroom_is_required_after_ollama_unload() -> None:
    server = OllamaServer()
    provider = OllamaProvider(
        "http://127.0.0.1:11434",
        transport=httpx.MockTransport(server.handler),
    )
    monitor = Headroom(588)
    image = ImageStub(monitor)
    scheduler = GpuScheduler(
        config=SchedulerConfig(
            job_timeout_seconds=5,
            release_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            gpu_memory_threshold_mb=1024,
        ),
        providers={ProviderKind.LLM: provider, ProviderKind.IMAGE: image},
        monitor=monitor,
    )
    await scheduler.start()
    try:
        chat = await scheduler.submit(ProviderKind.LLM, payload=ChatExchange(_chat()))
        assert (await asyncio.wait_for(chat.done, timeout=2)).status is JobStatus.SUCCEEDED
        image_job = await scheduler.submit(ProviderKind.IMAGE)
        result = await asyncio.wait_for(image_job.done, timeout=2)
        assert result.status is JobStatus.FAILED
        diagnostics = dict(result.diagnostics)
        assert diagnostics["provider_released"] == "true"
        assert "588" in diagnostics["memory"]
        assert image.prepare_calls == 0
        assert server.unloads[0]["keep_alive"] == 0
        assert isinstance(server.unloads[0]["keep_alive"], int)
        with pytest.raises(SchedulerUnavailable):
            await scheduler.submit(ProviderKind.LLM)

        monitor.free_mb = 7000
        await scheduler.recover()
        assert (await scheduler.status()).state is SchedulerState.IDLE
        resumed = await scheduler.submit(ProviderKind.IMAGE)
        await asyncio.wait_for(image.started.wait(), timeout=2)
        assert image.prepare_calls == 1
        assert image.monitor_calls_at_prepare >= 1
        image.release.set()
        assert (await asyncio.wait_for(resumed.done, timeout=2)).status is JobStatus.SUCCEEDED
    finally:
        image.release.set()
        await scheduler.stop()
        await provider.aclose()
