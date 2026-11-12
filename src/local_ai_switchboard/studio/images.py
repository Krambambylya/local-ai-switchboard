"""Decode local images. The original bytes are never rewritten."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}


class ImageRejected(ValueError):
    """The upload is not a usable PNG, JPEG, or WebP. The message is safe to return."""


@dataclass(frozen=True)
class PreparedImage:
    """Original upload plus an EXIF-oriented raster and a smaller analysis copy."""

    media_type: str
    suffix: str
    original: bytes
    width: int
    height: int
    oriented_png: bytes
    working_png: bytes
    working_width: int
    working_height: int


def prepare_image(data: bytes, *, max_bytes: int, analysis_edge: int) -> PreparedImage:
    """Validate by decoding. The extension is not trusted."""

    if not data:
        raise ImageRejected("The file is empty.")
    if len(data) > max_bytes:
        raise ImageRejected("The file is larger than the configured limit.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            media = _FORMATS.get(source.format or "")
            if media is None:
                raise ImageRejected("The file is not a PNG, JPEG, or WebP image.")
            oriented = ImageOps.exif_transpose(source)
            frame = (oriented or source).copy()
    except ImageRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected("The file could not be decoded as an image.") from exc
    width, height = frame.size
    if width < 1 or height < 1:
        raise ImageRejected("The image has no pixels.")
    oriented_png, _, _ = png_bytes(frame)
    working, working_width, working_height = png_bytes(frame, edge=analysis_edge)
    return PreparedImage(
        media_type=media[0],
        suffix=media[1],
        original=data,
        width=width,
        height=height,
        oriented_png=oriented_png,
        working_png=working,
        working_width=working_width,
        working_height=working_height,
    )


def png_bytes(image: Image.Image, *, edge: int | None = None) -> tuple[bytes, int, int]:
    """Encode a PNG. A maximum edge only shrinks; it is not an upscaler."""

    frame = image.copy()
    if edge is not None:
        frame.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    if frame.mode not in {"RGB", "RGBA"}:
        frame = frame.convert("RGBA" if "A" in frame.mode else "RGB")
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    return buffer.getvalue(), frame.size[0], frame.size[1]


def open_png(data: bytes) -> Image.Image:
    """Open a PNG this process wrote. Callers own the returned image."""

    with Image.open(io.BytesIO(data)) as source:
        return source.convert("RGBA" if source.mode == "RGBA" else "RGB")


def crop_normalized(
    image: Image.Image,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
) -> Image.Image:
    """Crop using normalized coordinates. The source image is not saved over."""

    pixel_width, pixel_height = image.size
    left = _edge(x, pixel_width)
    top = _edge(y, pixel_height)
    right = _edge(x + width, pixel_width)
    bottom = _edge(y + height, pixel_height)
    right = max(left + 1, min(pixel_width, right))
    bottom = max(top + 1, min(pixel_height, bottom))
    return image.crop((left, top, right, bottom))


def _edge(value: float, limit: int) -> int:
    return max(0, min(limit, round(value * limit)))
