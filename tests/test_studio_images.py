"""Image intake checks. Extensions are not trusted."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from local_ai_switchboard.studio.images import ImageRejected, crop_normalized, prepare_image


def _png(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 200, 120)).save(buffer, format="WEBP")
    return buffer.getvalue()


def test_prepare_accepts_png_jpeg_webp() -> None:
    for data, media in ((_png(), "image/png"), (_jpeg(), "image/jpeg"), (_webp(), "image/webp")):
        prepared = prepare_image(data, max_bytes=1024 * 1024, analysis_edge=32)
        assert prepared.media_type == media
        assert prepared.working_width <= 32
        assert prepared.working_height <= 32
        assert prepared.original == data


def test_prepare_rejects_empty_large_and_corrupt() -> None:
    with pytest.raises(ImageRejected, match="empty"):
        prepare_image(b"", max_bytes=100, analysis_edge=32)
    with pytest.raises(ImageRejected, match="larger"):
        prepare_image(_png(), max_bytes=10, analysis_edge=32)
    with pytest.raises(ImageRejected, match="decoded"):
        prepare_image(b"not-an-image", max_bytes=1000, analysis_edge=32)


def test_crop_keeps_source_intact() -> None:
    image = Image.open(io.BytesIO(_png())).convert("RGB")
    before = list(image.get_flattened_data())
    cropped = crop_normalized(image, x=0.1, y=0.1, width=0.5, height=0.5)
    assert cropped.size[0] > 0
    assert list(image.get_flattened_data()) == before
