"""
Animora AI Panel — Blender addon entry point.

Registers all operators, panels, and preferences. Auto-loaded when
Animora launches (set in default userpref.blend).
"""

from __future__ import annotations

bl_info = {
    "name": "Animora AI Panel",
    "author": "Animora Technologies",
    "version": (0, 1, 0),
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

# Sub-modules registered in order
_MODULES = [
    "preferences",
    "auth",
    "ws_client",
    "vision",
    "operators",
    "panel",
    "ui.chat_display",
    "ui.properties",
]

_loaded: list = []


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
        operators,
        panel,
        preferences,
        preview_icons,
        state,
        vision,
        ws_client,
    )
    from .ui import chat_display, properties as props_panel

    # Registration order matters:
    #   - preview_icons before panel (panel uses icon_value lookups)
    #   - state before panel (panel reads state on draw)
    #   - panel before border_glow / ads (both need SpaceAnimora active)
    #   - bundle LAST: its auto-launch/auto-connect uses preferences, auth,
    #     operators, ws_client — all must be registered first. No-op unless
    #     a bundle_config.json shipped alongside the addon (recording build).
    return [preferences, auth, ws_client, vision, preview_icons, state,
            operators, panel, border_glow, ads, chat_display, props_panel,
            bundle]


def register() -> None:
    import bpy

    log.info("Animora AI Panel v%s loading", ".".join(str(v) for v in bl_info["version"]))

    global _loaded
    _loaded = _import_modules()

    for mod in _loaded:
        if hasattr(mod, "register"):
            mod.register()

    log.info("Animora AI Panel registered")


def unregister() -> None:
    for mod in reversed(_loaded):
        if hasattr(mod, "unregister"):
            mod.unregister()
