"""
Animora Design System — canvas / draw handler.

Owns the ONE POST_PIXEL draw handler that ADS uses to paint chrome onto
the SpaceAnimora WINDOW region. Future phases dispatch from here to a
widget tree; Phase A draws a single deliverable: a status accent strip
that pulses with state colour above the input area while the AI is active.

Why one handler (not per widget):
  • Cheaper — one shader-bind + viewport set, then multiple primitives.
  • Easier teardown — one handle to track per register/unregister cycle.
  • Aligns with how Blender itself batches HUD draws.

The handler stays installed for the lifetime of the addon; the visible
chrome is gated on `state.is_active()` so it adds zero visual noise
while the panel is idle.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import state as state_module
from . import primitives, tokens

log = logging.getLogger("animora.ads.canvas")

_canvas_handle: Optional[Any] = None


# Pixel reservations measured against the bpy layout used by panel.py
# (see panel._draw_input — split factor 0.15, scale_y 1.5/1.6). These
# constants describe the "input area" footprint at the bottom of the
# region so the accent strip can land just above it.
_INPUT_AREA_HEIGHT_PX = 88     # prompt row (~36) + action row (~32) + padding
_ACCENT_STRIP_HEIGHT_PX = 4    # solid accent band
_ACCENT_GLOW_HEIGHT_PX = 18    # soft gradient above the accent strip
_SIDE_INSET_PX = 10            # horizontal margin from region edges


def _accent_color() -> tuple[float, float, float]:
    """Pick the state-coloured accent. Matches border_glow's palette so
    the two chrome layers feel like one design language."""
    cur = state_module.state.current
    if cur == state_module.S.EXECUTING:
        return tokens.ACCENT_CYAN
    if cur == state_module.S.QUALITY_CHECK:
        return tokens.ACCENT_WARM
    if cur == state_module.S.ERROR:
        return tokens.ACCENT_DANGER
    if cur == state_module.S.COMPLETE:
        return tokens.ACCENT_SUCCESS
    return tokens.ACCENT_PRIMARY


def _draw_canvas() -> None:
    """POST_PIXEL handler. Renders ADS-managed chrome.

    Phase A scope: a single status accent strip + soft underglow band
    sitting just above the input area when the AI is active. Idle state
    draws nothing so the panel reads as quiet.
    """
    # Idle: don't draw anything. Saves a few primitives per frame and
    # avoids tinting the panel when there's nothing to indicate.
    cur = state_module.state.current
    if cur == state_module.S.IDLE:
        return

    size = primitives.region_size()
    if size is None:
        return
    w, _h = size

    # Position the accent just above the bpy-drawn input area.
    strip_y = _INPUT_AREA_HEIGHT_PX
    strip_x = _SIDE_INSET_PX
    strip_w = max(0, w - 2 * _SIDE_INSET_PX)

    r, g, b = _accent_color()

    # Soft underglow — fades from full accent at the strip up to
    # transparent. Reads as a halo under the status pill text without
    # covering it (gradient top alpha is 0).
    primitives.vertical_gradient_strip(
        x=strip_x,
        y=strip_y,
        w=strip_w,
        h=_ACCENT_GLOW_HEIGHT_PX,
        color_bottom=(r, g, b, 0.22),
        color_top=(r, g, b, 0.00),
    )

    # Solid accent band — sharp 4px line; the visual anchor of the strip.
    primitives.horizontal_strip(
        x=strip_x,
        y=strip_y,
        w=strip_w,
        h=_ACCENT_STRIP_HEIGHT_PX,
        color=(r, g, b, 0.75),
    )


def register() -> None:
    global _canvas_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is None:
            log.warning("SpaceAnimora type not registered — ADS canvas disabled")
            return
        if _canvas_handle is None:
            _canvas_handle = space.draw_handler_add(
                _draw_canvas, (), "WINDOW", "POST_PIXEL",
            )
        log.debug("ADS canvas draw handler registered")
    except Exception as exc:
        log.warning("Failed to register ADS canvas: %s", exc)


def unregister() -> None:
    global _canvas_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is not None and _canvas_handle is not None:
            space.draw_handler_remove(_canvas_handle, "WINDOW")
    except Exception as exc:
        log.debug("Failed to remove ADS canvas: %s", exc)
    finally:
        _canvas_handle = None
