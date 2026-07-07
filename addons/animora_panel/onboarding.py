"""Onboarding gate — the pre-app experience.

When Animora launches without a signed-in session, the UI is taken over by
a fullscreen, 3-slide onboarding flow (what Animora is → what it can do →
sign in). The main interface is unreachable until authentication succeeds;
after that, returning users with a valid session never see the gate again
(silent restore in auth.controller). A definitive mid-session sign-out or
session rejection reopens the gate at the sign-in slide.

Mechanics (no native/C++ surface exists for this — see plan):
- Takeover = ensure an ANIMORA editor area exists, then
  `screen.screen_full_area(use_hide_panels=True)` on it, which hides the
  topbar, workspace tabs, and status bar (Blender's "Focus Mode").
- ANIMORA_PT_onboarding owns the ANIMORA space while active (panel.py's
  regular panels poll False during the gate).
- A 0.5 s watcher re-asserts the takeover if the user escapes fullscreen
  or switches workspace, and tears the gate down on AuthS.CONNECTED.
- Escape hatches (gate must never trap a user on a broken sign-in):
  ANIMORA_SKIP_GATE=1 env var, always-rendered retry on FAILED, and
  onboarding.close_gate() from the Python console.
"""

from __future__ import annotations

import contextlib
import logging
import os

log = logging.getLogger("animora.onboarding")

_SLIDE_MIN, _SLIDE_MAX = 0, 2
_WATCH_INTERVAL = 0.5
_OPEN_RETRY_INTERVAL = 0.4

_active = False
_watch_registered = False
_preview_collection = None

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "onboarding_assets")
_SLIDE_ICONS = ("slide_1", "slide_2", "slide_3")

SLIDES = (
    {
        "icon": "slide_1",
        "title": "Describe it. Animora builds it.",
        "body": (
            "Professional 3D creation, powered by an AI that",
            "works like a senior artist — no experience needed.",
        ),
    },
    {
        "icon": "slide_2",
        "title": "Watch your ideas take shape.",
        "body": (
            "Animora sees the viewport in real time, checks its own",
            "work, and refines every detail until it's ready.",
        ),
    },
    {
        "icon": "slide_3",
        "title": "Sign in to start creating.",
        "body": (
            "Your AI artist is ready when you are.",
        ),
    },
)


# ---------------------------------------------------------------------------
# Gate predicate + state
# ---------------------------------------------------------------------------

def gate_needed(
    *,
    background: bool | None = None,
    bundle_mode: bool | None = None,
    skip_env: str | None = None,
    restorable: bool | None = None,
) -> bool:
    """Should the gate open at startup? Pure given explicit kwargs (unit
    tested); the None defaults read the live environment."""
    if background is None:
        import bpy
        background = bpy.app.background
    if bundle_mode is None:
        from . import bundle
        bundle_mode = bundle.is_bundle_mode()
    if skip_env is None:
        skip_env = os.environ.get("ANIMORA_SKIP_GATE", "")
    if restorable is None:
        from .auth import session
        restorable = session.has_restorable_session()

    if background or bundle_mode or restorable:
        return False
    return skip_env.strip().lower() not in {"1", "true", "yes"}


def gate_active() -> bool:
    return _active


def clamp_slide(value: int) -> int:
    return max(_SLIDE_MIN, min(_SLIDE_MAX, value))


# ---------------------------------------------------------------------------
# Takeover / teardown
# ---------------------------------------------------------------------------

def _main_window():
    import bpy
    windows = bpy.context.window_manager.windows
    return windows[0] if windows else None


def _animora_area(screen):
    for area in screen.areas:
        if area.type == "ANIMORA":
            return area
    return None


def _window_region(area):
    for region in area.regions:
        if region.type == "WINDOW":
            return region
    return None


def _redraw_animora_areas() -> None:
    import bpy
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "ANIMORA":
                area.tag_redraw()


