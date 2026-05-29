"""
Vision-frame ring buffer (Redis-backed).

Holds the most recent N viewport frames and the most recent K HD captures
for each active session, so the LLM context builder can pull "what does
the viewport look like right now?" without round-tripping to the addon.

Storage layout:

  vision:{session_id}:frames     LIST of base64-encoded JPEGs (most recent
                                 at tail). Capped at MAX_FRAMES. TTL 5 min.
  vision:{session_id}:hd:{trigger}  STRING, base64-encoded PNG. TTL 1 hour.
  vision:{session_id}:hd_index   LIST of trigger labels in receive order.

Frame format on the wire is binary (17-byte header + JPEG payload — see
docs/AI_ARCHITECTURE.md §3.1). Inside Redis we store the raw JPEG bytes
as base64 so a JSON-only HGET tool can read them.

Backpressure:
  buffer_depth(session_id) returns the current frame count. main.py uses
  it to decide when to send `pause_stream` / `resume_stream` control
  messages to the addon.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

from .session_manager import get_redis

log = logging.getLogger("animora.vision_buffer")

MAX_FRAMES = 5              # rolling window of viewport frames per session
FRAMES_TTL_SEC = 300        # 5 minutes
HD_CAPTURES_KEEP = 8        # last 8 HD captures per session
HD_TTL_SEC = 3600           # 1 hour

# Backpressure thresholds — main.py compares against these.
PAUSE_AT = 5                # if buffer hits this, send pause_stream
RESUME_AT = 2               # if buffer drops to this, send resume_stream


def _frames_key(session_id: str) -> str:
    return f"vision:{session_id}:frames"


def _hd_key(session_id: str, trigger: str) -> str:
    return f"vision:{session_id}:hd:{trigger}"


def _hd_index_key(session_id: str) -> str:
    return f"vision:{session_id}:hd_index"


async def push_viewport_frame(session_id: str, jpeg_bytes: bytes) -> int:
    """Push a viewport frame; returns the new buffer depth (for backpressure)."""
    r = await get_redis()
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    key = _frames_key(session_id)

    pipe = r.pipeline()
    pipe.rpush(key, encoded)
    pipe.ltrim(key, -MAX_FRAMES, -1)
    pipe.expire(key, FRAMES_TTL_SEC)
    pipe.llen(key)
    results = await pipe.execute()
    depth = int(results[-1])
    return depth


async def get_latest_viewport_frame(session_id: str) -> Optional[bytes]:
    """Returns the most recent viewport frame's JPEG bytes, or None."""
    r = await get_redis()
    items = await r.lrange(_frames_key(session_id), -1, -1)
    if not items:
        return None
    try:
        return base64.b64decode(items[0])
    except Exception as exc:
        log.warning("Failed to decode viewport frame for %s: %s", session_id, exc)
        return None


async def push_hd_capture(session_id: str, png_bytes: bytes, trigger: str) -> None:
    """Store an HD capture under its trigger label. Replaces any prior
    capture with the same trigger."""
    r = await get_redis()
    encoded = base64.b64encode(png_bytes).decode("ascii")
    key = _hd_key(session_id, trigger)
    idx_key = _hd_index_key(session_id)

    pipe = r.pipeline()
    pipe.setex(key, HD_TTL_SEC, encoded)
    # Append to index, trim to keep N most-recent triggers
    pipe.rpush(idx_key, trigger)
    pipe.ltrim(idx_key, -HD_CAPTURES_KEEP, -1)
    pipe.expire(idx_key, HD_TTL_SEC)
    await pipe.execute()


async def get_latest_hd_capture(session_id: str) -> Optional[tuple[bytes, str]]:
    """Returns `(png_bytes, trigger)` for the most recent HD capture, or None."""
    r = await get_redis()
    idx = await r.lrange(_hd_index_key(session_id), -1, -1)
    if not idx:
        return None
    trigger = idx[0]
    encoded = await r.get(_hd_key(session_id, trigger))
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded), trigger
    except Exception as exc:
        log.warning("Failed to decode HD capture %s/%s: %s", session_id, trigger, exc)
        return None


async def get_hd_capture_by_trigger(session_id: str, trigger: str) -> Optional[bytes]:
    """Returns the PNG bytes for a specific named trigger (e.g. 'post_script')."""
    r = await get_redis()
    encoded = await r.get(_hd_key(session_id, trigger))
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded)
    except Exception:
        return None


async def buffer_depth(session_id: str) -> int:
    """Current viewport-frame buffer depth. Used for backpressure decisions."""
    r = await get_redis()
    return int(await r.llen(_frames_key(session_id)))


async def clear_session_vision(session_id: str) -> None:
    """Called on session close — frees Redis memory immediately."""
    r = await get_redis()
    pipe = r.pipeline()
    pipe.delete(_frames_key(session_id))
    idx = await r.lrange(_hd_index_key(session_id), 0, -1)
    for trigger in idx:
        pipe.delete(_hd_key(session_id, trigger))
    pipe.delete(_hd_index_key(session_id))
    await pipe.execute()
