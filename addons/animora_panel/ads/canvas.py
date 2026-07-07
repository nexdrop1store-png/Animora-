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
import math
import time
from typing import Any

from .. import state as state_module
from . import primitives, tokens

log = logging.getLogger("animora.ads.canvas")

_canvas_handle: Any | None = None

# Breathing period for the active-state accent. Kept in phase with
# border_glow._PULSE_PERIOD_SEC so the two chrome layers breathe together.
_PULSE_PERIOD_SEC = 1.8


def _pulse(lo: float, hi: float) -> float:
    """Time-based ease between lo..hi (sinusoidal breathing)."""
    phase = (time.monotonic() % _PULSE_PERIOD_SEC) / _PULSE_PERIOD_SEC
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(phase * 2.0 * math.pi))


# Pixel reservations for the accent chrome (all multiplied by UI scale at
# draw time). The accent strip rides the top edge of the composer glass
# card drawn by the underlay below.
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


_draw_error_logged = False


def _guarded(fn):
    """Wrap a draw handler so a rendering bug logs ONCE instead of
    spamming a traceback per redraw (and so we hear about it from testers
    via the console log rather than 'the glow is missing')."""
    def _wrapped():
        global _draw_error_logged
        try:
            fn()
        except Exception as exc:
            if not _draw_error_logged:
                _draw_error_logged = True
                log.warning("ADS draw handler failed (%s): %s", fn.__name__, exc)
    _wrapped.__name__ = fn.__name__
    return _wrapped


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
    s = _ui_scale()

    # Position the accent just above the composer glass card (underlay).
    strip_y = (_COMPOSER_MARGIN_PX + _composer_height_px(w)) * s
    strip_x = _SIDE_INSET_PX * s
    strip_w = max(0.0, w - 2 * _SIDE_INSET_PX * s)

    r, g, b = _accent_color()
    active = state_module.is_active()

    # Soft underglow halo above the band (breathes).
    glow_a = _pulse(0.12, 0.34) if active else 0.22
    primitives.vertical_gradient_strip(
        x=strip_x,
        y=strip_y,
        w=strip_w,
        h=_ACCENT_GLOW_HEIGHT_PX,
        color_bottom=(r, g, b, glow_a),
        color_top=(r, g, b, 0.00),
    )

    band_h = _ACCENT_STRIP_HEIGHT_PX * s
    if active:
        # WORKING: an unmistakable highlight SWEEPS left→right across the band
        # (motion reads as "Animora is working" far better than a static rim).
        # A dim base band with a bright gaussian blob riding along it, drawn as
        # thin segments so the falloff is smooth.
        primitives.horizontal_strip(strip_x, strip_y, strip_w, band_h, (r, g, b, 0.22))
        period = 1.5
        phase = (time.monotonic() % period) / period
        center = strip_x + phase * strip_w
        sigma = max(1.0, strip_w * 0.11)
        segs = 56
        seg_w = strip_w / segs
        for i in range(segs):
            sx = strip_x + i * seg_w
            dist = (sx + seg_w * 0.5) - center
            falloff = math.exp(-(dist / sigma) ** 2)
            if falloff < 0.02:
                continue
            primitives.horizontal_strip(
                sx, strip_y, seg_w + 1.0, band_h, (r, g, b, 0.22 + 0.75 * falloff),
            )
    else:
        # Settled (COMPLETE/ERROR) — a steady band, no motion.
        primitives.horizontal_strip(strip_x, strip_y, strip_w, band_h, (r, g, b, 0.75))


# ── Underlay ("glass") layer — drawn BEHIND the bpy widgets ────────────
# The native ANIMORA region dispatches Python 'PRE_VIEW' handlers between
# its background clear and the widget pass (space_animora.cc), so opaque
# designed chrome here never covers text. On binaries without that
# dispatch the handler is simply never called — harmless.

_underlay_handle: Any | None = None

_COMPOSER_MARGIN_PX = 6       # gap between region edge and the glass card
_COMPOSER_HEIGHT_PX = 104     # covers the input box + attachment chips
_COMPOSER_RADIUS_PX = 10


def _ui_scale() -> float:
    try:
        import bpy
        return float(bpy.context.preferences.system.ui_scale)
    except Exception:
        return 1.0


