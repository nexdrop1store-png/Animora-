"""
Sync the Animora AI panel source -> installed locations.

The Animora desktop app loads the AI panel from its INSTALLED path, not
from the repo's `addons/animora_panel/`. Editing the source files
doesn't reach the running app until either:
  (a) Animora is rebuilt + reinstalled (slow — full cmake + Inno cycle), or
  (b) the source files are copied into the installed path and the
      panel is disabled/re-enabled in Animora's Preferences.

This script does (b). It's the dev-iteration shortcut.

The canonical source for the AI panel is `addons/animora_panel/` at the
repo root. `scripts/rebrand.py` copies it into the fork tree at build
time; this sync script also pushes it into any already-installed
Animora so dev edits show up without a full rebuild.

Targets synced (whichever exist) — VERSION COMES FROM animora_config.py:
  • build/windows/bin/<X.Y>/scripts/addons_core/animora_panel/
        — the dev build's bin/, populated by `cmake --install`
  • %LOCALAPPDATA%/Programs/Animora/<X.Y>/scripts/addons_core/animora_panel/
        — the Inno-installed Animora's panel path
  • %LOCALAPPDATA%/Programs/Animora/<X.Y>/scripts/addons/animora_panel/
        — fallback path (kept for safety)

After running this:
  1. Restart Animora, OR
  2. In Animora: Edit > Preferences > Add-ons > toggle Animora panel off + on.

Usage:
    python scripts/sync_addon.py                 # uses BLENDER_VERSION from config
    python scripts/sync_addon.py --version 5.2   # override (cross-version testing)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path

from animora_config import AI_PANEL_SRC, BLENDER_VERSION, REPO_ROOT

# Canonical source moved to top-level addons/ in 2026-05-23.
SRC = AI_PANEL_SRC


def build_dests(version: str) -> list[Path]:
    """Compose the candidate destination list for a given Blender version.
    Hyphen-tolerant on Windows; os.path.expandvars resolves %LOCALAPPDATA%."""
    local = os.path.expandvars(r"%LOCALAPPDATA%")
    return [
        REPO_ROOT / "build" / "windows" / "bin" / version / "scripts" / "addons_core" / "animora_panel",
        Path(local) / "Programs" / "Animora" / version / "scripts" / "addons_core" / "animora_panel",
        Path(local) / "Programs" / "Animora" / version / "scripts" / "addons" / "animora_panel",
        # Staging path — kept warm so a later installer build picks up dev edits.
        REPO_ROOT / "build" / "windows" / "animora-stage" / version / "scripts" / "addons_core" / "animora_panel",
    ]


def sync_dir(src: Path, dst: Path) -> tuple[int, int, int]:
    """Mirror src/* → dst/*, returns (copied, unchanged, deleted).

    This is a MIRROR, not a plain copy: destination files that no longer
    exist in the source are removed. Without that, a deleted module (e.g.
    the old auth.py, replaced by the auth/ package) lingers in the install
    and shadows its replacement at import time."""
    if not dst.parent.exists():
        return 0, 0, 0  # parent doesn't exist → that install location isn't present
    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    unchanged = 0
    for src_path in src.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(src)
        dst_path = dst / rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        # Skip if identical (size + mtime). Cheap heuristic; good enough.
        if dst_path.exists():
            s = src_path.stat()
            d = dst_path.stat()
            if s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) < 1.0:
                unchanged += 1
                continue

        shutil.copy2(src_path, dst_path)
        copied += 1

    # Delete destination files with no source counterpart (bundle_config.json
    # is spared: the recording build drops it beside the addon post-install).
    deleted = 0
    keep = {"bundle_config.json"}
    for dst_path in list(dst.rglob("*")):
        if dst_path.is_dir() or "__pycache__" in dst_path.parts:
            continue
        rel = dst_path.relative_to(dst)
        if dst_path.name in keep:
            continue
        if not (src / rel).exists():
            dst_path.unlink(missing_ok=True)
            deleted += 1

    # Remove .pyc caches (and now-empty dirs) so Blender rebuilds cleanly
    for pyc in dst.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    for sub in sorted((p for p in dst.rglob("*") if p.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            sub.rmdir()  # only succeeds when empty

    return copied, unchanged, deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync the Animora AI panel into installed locations")
    parser.add_argument(
        "--version", default=BLENDER_VERSION,
        help=f"Blender install dir version (default: {BLENDER_VERSION} from animora_config.py)",
    )
    args = parser.parse_args()

    if not SRC.is_dir():
        print(f"ERROR: source not found: {SRC}")
        return 1

    print(f"Syncing from {SRC} (version={args.version})")
    print()

    any_synced = False
    for dst in build_dests(args.version):
        # Show what we're targeting even if it doesn't exist (helpful)
        marker = "  [exists]" if dst.exists() else "  [skip — destination tree missing]"
        print(f"-> {dst}{marker}")
        if not dst.parent.parent.exists():
            continue
        copied, unchanged, deleted = sync_dir(SRC, dst)
        if copied or unchanged or deleted:
            print(f"     copied={copied}  unchanged={unchanged}  deleted={deleted}")
            if copied or deleted:
                any_synced = True
        print()

    if not any_synced:
        print("No destinations updated. Either Animora isn't installed yet,")
        print("or the source already matches every destination.")
        return 0

    print("Done. Next steps:")
    print("  1. Restart Animora (close + relaunch), OR")
    print("  2. Edit > Preferences > Add-ons > toggle Animora off then on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
