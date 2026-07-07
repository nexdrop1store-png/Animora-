"""
GPU chrome overlays for the ANIMORA panel.

Two draw routines, both attached as POST_PIXEL handlers on SpaceAnimora:

  1. _draw_border_glow — animated rim around the panel edge while
     active. Pulses indigo/cyan/amber depending on the state. Off when
     idle.

  2. _draw_chrome_accents — always-on subtle elements that add visual
     hierarchy WITHOUT covering up the bpy UILayout content:
       • Thin accent line under the header (~36px from top)
       • Thin accent line above the input (~80px from bottom)
       • A soft underglow strip beneath the LATEST assistant message
         while it's streaming, so the user's eye snaps to the live text

POST_PIXEL handlers draw ON TOP of the region's regular bpy content,
which means we can ONLY draw thin overlay strokes — solid fills would
cover up text/buttons. The visual richness comes from layering accent
strokes + the bpy layout's own boxing.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from . import state as state_module

log = logging.getLogger("animora.border_glow")


# Color components for the pulse. RGB picked to match the Animora indigo
# palette — see assets/branding/. The pulse modulates alpha, not the
# color itself, so the rim feels like a single light source dimming up
# and down rather than colour-cycling.
_GLOW_COLOR_THINKING = (0.55, 0.42, 1.00)   # indigo — thinking / streaming
_GLOW_COLOR_EXECUTING = (0.42, 0.78, 1.00)  # cyan — tool execution
_GLOW_COLOR_QUALITY = (1.00, 0.78, 0.30)    # warm — quality checking

_PULSE_PERIOD_SEC = 1.8  # one full pulse takes ~1.8s
# Strengthened 2026-07-05: the original 0.18/0.55 rim read as invisible on
# the darker glass backdrop — the "working glow" is a headline feature and
# must be unmistakable.
_OUTER_LINE_WIDTH = 9.0
_INNER_LINE_WIDTH = 3.0
_OUTER_MAX_ALPHA = 0.40
_INNER_MAX_ALPHA = 0.85

# Always-on accent palette — indigo for divider lines, used at LOW alpha
# so they read as subtle chrome, not as bright UI lines that fight the
# bpy layout. RGB matches the Animora brand purple.
_ACCENT_COLOR = (0.55, 0.42, 1.00)
_ACCENT_DIVIDER_ALPHA = 0.22

_border_handle: Any | None = None
_chrome_handle: Any | None = None


def _color_for_state() -> tuple[float, float, float]:
    s = state_module.state.current
    if s == state_module.S.EXECUTING:
        return _GLOW_COLOR_EXECUTING
    if s == state_module.S.QUALITY_CHECK:
        return _GLOW_COLOR_QUALITY
    return _GLOW_COLOR_THINKING


def _pulse_alpha() -> float:
    """0..1 sinusoidal modulator. Smooth, not jarring."""
    phase = (time.monotonic() % _PULSE_PERIOD_SEC) / _PULSE_PERIOD_SEC
    # 0.5 → 1.0 → 0.5 — never goes fully dark, so the user can always
    # tell something is active even at the bottom of the pulse.
    return 0.55 + 0.45 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))


def _get_gpu():
    """Resolve gpu + batch_for_shader. Returns None if unavailable.
    Cached to avoid repeated import on every redraw."""
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        return gpu, batch_for_shader
    except ImportError:
        return None


def _region_size():
    try:
        import bpy
        region = bpy.context.region
        if region is None or region.width < 10 or region.height < 10:
            return None
        return region.width, region.height
    except Exception:
        return None


_draw_error_logged = False


def _draw_border_glow() -> None:
    """Animated rim around the panel edge while AI is active. Off when idle."""
    global _draw_error_logged
    try:
        _draw_border_glow_impl()
    except Exception as exc:
        if not _draw_error_logged:
            _draw_error_logged = True
            log.warning("border glow draw failed: %s", exc)


def _draw_border_glow_impl() -> None:
    if not state_module.is_active():
        return
    gp = _get_gpu()
    if gp is None:
        return
    gpu, batch_for_shader = gp
    size = _region_size()
    if size is None:
        return
    w, h = size

    inset = 1
    coords = [
        (inset, inset), (w - inset, inset),
        (w - inset, inset), (w - inset, h - inset),
        (w - inset, h - inset), (inset, h - inset),
        (inset, h - inset), (inset, inset),
    ]
    pulse = _pulse_alpha()
    r, g, b = _color_for_state()

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": coords})

    gpu.state.line_width_set(_OUTER_LINE_WIDTH)
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", (r, g, b, _OUTER_MAX_ALPHA * pulse))
    batch.draw(shader)

    gpu.state.line_width_set(_INNER_LINE_WIDTH)
    shader.uniform_float("color", (r, g, b, _INNER_MAX_ALPHA * pulse))
    batch.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def _draw_horizontal_line(gpu_mod, batch_for_shader, x0: float, x1: float, y: float,
                          color: tuple[float, float, float], alpha: float,
                          width_px: float = 1.0) -> None:
    """Helper — draw a single horizontal accent line at pixel y."""
    coords = [(x0, y), (x1, y)]
    shader = gpu_mod.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": coords})
    r, g, b = color
    gpu_mod.state.line_width_set(width_px)
    gpu_mod.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", (r, g, b, alpha))
    batch.draw(shader)
    gpu_mod.state.line_width_set(1.0)
    gpu_mod.state.blend_set("NONE")


def _draw_chrome_accents() -> None:
    """Always-on subtle accents that add hierarchy without covering UI.

    Currently: a single thin indigo divider line just below the header,
    so the header chrome reads as visually separate from the conversation
    body. Cheap (one line draw per frame); not animated.
    """
    gp = _get_gpu()
    if gp is None:
        return
    gpu, batch_for_shader = gp
    size = _region_size()
    if size is None:
        return
    w, h = size

    # Header divider: ~26 pixels from the top. Tuned to land just below
    # Blender's default header height at default UI scale.
    header_y = h - 26
    if 0 < header_y < h:
        _draw_horizontal_line(
            gpu, batch_for_shader,
            x0=10, x1=w - 10, y=header_y,
            color=_ACCENT_COLOR, alpha=_ACCENT_DIVIDER_ALPHA,
        )


def register() -> None:
    global _border_handle, _chrome_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is None:
            log.warning("SpaceAnimora type not registered — chrome disabled")
            return
        if _border_handle is None:
            _border_handle = space.draw_handler_add(
                _draw_border_glow, (), "WINDOW", "POST_PIXEL",
            )
        if _chrome_handle is None:
            _chrome_handle = space.draw_handler_add(
                _draw_chrome_accents, (), "WINDOW", "POST_PIXEL",
            )
        log.debug("Panel chrome draw handlers registered")
    except Exception as exc:
        log.warning("Failed to register chrome handlers: %s", exc)


def unregister() -> None:
    global _border_handle, _chrome_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is not None:
            if _border_handle is not None:
                space.draw_handler_remove(_border_handle, "WINDOW")
            if _chrome_handle is not None:
                space.draw_handler_remove(_chrome_handle, "WINDOW")
    except Exception as exc:
        log.debug("Failed to remove chrome handlers: %s", exc)
    finally:
        _border_handle = None
        _chrome_handle = None