_PREVIEW_ROW_PX = 17   # matches panel.py preview rows at scale_y 0.75
_CHIP_ROW_PX = 21      # matches panel.py chip rows at scale_y 0.85


def _composer_height_px(region_width: int) -> float:
    """Unscaled glass-card height: base + long-prompt preview + chips.

    Reuses panel.composer_preview_lines / operators.pending_attachments so
    the card ALWAYS matches what the bpy composer actually draws."""
    extra = 0.0
    try:
        import bpy

        from .. import operators as ops_module
        from .. import panel as panel_module
        text = getattr(bpy.context.window_manager, "animora_input_text", "") or ""
        preview = panel_module.composer_preview_lines(text, region_width)
        if preview:
            extra += len(preview) * _PREVIEW_ROW_PX + 6
        extra += len(ops_module.pending_attachments()) * _CHIP_ROW_PX
    except Exception:
        pass
    return _COMPOSER_HEIGHT_PX + extra


def _draw_underlay() -> None:
    # The onboarding gate owns the ANIMORA region while it's up — its own
    # PRE_VIEW handler paints the full-screen slide art. Drawing the
    # composer glass card here would land on top of the sign-in slide.
    try:
        from .. import onboarding
        if onboarding.gate_active():
            return
    except Exception:
        pass

    size = primitives.region_size()
    if size is None:
        return
    w, h = size
    s = _ui_scale()

    # 1. Designed backdrop: a subtle top-to-bottom indigo wash instead of
    #    the flat editor grey. Opaque is fine — we're under the widgets.
    top = (*tokens.BG_BASE, 1.0)
    bottom = (
        tokens.BG_BASE[0] + 0.025,
        tokens.BG_BASE[1] + 0.02,
        tokens.BG_BASE[2] + 0.07,
        1.0,
    )
    primitives.vertical_gradient_strip(
        x=0, y=0, w=w, h=h, color_bottom=bottom, color_top=top,
    )

    # 2. Composer glass card: an elevated rounded surface behind the input
    #    area, with a soft drop shadow and a breathing accent outline while
    #    the AI is active. The bpy input box draws inside this footprint,
    #    reading as a floating modern composer.
    margin = _COMPOSER_MARGIN_PX * s
    card_h = _composer_height_px(w) * s
    card_x = margin
    card_y = margin
    card_w = max(0.0, w - margin * 2)
    radius = _COMPOSER_RADIUS_PX * s

    primitives.soft_shadow_rounded(card_x, card_y, card_w, card_h, radius,
                                   spread=8.0 * s, alpha=0.35)
    primitives.rounded_rect_fill(card_x, card_y, card_w, card_h, radius,
                                 (*tokens.BG_ELEVATED, 1.0))

    r, g, b = _accent_color()
    active = state_module.is_active()
    outline_a = _pulse(0.35, 0.90) if active else 0.28
    primitives.rounded_rect_outline(card_x, card_y, card_w, card_h, radius,
                                    (r, g, b, outline_a),
                                    width=2.0 if active else 1.0)


def register() -> None:
    global _canvas_handle, _underlay_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is None:
            log.warning("SpaceAnimora type not registered — ADS canvas disabled")
            return
        if _canvas_handle is None:
            _canvas_handle = space.draw_handler_add(
                _guarded(_draw_canvas), (), "WINDOW", "POST_PIXEL",
            )
        if _underlay_handle is None:
            try:
                _underlay_handle = space.draw_handler_add(
                    _guarded(_draw_underlay), (), "WINDOW", "PRE_VIEW",
                )
            except Exception as exc:
                # Binary predates the native PRE_VIEW dispatch — degrade to
                # accents-only chrome.
                log.info("ADS underlay unavailable on this build: %s", exc)
        log.debug("ADS canvas draw handlers registered")
    except Exception as exc:
        log.warning("Failed to register ADS canvas: %s", exc)


def unregister() -> None:
    global _canvas_handle, _underlay_handle
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is not None:
            if _canvas_handle is not None:
                space.draw_handler_remove(_canvas_handle, "WINDOW")
            if _underlay_handle is not None:
                space.draw_handler_remove(_underlay_handle, "WINDOW")
    except Exception as exc:
        log.debug("Failed to remove ADS canvas: %s", exc)
    finally:
        _canvas_handle = None
        _underlay_handle = None
