"""
Animora AI Panel — Blender addon entry point.

Registers all operators, panels, and preferences. Auto-loaded when
Animora launches (set in default userpref.blend).
"""

from __future__ import annotations

bl_info = {
    "name": "Animora AI Panel",
    "author": "Animora Technologies",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-Panel > Animora",
    "description": "AI-powered assistant integrated into the 3D viewport",
    "category": "Interface",
    "doc_url": "https://animora.tech/docs",
    "tracker_url": "https://animora.tech/support",
}

import importlib
import logging

log = logging.getLogger("animora")

# Sub-modules registered in order (informational — the authoritative list
# with ordering rationale lives in _import_modules() below).
_MODULES = [
    "preferences",
    "auth",
    "ws_client",
    "vision",
    "preview_icons",
    "state",
    "operators",
    "panel",
    "onboarding",
    "sculpt_guard",
    "border_glow",
    "ads",
    "ui.chat_display",
    "ui.properties",
    "bundle",
]

_loaded: list = []
_layout_timer_registered = False


def _has_native_animora_space() -> bool:
    import bpy

    return getattr(bpy.types, "SpaceAnimora", None) is not None


def _import_modules() -> list:
    import importlib
    # Pure utility modules — imported for their side-effect of being loadable;
    # no register/unregister hooks. Doing the import here surfaces any
    # import-time failures (missing `keyring`, etc.) during addon load.
    from . import api_validator, credentials  # noqa: F401

    from . import (
        ads,
        auth,
        border_glow,
        bundle,
        onboarding,
        operators,
        panel,
        preferences,
        preview_icons,
        sculpt_guard,
        state,
        vision,
        ws_client,
    )
    from .ui import chat_display, properties as props_panel

    # Registration order matters:
    #   - preview_icons before panel (panel uses icon_value lookups)
    #   - state before panel (panel reads state on draw)
    #   - panel before border_glow / ads (both need SpaceAnimora active)
    #   - onboarding after panel (its gate hides the panel via poll) and
    #     after auth (gate_needed reads the persisted session)
    #   - bundle LAST: its auto-launch/auto-connect uses preferences, auth,
    #     operators, ws_client — all must be registered first. No-op unless
    #     a bundle_config.json shipped alongside the addon (recording build).
    return [preferences, auth, ws_client, vision, preview_icons, state,
            operators, panel, onboarding, sculpt_guard, border_glow, ads,
            chat_display, props_panel, bundle]


def register() -> None:
    import bpy

    log.info("Animora AI Panel v%s loading", ".".join(str(v) for v in bl_info["version"]))

    global _loaded
    _loaded = _import_modules()

    for mod in _loaded:
        if hasattr(mod, "register"):
            mod.register()

    if bpy.app.background:
        # Headless run (CI checks, renders): no UI to arrange. Sub-modules
        # apply their own background guards for auth / timers / threads.
        pass
    else:
        if _has_native_animora_space():
            _register_layout_ensure()
        else:
            log.warning(
                "SpaceAnimora type not registered in this build; "
                "skipping forced layout injection and using sidebar fallback",
            )

        # Brand theme (Refined Indigo) — applied once per theme.THEME_VERSION,
        # including onto pre-existing grey userprefs. See theme.py.
        from . import theme
        theme.ensure_theme()

    log.info("Animora AI Panel registered")


def unregister() -> None:
    _unregister_layout_ensure()
    for mod in reversed(_loaded):
        if hasattr(mod, "unregister"):
            mod.unregister()


def _ensure_left_ai_area() -> None:
    """Guarantee Animora's core AI surface is visible in legacy layouts.

    Fresh installs get the left-side ANIMORA editor from startup.blend. This
    fallback is for existing userprefs or .blend files that still open into a
    plain Blender-style viewport. It is intentionally conservative: if any
    ANIMORA area already exists, it leaves the user's layout alone.
    """
    import bpy

    if not _has_native_animora_space():
        return

    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return
    if any(area.type == "ANIMORA" for area in screen.areas):
        return

    viewports = [
        area for area in screen.areas
        if area.type == "VIEW_3D" and area.width > 120 and area.height > 120
    ]
    if not viewports:
        return

    viewport = max(viewports, key=lambda area: area.width * area.height)
    region = next((r for r in viewport.regions if r.type == "WINDOW"), None)
    if region is None:
        return

    before = {area.as_pointer() for area in screen.areas}
    try:
        with bpy.context.temp_override(area=viewport, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=0.22)
    except Exception as exc:
        log.debug("Could not create default Animora area: %s", exc)
        return

    created = [area for area in screen.areas if area.as_pointer() not in before]
    if not created:
        return

    try:
        created[0].type = "ANIMORA"
        log.info("Opened Animora AI panel on the left side of the viewport")
    except Exception as exc:
        log.debug("Could not assign Animora area type: %s", exc)


def _layout_timer() -> None:
    _ensure_left_ai_area()
    return None


def _schedule_layout_ensure(*_args) -> None:
    import bpy

    if not bpy.app.timers.is_registered(_layout_timer):
        bpy.app.timers.register(_layout_timer, first_interval=0.8)


def _register_layout_ensure() -> None:
    import bpy
    from bpy.app.handlers import persistent

    global _layout_timer_registered, _load_post_handler

    if "_load_post_handler" not in globals():
        @persistent
        def _load_post_handler(_dummy):
            _schedule_layout_ensure()

        globals()["_load_post_handler"] = _load_post_handler

    if not _layout_timer_registered:
        bpy.app.handlers.load_post.append(globals()["_load_post_handler"])
        _layout_timer_registered = True

    _schedule_layout_ensure()


def _unregister_layout_ensure() -> None:
    import bpy

    global _layout_timer_registered
    handler = globals().get("_load_post_handler")
    if _layout_timer_registered and handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(handler)
    _layout_timer_registered = False
    if bpy.app.timers.is_registered(_layout_timer):
        bpy.app.timers.unregister(_layout_timer)