def _try_takeover() -> bool:
    """Fullscreen the ANIMORA area on the main window. Returns True when the
    gated state is in place. Safe to call repeatedly (idempotent)."""
    import bpy

    win = _main_window()
    if win is None or win.screen is None:
        return False
    screen = win.screen

    if screen.show_fullscreen:
        # Already in a fullscreen (temp) screen: make sure it shows ANIMORA.
        big = max(screen.areas, key=lambda a: a.width * a.height)
        if big.type != "ANIMORA":
            try:
                big.type = "ANIMORA"
            except Exception as exc:
                log.debug("Gate: could not retype fullscreen area: %s", exc)
                return False
        return True

    area = _animora_area(screen)
    if area is None:
        # Reuse the addon's layout-injection fallback to create the area.
        from . import _ensure_left_ai_area
        _ensure_left_ai_area()
        area = _animora_area(screen)
        if area is None:
            return False

    region = _window_region(area)
    if region is None:
        return False
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screen_full_area(use_hide_panels=True)
    except Exception as exc:
        log.debug("Gate: fullscreen takeover failed (retrying): %s", exc)
        return False
    log.info("Onboarding gate active")
    return True


def _try_restore() -> None:
    """Leave fullscreen (back to the normal workspace screen)."""
    import bpy

    win = _main_window()
    if win is None or win.screen is None or not win.screen.show_fullscreen:
        return
    screen = win.screen
    big = max(screen.areas, key=lambda a: a.width * a.height)
    region = _window_region(big)
    try:
        with bpy.context.temp_override(window=win, area=big, region=region):
            bpy.ops.screen.screen_full_area(use_hide_panels=True)
    except Exception as exc:
        log.warning("Gate: could not leave fullscreen automatically: %s", exc)


def open_gate(slide: int = 0) -> None:
    """Open (or re-open) the onboarding gate at `slide`. Idempotent."""
    import bpy

    global _active
    if bpy.app.background:
        return
    from . import bundle
    if bundle.is_bundle_mode():
        return

    with contextlib.suppress(Exception):
        bpy.context.window_manager.animora_onboarding_slide = clamp_slide(slide)

    _mark_slide_changed()
    if _active:
        _redraw_animora_areas()
        return
    _active = True
    _register_art_handler()
    try:
        _try_takeover()
    except Exception as exc:
        # Context not ready (restricted context / no window yet) — the
        # watcher keeps retrying until the takeover sticks.
        log.debug("Gate takeover deferred: %s", exc)
    _ensure_watcher()
    with contextlib.suppress(Exception):
        _redraw_animora_areas()


def close_gate() -> None:
    """Tear the gate down and return to the normal workspace."""
    global _active
    if not _active:
        return
    _active = False
    _unregister_art_handler()
    try:
        _try_restore()
    finally:
        _redraw_animora_areas()
    log.info("Onboarding gate dismissed")


# ── GPU art handler + fade ─────────────────────────────────────────────

def _register_art_handler() -> None:
    global _art_handle
    if _art_handle is not None or not _textures:
        return
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is not None:
            _art_handle = space.draw_handler_add(_draw_art, (), "WINDOW", "PRE_VIEW")
    except Exception as exc:
        log.info("Onboarding art handler unavailable: %s", exc)


def _unregister_art_handler() -> None:
    global _art_handle
    if _art_handle is None:
        return
    try:
        import bpy
        space = getattr(bpy.types, "SpaceAnimora", None)
        if space is not None:
            space.draw_handler_remove(_art_handle, "WINDOW")
    except Exception:
        pass
    finally:
        _art_handle = None


def _mark_slide_changed() -> None:
    """Restart the fade and pump redraws for its duration."""
    global _slide_changed_at
    import time as _t
    _slide_changed_at = _t.monotonic()
    try:
        import bpy
        if not bpy.app.timers.is_registered(_fade_tick):
            bpy.app.timers.register(_fade_tick, first_interval=0.0)
    except Exception:
        pass


def _fade_tick() -> float | None:
    import time as _t
    if not _active:
        return None
    _redraw_animora_areas()
    if _t.monotonic() - _slide_changed_at >= _FADE_SEC:
        return None  # fade done — stop pumping
    return 0.03


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

def _ensure_watcher() -> None:
    import bpy

    global _watch_registered
    if not _watch_registered or not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=_OPEN_RETRY_INTERVAL)
        _watch_registered = True


