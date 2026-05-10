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
    from . import (
        auth,
        operators,
        panel,
        preferences,
        vision,
        ws_client,
    )
    from .ui import chat_display, properties as props_panel

    return [preferences, auth, ws_client, vision, operators, panel, chat_display, props_panel]


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
