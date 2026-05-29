"""
Custom icon collection for the Animora AI panel.

Loads PNG glyphs from the addon's `icons/` directory into a bpy preview
collection so they can be drawn in the panel via `icon_value=`.
"""

from __future__ import annotations

import os

import bpy
import bpy.utils.previews

_collections: dict = {}

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
ICON_NAMES = ("icon_chair", "icon_sun", "icon_loop")


def register() -> None:
    pcoll = bpy.utils.previews.new()
    for name in ICON_NAMES:
        path = os.path.join(ICON_DIR, f"{name}.png")
        if os.path.exists(path):
            pcoll.load(name, path, "IMAGE")
    _collections["main"] = pcoll


def unregister() -> None:
    for pcoll in list(_collections.values()):
        bpy.utils.previews.remove(pcoll)
    _collections.clear()


def get_icon(name: str) -> int:
    """Return the icon_id for the named glyph, or 0 if not loaded."""
    pcoll = _collections.get("main")
    if pcoll and name in pcoll:
        return pcoll[name].icon_id
    return 0
