"""Image media-type detection from raw bytes.

The vision channel (hd_capture frames, tool_result HD captures, uploaded
references) historically carried NO reliable format label — a function
named `capture_hd_png` actually emits JPEG, and several embed sites
hardcoded `image/png`. Anthropic validates the declared media_type against
the actual bytes and rejects mismatches with HTTP 400, which silently
broke every image-bearing request.

The fix: never trust a label. Sniff the magic bytes at the embed site.
This makes any format (PNG/JPEG/WebP/GIF) work regardless of what the
addon claimed, and masks mislabels from older addon versions.
"""

from __future__ import annotations

_DEFAULT = "image/jpeg"  # viewport captures are JPEG; safest default


def sniff_image_media_type(raw: bytes, default: str = _DEFAULT) -> str:
    """Return the Anthropic media_type for `raw` from its magic bytes.

    Supports the formats Claude accepts: png, jpeg, webp, gif. Falls back
    to `default` when the signature is unrecognized (rather than asserting
    a wrong label that Anthropic would reject)."""
    if not raw or len(raw) < 12:
        return default
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return default
