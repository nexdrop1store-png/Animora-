"""Media-type sniffing from magic bytes — the fix for the 400 mismatch."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# image_media is pure (no package imports) — load it by file so we don't
# trigger orchestrator/__init__ (which pulls in the whole streaming stack).
_spec = importlib.util.spec_from_file_location(
    "animora_image_media",
    Path(__file__).resolve().parent.parent / "orchestrator" / "image_media.py",
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
sniff_image_media_type = _mod.sniff_image_media_type

# Real minimal magic-byte prefixes for each format.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 8
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8
GIF = b"GIF89a\x01\x00\x01\x00\x00" + b"\x00" * 8


def test_jpeg_detected():
    assert sniff_image_media_type(JPEG) == "image/jpeg"


def test_png_detected():
    assert sniff_image_media_type(PNG) == "image/png"


def test_webp_detected():
    assert sniff_image_media_type(WEBP) == "image/webp"


def test_gif_detected():
    assert sniff_image_media_type(GIF) == "image/gif"


def test_jpeg_bytes_never_labeled_png():
    # The exact bug: JPEG bytes must never come back as image/png.
    assert sniff_image_media_type(JPEG) != "image/png"


def test_unknown_falls_back_to_default():
    assert sniff_image_media_type(b"not an image at all") == "image/jpeg"
    assert sniff_image_media_type(b"xyz", default="image/png") == "image/png"


def test_empty_and_short_safe():
    assert sniff_image_media_type(b"") == "image/jpeg"
    assert sniff_image_media_type(b"\xff\xd8") == "image/jpeg"  # too short → default
