"""
Single source of truth for cross-script constants.

Anything that's tied to a specific Blender release lives here so that
bumping Blender (5.1 → 5.2 → ...) is a one-line change instead of a
search-and-replace across the repo. Import from here in build / rebrand
/ sync / staging scripts. Inno Setup pulls the same value via the
`scripts/print_blender_version.py` shim invoked at compile time.

When you upgrade to a new Blender version:
  1. Bump BLENDER_VERSION below.
  2. Re-fetch blender-fork at the new tag (`git checkout v5.2.0` etc.).
  3. Re-apply patches/animora-native-full.patch (`git apply --ignore-whitespace`)
     then overlay patches/native-overlay/* — auto-merge unless upstream
     moved the patch sites.
  4. Run `python scripts/build.py` — the AI panel is copied in fresh
     from addons/animora_panel/ so no merge of the panel is needed.
  5. Smoke-test on a clean Windows VM.

See docs/UPGRADE_BLENDER.md for the full procedure.
"""

from __future__ import annotations

from pathlib import Path

# ── Animora PRODUCT version (what users see) ────────────────────────────
# This is Animora's own version, shown in the installer, window title,
# About box, and splash. It is DELIBERATELY separate from the Blender base
# version below: users should never see "5.1". Bump this for Animora
# releases (V1 = 1.x); it has nothing to do with the Blender install dir.
#
# Bump in LOCKSTEP with two other files on every release:
#   - installer/windows/inno/Animora.iss's #define MyAppVersion
#   - addons/animora_panel/__init__.py's bl_info["version"] tuple —
#     this one matters functionally, not just cosmetically: it's the
#     only one of the three actually shipped inside the installed
#     addon, so addons/animora_panel/updater.py reads it at runtime to
#     decide whether a published release is newer. Letting it drift
#     makes the in-app update check silently wrong.
ANIMORA_VERSION = "1.3"

# The Blender release Animora is currently built against.
# Used ONLY for the internal install version dir Blender requires
# (e.g. `{app}/5.1/scripts/addons_core/animora_panel/`) — NOT user-facing.
BLENDER_VERSION = "5.1"

# The patch version (third component) is allowed to drift for security
# fixes from upstream without changing the install dir layout — Blender
# nests scripts under MAJOR.MINOR only.
BLENDER_FULL_VERSION = "5.1.1"

# Anchor paths derived once so callers don't reinvent them.
REPO_ROOT = Path(__file__).resolve().parent.parent
FORK_ROOT = REPO_ROOT / "blender-fork"
ADDONS_SRC = REPO_ROOT / "addons"
AI_PANEL_SRC = ADDONS_SRC / "animora_panel"

# Where the AI panel gets injected into the fork at build time. This
# path follows Blender's bundled-addon convention; the addon ships
# inside Animora.exe's install dir, NOT in %APPDATA% per-user.
def ai_panel_fork_dest(fork_root: Path = FORK_ROOT) -> Path:
    return fork_root / "scripts" / "addons_core" / "animora_panel"
