"""Shared test setup: make the addon package importable without Blender.

`animora_panel/__init__.py` and the auth submodules pkce/supabase/loopback/
session are import-safe in plain Python (bpy only appears inside functions
or in modules the tests stub explicitly)."""

from __future__ import annotations

import sys
from pathlib import Path

_ADDONS_DIR = Path(__file__).resolve().parent.parent
if str(_ADDONS_DIR) not in sys.path:
    sys.path.insert(0, str(_ADDONS_DIR))
