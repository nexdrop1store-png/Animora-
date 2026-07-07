"""
Animora Theme — dev wrapper.

The canonical "Refined Indigo" palette and apply logic live in the addon:
    addons/animora_panel/theme.py
which ships with the product and applies itself once per THEME_VERSION
(stamped in AnimoraPreferences.theme_version), so end users get the brand
theme automatically — including over pre-existing grey userprefs.

This script remains for manual dev use: force-apply the theme to the local
Blender/Animora profile and save prefs, regardless of the stamp.

Run: blender --background --python scripts/setup_theme.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import bpy

_THEME_PY = Path(__file__).resolve().parent.parent / "addons" / "animora_panel" / "theme.py"


def _load_theme_module():
    # Load standalone (no package context) so this works both inside an
    # Animora build and against a bare repo checkout.
    spec = importlib.util.spec_from_file_location("animora_theme_dev", _THEME_PY)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["animora_theme_dev"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


theme = _load_theme_module()
theme.apply_refined_indigo()
bpy.ops.wm.save_userpref()
print(f"Animora refined-indigo theme v{theme.THEME_VERSION} saved.")
