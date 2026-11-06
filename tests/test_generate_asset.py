"""The macOS client speaks HTTP to a local stand-in for the Gateway."""

from __future__ import annotations

import ast
import importlib.util
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOKEN = "test-token-value-0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\nfake-image"
ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "client" / "macos" / "generate_asset.py"


def _load_client():
    spec = importlib.util.spec_from_file_location("generate_asset", CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RETRY_BACKOFF_SECONDS = (0, 0, 0, 0, 0)
    module.POLL_INTERVAL_SECONDS = 0.01
    module.TIMEOUT_SECONDS = 30
    module.RETRY_ATTEMPTS = 5
    module.POLL_DEADLINE_SECONDS = 5
    return module


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _handle(self) -> None:
        gateway: Gateway = self.server  # type: ignore[assignment]
        if gateway.drop_remaining:
            gateway.drop_remaining -= 1
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        gateway.authorizations.append(self.headers.get("Authorization", ""))
        if self.headers.get("Authorization") != f"Bearer {gateway.token}":
            self._send(401, {"error": {"message": "authentication failed", "type": "auth"}})
            return
        if gateway.unavailable:
            gateway.unavailable -= 1
            self._send(503, {"error": {"message": "busy", "type": "provider_error"}})
            return
        if gateway.leak_token:
            self._send(400, {"error": {"message": f"bad {gateway.token}", "type": "invalid_request_error"}})
            return
        if self.command == "POST" and self.path == "/v1/assets":
            body = json.loads(raw.decode("utf-8"))
            gateway.bodies.append(body)
            self._send(202, {"id": "job-1", "status": "queued", "workflow_id": body["workflow_id"]})
            return
        if self.path == "/v1/jobs/job-1":
            gateway.polls += 1
            state = "queued" if gateway.polls < 2 else "succeeded"
            outputs = [] if state != "succeeded" else [{"filename": "hero.png", "media_type": "image/png"}]
            self._send(
                200,
                {
                    "id": "job-1",
                    "status": state,
                    "workflow_id": "fixture-image",
                    "error": None,
                    "prompt_id": "prompt-1",
                    "outputs": outputs,
                },
            )
            return
        if self.path == "/v1/jobs/job-1/download":
            self._send(200, PNG, "image/png")
            return
        self._send(404, {"error": {"message": "missing", "type": "not_found"}})

    def _send(self, status: int, body: dict[str, object] | bytes, content_type: str = "application/json") -> None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Gateway(ThreadingHTTPServer):
    def __init__(self, token: str) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.token = token
        self.drop_remaining = 0
        self.unavailable = 0
        self.leak_token = False
        self.bodies: list[dict[str, object]] = []
        self.authorizations: list[str] = []
        self.polls = 0

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def asset_client():
    return _load_client()


def _config(path: Path, url: str, token: str = TOKEN) -> None:
    path.write_text(
        json.dumps({"gateway_url": url, "token": token}, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _serve() -> tuple[Gateway, threading.Thread]:
    server = Gateway(TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server: Gateway) -> None:
    server.shutdown()
    server.server_close()


def test_client_uses_the_standard_library_only() -> None:
    source = CLIENT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}
    example = (ROOT / "client" / "macos" / "client.json.example").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "MAC_CLIENT_SETUP.md").read_text(encoding="utf-8")
    assert "gateway_url" in example
    assert "chmod 600" in docs
    assert "macOS Terminal" in docs


def test_client_saves_a_unicode_path_and_does_not_print_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    asset_client,
) -> None:
    server, _thread = _serve()
    config = tmp_path / "client.json"
    _config(config, server.url)
    monkeypatch.setenv("LAS_CLIENT_CONFIG", str(config))
    destination = tmp_path / "My Projects" / "герой" / "hero image.png"
    try:
        code = asset_client.main(
            [
                "--prompt",
                "Premium editorial photograph",
                "--output",
                str(destination),
                "--workflow",
                "fixture-image",
                "--width",
                "768",
                "--height",
                "768",
                "--seed",
                "7",
            ]
        )
    finally:
        _stop(server)
    captured = capsys.readouterr()
    assert code == 0
    assert destination.read_bytes() == PNG
    assert server.bodies[0] == {
        "workflow_id": "fixture-image",
        "prompt": "Premium editorial photograph",
        "width": 768,
        "height": 768,
        "seed": 7,
    }
    assert server.authorizations
    assert all(item == f"Bearer {TOKEN}" for item in server.authorizations)
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "submitted job-1" in captured.out
    assert str(destination) in captured.out


def test_client_retries_dropped_connections_and_unavailable_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    asset_client,
) -> None:
    server, _thread = _serve()
    server.drop_remaining = 1
    server.unavailable = 1
    config = tmp_path / "client.json"
    _config(config, server.url)
    monkeypatch.setenv("LAS_CLIENT_CONFIG", str(config))
    destination = tmp_path / "out.png"
    try:
        code = asset_client.main(
            ["--prompt", "retry me", "--output", str(destination), "--workflow", "fixture-image"]
        )
    finally:
        _stop(server)
    captured = capsys.readouterr()
    assert code == 0
    assert destination.read_bytes() == PNG
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_client_timeout_and_leaked_token_are_not_printed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    asset_client,
) -> None:
    asset_client.TIMEOUT_SECONDS = 0.2
    asset_client.RETRY_ATTEMPTS = 2
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    config = tmp_path / "client.json"
    _config(config, f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("LAS_CLIENT_CONFIG", str(config))
    try:
        timed_out = asset_client.main(
            ["--prompt", "slow", "--output", str(tmp_path / "slow.png"), "--workflow", "fixture-image"]
        )
    finally:
        listener.close()
    captured = capsys.readouterr()
    assert timed_out == 1
    assert "unreachable" in captured.err
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err

    server, _thread = _serve()
    server.leak_token = True
    _config(config, server.url)
    try:
        leaked = asset_client.main(
            ["--prompt", "secret", "--output", str(tmp_path / "nope.png"), "--workflow", "fixture-image"]
        )
    finally:
        _stop(server)
    captured = capsys.readouterr()
    assert leaked == 1
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "[REDACTED]" in captured.err


def test_world_readable_config_is_refused_without_printing_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    asset_client,
) -> None:
    config = tmp_path / "client.json"
    _config(config, "http://127.0.0.1:9")
    config.chmod(0o644)
    monkeypatch.setenv("LAS_CLIENT_CONFIG", str(config))
    code = asset_client.main(
        ["--prompt", "hidden", "--output", str(tmp_path / "hidden.png"), "--workflow", "fixture-image"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "readable only by the current user" in captured.err
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