def _watch() -> float | None:
    """While the gate is active: dismiss on CONNECTED, otherwise re-assert
    the fullscreen takeover (the user may have escaped fullscreen, switched
    workspace, or the window wasn't ready at open time)."""
    global _watch_registered
    try:
        if not _active:
            _watch_registered = False
            return None  # unregister
        from . import state
        if state.state.auth_status == state.AuthS.CONNECTED:
            close_gate()
            _watch_registered = False
            return None
        _try_takeover()
        # Keep the status area live while a sign-in is pending (labels don't
        # redraw on their own when state changes off the main thread).
        _redraw_animora_areas()
    except Exception as exc:  # a timer must never raise
        log.debug("Gate watcher tick error: %s", exc)
    return _WATCH_INTERVAL


# ---------------------------------------------------------------------------
# Slide art — GPU textures (crisp, native-resolution) with a preview-icon
# fallback for builds/GL contexts where texture upload fails.
# ---------------------------------------------------------------------------

_ART_ASPECT = 1600.0 / 1000.0   # slide canvas 16:10
_ART_BAND_FRAC = 220.0 / 1000.0  # bottom fraction reserved for buttons
_FADE_SEC = 0.22

_textures: dict[str, object] = {}
_texture_images: dict[str, object] = {}
_art_handle = None
_slide_changed_at = 0.0


def slide_icon_id(name: str) -> int:
    """Preview-icon fallback (soft/upscaled). 0 when unavailable."""
    pcoll = _preview_collection
    if pcoll is None:
        return 0
    icon = pcoll.get(name)
    return icon.icon_id if icon else 0


def art_available() -> bool:
    return bool(_textures)


def _load_previews() -> None:
    global _preview_collection
    import bpy.utils.previews

    pcoll = bpy.utils.previews.new()
    for name in _SLIDE_ICONS:
        path = os.path.join(_ASSET_DIR, f"{name}.png")
        if os.path.exists(path):
            pcoll.load(name, path, "IMAGE")
        else:
            log.warning("Onboarding slide asset missing: %s", path)
    _preview_collection = pcoll


def _load_textures() -> None:
    from .ads import primitives
    for name in _SLIDE_ICONS:
        path = os.path.join(_ASSET_DIR, f"{name}.png")
        if not os.path.exists(path):
            continue
        tex, img = primitives.load_gpu_texture(path)
        if tex is not None:
            _textures[name] = tex
            _texture_images[name] = img
    if _textures:
        log.info("Onboarding: %d slide textures loaded (crisp)", len(_textures))
    else:
        log.warning(
            "Onboarding: all slide textures failed to load — falling back "
            "to the blurry template_icon preview path. Last error: %s",
            primitives.last_texture_error,
        )
        _write_texture_failure_breadcrumb(primitives.last_texture_error)


def _write_texture_failure_breadcrumb(last_error: str | None) -> None:
    """Best-effort diagnostic file for the all-textures-failed case. The
    shipped launcher hides the console, so without this file a GPU/driver
    -specific load failure leaves zero trace anywhere."""
    try:
        import time as _t

        import bpy
        import gpu

        cfg_dir = bpy.utils.user_resource('CONFIG')
        if not cfg_dir:
            return
        renderer = vendor = "unknown"
        with contextlib.suppress(Exception):
            renderer = gpu.platform.renderer_get()
        with contextlib.suppress(Exception):
            vendor = gpu.platform.vendor_get()
        path = os.path.join(cfg_dir, "animora_onboarding_texture_failure.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                f"{_t.strftime('%Y-%m-%d %H:%M:%S')} — all onboarding slide "
                f"textures failed to load.\n"
                f"  last_error: {last_error}\n"
                f"  gpu_vendor: {vendor}\n"
                f"  gpu_renderer: {renderer}\n"
            )
    except Exception as exc:
        log.debug("Onboarding texture-failure breadcrumb write failed: %s", exc)


def _unload_previews() -> None:
    global _preview_collection
    if _preview_collection is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_preview_collection)
        _preview_collection = None
    _textures.clear()
    for img in _texture_images.values():
        try:
            import bpy
            bpy.data.images.remove(img)
        except Exception:
            pass
    _texture_images.clear()


