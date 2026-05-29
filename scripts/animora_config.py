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
  3. Re-apply patches/animora-native.patch (`git apply --3way` will
     auto-merge unless upstream moved the patch sites).
  4. Run `python scripts/build.py` — the AI panel is copied in fresh
     from addons/animora_panel/ so no merge of the panel is needed.
  5. Smoke-test on a clean Windows VM.

See docs/UPGRADE_BLENDER.md for the full procedure.
"""

from __future__ import annotations

from pathlib import Path

# The Blender release Animora is currently built against.
# Used by every script that needs to refer to the install version dir
# (e.g. `{app}/5.1/scripts/addons_core/animora_panel/`).
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
