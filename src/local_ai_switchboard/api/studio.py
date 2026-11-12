"""Serve the built Local AI Studio page from the gateway package.

The handler is mounted only under /studio. It does not answer /v1, /health,
or any other API path. A missing build returns a short HTML diagnostic and
does not include a server path.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

router = APIRouter()

_MISSING_STUDIO = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Local AI Studio</title>
</head>
<body>
<p>Local AI Studio is not built. Build the studio frontend and reinstall the gateway package.</p>
</body>
</html>
"""

_MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
_HASHED_ASSET = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_INDEX_CACHE = "no-cache"
_ASSET_CACHE = "public, max-age=31536000, immutable"


def packaged_studio_dir() -> Path:
    """Return the studio build shipped inside this package."""

    return Path(__file__).resolve().parent.parent / "studio_web"


def confined_studio_file(root: Path, relative: str) -> Path | None:
    """Return a file inside root, or None when the path leaves that directory."""

    if not relative or "\x00" in relative or "\\" in relative or ":" in relative:
        return None
    if relative.startswith(("/", ".")):
        return None
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        return None
    root_resolved = root.resolve()
    direct = root_resolved.joinpath(*parts)
    cursor = direct
    while cursor != root_resolved:
        if cursor.is_symlink():
            return None
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent
    candidate = direct.resolve()
    if not candidate.is_file() or not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def _cache_control(name: str) -> str:
    if name == "index.html":
        return _INDEX_CACHE
    stem = Path(name).stem
    token = stem.rsplit("-", 1)[-1]
    if "-" in stem and _HASHED_ASSET.fullmatch(token):
        return _ASSET_CACHE
    return _INDEX_CACHE


def _html(body: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers={"Cache-Control": _INDEX_CACHE})


def _index(index: Path) -> FileResponse:
    return FileResponse(
        index,
        media_type=_MEDIA_TYPES[".html"],
        headers={"Cache-Control": _INDEX_CACHE},
    )


def _asset(path: Path) -> FileResponse:
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": _cache_control(path.name)})


@router.get("/studio", include_in_schema=False)
@router.get("/studio/", include_in_schema=False)
@router.get("/studio/{asset_path:path}", include_in_schema=False)
def studio_page(request: Request, asset_path: str = "") -> Response:
    """Serve the studio shell, a built asset, or a diagnostic when the build is absent."""

    root = Path(request.app.state.studio_dir)
    index = root / "index.html"
    if not index.is_file():
        return _html(_MISSING_STUDIO, 503)
    relative = asset_path.strip("/")
    if not relative or relative == "index.html":
        return _index(index)
    target = confined_studio_file(root, relative)
    if target is not None:
        return _asset(target)
    if Path(relative).suffix:
        return _html("Not found", 404)
    return _index(index)