def _art_fit_rect(region_w: int, region_h: int) -> tuple[float, float, float, float]:
    """'Contain' fit of the 16:10 art within the region (bottom-left origin).
    Letterbox bars are the slide's own bg colour so it reads full-bleed."""
    if region_w / region_h > _ART_ASPECT:
        # Region wider than art → height-bound.
        art_h = region_h
        art_w = art_h * _ART_ASPECT
    else:
        art_w = region_w
        art_h = art_w / _ART_ASPECT
    x = (region_w - art_w) * 0.5
    y = (region_h - art_h) * 0.5
    return x, y, art_w, art_h


def art_button_band_top_px() -> float:
    """Y (bottom-origin) of the top of the reserved button band, in the
    CURRENT region — the panel positions its nav/sign-in above this."""
    try:
        from .ads import primitives
        size = primitives.region_size()
        if size is None:
            return 0.0
        w, h = size
        _x, y, _aw, art_h = _art_fit_rect(w, h)
        return y + art_h * _ART_BAND_FRAC
    except Exception:
        return 0.0


def _draw_art() -> None:
    """PRE_VIEW handler (gate only): draw the current slide crisp + fading,
    then the headline/body via blf ON TOP (vector text — always sharp, never
    baked into the image)."""
    if not _active:
        return
    try:
        import bpy

        from .ads import primitives

        size = primitives.region_size()
        if size is None:
            return
        w, h = size
        slide = clamp_slide(bpy.context.window_manager.animora_onboarding_slide)
        name = _SLIDE_ICONS[slide]

        import time as _t
        fade = min(1.0, (_t.monotonic() - _slide_changed_at) / _FADE_SEC) if _FADE_SEC else 1.0
        x, y, aw, ah = _art_fit_rect(w, h)

        tex = _textures.get(name)
        if tex is not None:
            primitives.image_texture(tex, x, y, aw, ah, alpha=fade)

        _draw_slide_text(SLIDES[slide], x, y, aw, ah, fade)
    except Exception as exc:
        log.debug("Onboarding art draw failed: %s", exc)


def _ui_scale() -> float:
    try:
        import bpy
        return float(bpy.context.preferences.system.ui_scale)
    except Exception:
        return 1.0


def _draw_slide_text(spec: dict, ax: float, ay: float, aw: float, ah: float,
                     fade: float) -> None:
    """Draw the headline + body centered in the art's lower third via blf.
    Crisp at any size (vector), and it fades in with the slide."""
    from .ads import primitives

    s = _ui_scale()
    title_px = 34.0 * s
    body_px = 17.0 * s
    line_h = body_px * 1.55
    n_body = len(spec["body"])

    text_col = (0.94, 0.95, 0.99, fade)
    muted_col = (0.70, 0.69, 0.82, fade)
    cx = ax + aw * 0.5

    base_y = ay + ah * (_ART_BAND_FRAC + 0.05)
    block_h = n_body * line_h + title_px * 1.6  # body + gap + title
    block_top = base_y + block_h

    # Legibility scrim — a soft dark band BEHIND the text so it reads over any
    # background image (drawn at runtime; never baked into the asset). Fades to
    # transparent at both edges so it doesn't look like a hard box.
    scrim_lo = base_y - line_h * 0.8
    scrim_mid = (scrim_lo + block_top) * 0.5
    scrim_a = 0.55 * fade
    primitives.vertical_gradient_strip(
        x=ax, y=scrim_lo, w=aw, h=scrim_mid - scrim_lo,
        color_bottom=(0.02, 0.02, 0.05, 0.0),
        color_top=(0.02, 0.02, 0.05, scrim_a),
    )
    primitives.vertical_gradient_strip(
        x=ax, y=scrim_mid, w=aw, h=block_top - scrim_mid,
        color_bottom=(0.02, 0.02, 0.05, scrim_a),
        color_top=(0.02, 0.02, 0.05, 0.0),
    )

    # Body lines, then the headline above them (anchored upward from the band).
    by = base_y
    for line in reversed(spec["body"]):
        lw = primitives.text_width(body_px, line)
        primitives.text(cx - lw * 0.5, by, body_px, muted_col, line)
        by += line_h

    ty = by + title_px * 0.5
    tw = primitives.text_width(title_px, spec["title"])
    primitives.text(cx - tw * 0.5, ty, title_px, text_col, spec["title"])


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _make_operators():
    import bpy
    from bpy.types import Operator

    class OT_AnimoraOnboardingNav(Operator):
        bl_idname = "animora.onboarding_nav"
        bl_label = "Onboarding Navigation"
        bl_description = "Move between onboarding pages"
        bl_options = {"INTERNAL"}

        def execute(self, context):
            wm = context.window_manager
            current = wm.animora_onboarding_slide
            target = clamp_slide(self.goto if self.goto >= 0 else current + self.offset)
            if target != current:
                wm.animora_onboarding_slide = target
                _mark_slide_changed()  # fade the new slide in
            _redraw_animora_areas()
            return {"FINISHED"}

    # Assigned AFTER the class body: this module uses PEP 563 stringified
    # annotations, and Blender can't eval "bpy.props.IntProperty(...)" for a
    # class defined inside a function (bpy isn't in the module globals).
    # Real property objects in __annotations__ need no eval.
    OT_AnimoraOnboardingNav.__annotations__["offset"] = bpy.props.IntProperty(default=0)
    OT_AnimoraOnboardingNav.__annotations__["goto"] = bpy.props.IntProperty(default=-1)

    return [OT_AnimoraOnboardingNav]


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

