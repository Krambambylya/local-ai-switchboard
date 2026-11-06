#!/usr/bin/env python3
"""Save a Gateway image into a local project directory.

The script uses the Python standard library. Configuration is the Gateway URL in
~/.config/local-ai-switchboard/config.json. The Gateway does not use credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "local-ai-switchboard" / "config.json"
_SECRET_KEYS = frozenset({"token", "api_key", "authorization", "password", "secret"})
TIMEOUT_SECONDS = 30.0
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0)
POLL_INTERVAL_SECONDS = 0.5
POLL_DEADLINE_SECONDS = 300.0
RETRY_STATUSES = frozenset({429, 502, 503, 504})
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class ClientError(Exception):
    """The client could not complete the request."""


class JobFailed(ClientError):
    """The gateway finished the job without an image."""


def config_path() -> Path:
    """Return the user configuration path, or LAS_CLIENT_CONFIG when it is set."""

    override = os.environ.get("LAS_CLIENT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG


def load_config(path: Path) -> dict[str, str]:
    """Read the gateway URL. Refuse a config that stores a credential."""

    if not path.is_file():
        raise ClientError(f"client configuration was not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClientError("client configuration is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClientError("client configuration must be a JSON object")
    present = _SECRET_KEYS.intersection(payload)
    if present:
        names = ", ".join(sorted(present))
        raise ClientError(f"client configuration must not contain credentials: {names}")
    url = payload.get("gateway_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ClientError("gateway_url must be an http or https URL")
    remainder = url.split("://", 1)[1]
    if "@" in remainder:
        raise ClientError("gateway_url must not contain credentials")
    return {"gateway_url": url.rstrip("/")}


def generate(
    config: dict[str, str],
    *,
    prompt: str,
    output: Path,
    workflow: str,
    width: int | None = None,
    height: int | None = None,
    seed: int | None = None,
) -> Path:
    """Submit one image job, poll it, and save the downloaded file."""

    if not prompt.strip():
        raise ClientError("prompt is required")
    if not workflow.strip():
        raise ClientError("workflow is required")
    destination = Path(output)
    if destination.exists() and destination.is_dir():
        raise ClientError("output path is a directory")
    body: dict[str, object] = {"workflow_id": workflow, "prompt": prompt}
    if width is not None:
        body["width"] = width
    if height is not None:
        body["height"] = height
    if seed is not None:
        body["seed"] = seed
    status, payload = _exchange(config, "POST", "/v1/assets", body)
    created = _json_object(status, payload, {202})
    job_id = created.get("id")
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ClientError("gateway returned an invalid job id")
    print(f"submitted {job_id}", flush=True)
    _poll(config, job_id)
    image = _download(config, job_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    partial.write_bytes(image)
    os.replace(partial, destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    """Run the command line client."""

    parser = argparse.ArgumentParser(description="Download an image from the local AI gateway.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    try:
        config = load_config(config_path())
        saved = generate(
            config,
            prompt=args.prompt,
            output=Path(args.output),
            workflow=args.workflow,
            width=args.width,
            height=args.height,
            seed=args.seed,
        )
    except JobFailed as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"saved {saved}", flush=True)
    return 0


def _poll(config: dict[str, str], job_id: str) -> str:
    deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    while True:
        status, payload = _exchange(config, "GET", f"/v1/jobs/{job_id}")
        body = _json_object(status, payload, {200})
        state = body.get("status")
        if not isinstance(state, str):
            raise ClientError("gateway returned a job without a status")
        print(f"status {state}", flush=True)
        if state == "succeeded":
            return state
        if state in _TERMINAL:
            message = body.get("error")
            detail = message if isinstance(message, str) and message else state
            raise JobFailed(detail)
        if time.monotonic() >= deadline:
            raise ClientError("timed out waiting for the image")
        time.sleep(POLL_INTERVAL_SECONDS)


def _download(config: dict[str, str], job_id: str) -> bytes:
    status, payload = _exchange(config, "GET", f"/v1/jobs/{job_id}/download")
    if status != 200 or not payload:
        raise ClientError("gateway did not return the image")
    return payload


def _exchange(
    config: dict[str, str],
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    last_error = "gateway is unreachable"
    for attempt in range(RETRY_ATTEMPTS):
        try:
            status, payload = _open(config, method, path, body)
        except (urllib.error.URLError, TimeoutError, OSError):
            last_error = "gateway is unreachable"
            if not _pause(attempt):
                break
            continue
        if status in RETRY_STATUSES:
            last_error = f"gateway returned HTTP {status}"
            if not _pause(attempt):
                break
            continue
        return status, payload
    raise ClientError(last_error)


def _pause(attempt: int) -> bool:
    if attempt + 1 >= RETRY_ATTEMPTS:
        return False
    delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
    time.sleep(delay)
    return True


def _open(
    config: dict[str, str],
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(config["gateway_url"] + path, data=data, method=method)
    request.add_header("Accept", "application/json, image/*")
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json_object(status: int, payload: bytes, allowed: set[int]) -> dict[str, object]:
    if status not in allowed:
        message = _error_message(payload) or f"gateway returned HTTP {status}"
        raise ClientError(message)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClientError("gateway returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ClientError("gateway returned invalid JSON")
    return parsed


def _error_message(payload: bytes) -> str | None:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return None


if __name__ == "__main__":
    sys.exit(main())