def _make_panel():
    from bpy.types import Panel

    class ANIMORA_PT_onboarding(Panel):
        bl_label = ""
        bl_idname = "ANIMORA_PT_onboarding"
        bl_space_type = "ANIMORA"
        bl_region_type = "WINDOW"
        bl_options = {"HIDE_HEADER"}

        @classmethod
        def poll(cls, context):
            return gate_active()

        def draw(self, context):
            layout = self.layout
            wm = context.window_manager
            slide = clamp_slide(wm.animora_onboarding_slide)
            spec = SLIDES[slide]

            region_h = context.region.height if context.region else 900

            if art_available():
                # Crisp GPU art fills the region (drawn behind, PRE_VIEW).
                # The panel draws ONLY the nav / Sign In controls, pushed
                # down into the slide's reserved bottom band.
                band_top = art_button_band_top_px()  # bottom-origin px
                # Convert to a top separator: region_h - band_top is the
                # distance from the top edge down to the band. ~20px per
                # separator factor unit.
                gap_px = max(40.0, region_h - band_top - 10.0)
                layout.separator(factor=gap_px / 20.0)

                split = layout.split(factor=0.12)
                split.column()
                rest = split.split(factor=0.86)
                col = rest.column(align=False)
                rest.column()
                if slide < _SLIDE_MAX:
                    self._draw_nav(col, slide)
                else:
                    self._draw_dots_nav(col, slide, arrows=False)
                    col.separator(factor=0.6)
                    self._draw_signin(col, context)
                return

            # Fallback (no GPU texture): the old widget-drawn layout with
            # the soft preview icon, sized to fill the region.
            region_w = context.region.width if context.region else 1400
            art_scale = min(region_h * 0.70, region_w * 0.62) / 20.0
            art_scale = max(12.0, min(34.0, art_scale))
            top_pad = max(0.6, (region_h - art_scale * 20.0 - 160.0) / 2.0 / 20.0)
            layout.separator(factor=top_pad)

            split = layout.split(factor=0.10)
            split.column()  # left margin
            rest = split.split(factor=0.875)
            col = rest.column(align=False)
            rest.column()  # right margin

            self._draw_slide(col, context, slide, spec, art_scale)

        # ── Slide pieces ────────────────────────────────────────────────
        def _draw_slide(self, col, context, slide: int, spec: dict,
                        art_scale: float) -> None:
            art = col.row()
            art.alignment = "CENTER"
            icon_id = slide_icon_id(spec["icon"])
            if icon_id:
                art.template_icon(icon_value=icon_id, scale=art_scale)
            else:
                # Asset missing (dev tree) — fall back to text so the gate
                # still communicates. The PNGs bake this copy normally.
                title = col.row()
                title.alignment = "CENTER"
                title.label(text=spec["title"])
                for line in spec["body"]:
                    row = col.row()
                    row.alignment = "CENTER"
                    row.scale_y = 0.85
                    row.label(text=line)

            col.separator(factor=1.0)

            if slide < _SLIDE_MAX:
                self._draw_nav(col, slide)
            else:
                self._draw_dots_nav(col, slide, arrows=False)
                col.separator(factor=0.8)
                self._draw_signin(col, context)

        def _draw_nav(self, col, slide: int) -> None:
            """One centered pager row: [◀] ● ○ ○ [▶] — icon chevrons."""
            self._draw_dots_nav(col, slide, arrows=True)

        def _draw_dots_nav(self, col, slide: int, *, arrows: bool) -> None:
            nav = col.row()
            nav.alignment = "CENTER"
            inner = nav.row(align=True)
            inner.scale_x = 1.5
            inner.scale_y = 1.5

            if arrows:
                back = inner.row(align=True)
                back.enabled = slide > _SLIDE_MIN
                back_op = back.operator("animora.onboarding_nav", text="", icon="TRIA_LEFT")
                back_op.offset = -1
                back_op.goto = -1
                inner.separator(factor=1.2)

            for i in range(_SLIDE_MAX + 1):
                op = inner.operator(
                    "animora.onboarding_nav",
                    text="●" if i == slide else "○",
                    emboss=False,
                )
                op.goto = i
                op.offset = 0

            if arrows:
                inner.separator(factor=1.2)
                nxt_op = inner.operator("animora.onboarding_nav", text="", icon="TRIA_RIGHT")
                nxt_op.offset = 1
                nxt_op.goto = -1

        def _draw_signin(self, col, context) -> None:
            from . import state
            from .preferences import get_prefs

            status = state.state.auth_status
            in_flight = status in {
                state.AuthS.PENDING_BROWSER,
                state.AuthS.EXCHANGING_CODE,
                state.AuthS.CONNECTING,
            }

            action = col.row()
            action.alignment = "CENTER"
            inner = action.row(align=True)
            inner.scale_x = 2.0
            inner.scale_y = 1.7
            if in_flight:
                inner.enabled = False
                inner.operator("animora.sign_in", text="Waiting…", icon="SORTTIME")
            else:
                inner.operator("animora.sign_in", text="Sign In", icon="URL")

            col.separator(factor=0.6)

            if in_flight:
                hint = col.row()
                hint.alignment = "CENTER"
                hint.scale_y = 0.85
                hint.label(
                    text=state.state.auth_message or "Waiting for browser confirmation…",
                    icon="SORTTIME",
                )
            elif status == state.AuthS.FAILED:
                box = col.box()
                box.alert = True
                msg = box.column(align=True)
                msg.scale_y = 0.9
                msg.label(
                    text=state.state.auth_message or "Sign-in failed. Please try again.",
                    icon="ERROR",
                )

            back = col.row()
            back.alignment = "CENTER"
            back_inner = back.row(align=True)
            back_op = back_inner.operator(
                "animora.onboarding_nav", text="", icon="TRIA_LEFT", emboss=False
            )
            back_op.offset = -1
            back_op.goto = -1

            if get_prefs().dev_mode:
                col.separator(factor=0.8)
                dev = col.row()
                dev.alignment = "CENTER"
                dev.operator(
                    "animora.dev_connect",
                    text="Dev: Connect to Local Backend",
                    icon="CONSOLE",
                )

    return [ANIMORA_PT_onboarding]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes: list = []


def register() -> None:
    import bpy

    global _classes
    _classes = _make_operators()
    if getattr(bpy.types, "SpaceAnimora", None) is not None:
        _classes += _make_panel()
    else:
        log.warning("SpaceAnimora missing — onboarding gate disabled (dev build?)")

    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.animora_onboarding_slide = bpy.props.IntProperty(
        default=0, min=_SLIDE_MIN, max=_SLIDE_MAX,
    )

    if bpy.app.background:
        return

    _load_previews()
    _load_textures()  # crisp GPU art; falls back to previews if it fails

    if gate_needed() and getattr(bpy.types, "SpaceAnimora", None) is not None:
        # Defer past the layout-injection tick (0.8 s in __init__) so the
        # ANIMORA area exists before we fullscreen it.
        def _deferred_open():
            open_gate(slide=0)
            return None
        bpy.app.timers.register(_deferred_open, first_interval=0.9)


def unregister() -> None:
    import bpy

    global _active, _watch_registered
    _active = False
    _watch_registered = False
    _unregister_art_handler()
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    _unload_previews()
    del bpy.types.WindowManager.animora_onboarding_slide
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
