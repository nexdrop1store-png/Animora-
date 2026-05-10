"""
Non-destructive Blender → Animora rebrand script.

Copies Animora assets into the blender-fork build staging area and patches
string constants in C source files. Designed to run before every cmake
invocation. Original tracked files in blender-fork/ are never modified.

Usage:
    python scripts/rebrand.py [--fork-root PATH] [--assets-root PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("rebrand")

REPO_ROOT = Path(__file__).resolve().parent.parent
FORK_ROOT_DEFAULT = REPO_ROOT / "blender-fork"
ASSETS_ROOT_DEFAULT = REPO_ROOT / "assets" / "branding"

# ---------------------------------------------------------------------------
# String replacement map: (pattern, replacement) applied to C/C++ source
# All replacements are plain string literals — no regex — to stay predictable.
# ---------------------------------------------------------------------------
STRING_REPLACEMENTS: list[tuple[str, str]] = [
    # App name visible to user (exact quoted string)
    ('"Blender"', '"Animora"'),
    ("'Blender'", "'Animora'"),
    # About / version strings
    ("Blender Foundation", "Animora Technologies"),
    ("www.blender.org", "animora.tech"),
    ("https://www.blender.org", "https://animora.tech"),
    ("blender.org", "animora.tech"),
    # Window title (wm_window.cc fmt::format call)
    ('" - Blender {}"', '" - Animora {}"'),
    # Version/help output (creator_args.cc printf format strings)
    ('"Blender %s\\n"', '"Animora %s\\n"'),
    ('"Blender %s (hash %s built %s %s)\\n"', '"Animora %s (hash %s built %s %s)\\n"'),
    # Menu / operator names
    ('"Quit Blender"', '"Quit Animora"'),
    ('"Blender Animation Player"', '"Animora Animation Player"'),
    ('"Blender - "', '"Animora - "'),
    ('"Blender File View"', '"Animora File View"'),
    ('"Open a Blender file"', '"Open an Animora file"'),
    ('"Save Blender File"', '"Save Animora File"'),
    ('"Save the current Blender file"', '"Save the current Animora file"'),
    ('"Load Factory Blender Preferences"', '"Load Factory Animora Preferences"'),
    ('"Blender will start next time as it is now."',
     '"Animora will start next time as it is now."'),
    # Signal handler messages
    ('"\\nBlender killed\\n"', '"\\nAnimora killed\\n"'),
    ('"\\nSent an internal break event. Press ^C again to kill Blender\\n"',
     '"\\nSent an internal break event. Press ^C again to kill Animora\\n"'),
]

# Source file globs whose string content we patch
SOURCE_GLOBS: list[str] = [
    "source/blender/blenkernel/*.c",
    "source/blender/blenkernel/*.cc",
    "source/blender/editors/space_info/*.c",
    "source/blender/editors/space_info/*.cc",
    "source/blender/windowmanager/intern/*.c",
    "source/blender/windowmanager/intern/*.cc",
    "source/creator/*.c",
    "source/creator/*.cc",
    "intern/ghost/intern/*.cpp",
]

# Asset copy map: src (relative to assets/branding) → dst (relative to fork root)
ASSET_COPIES: list[tuple[str, str]] = [
    ("splash.png", "release/datafiles/splash.png"),
    ("splash_2x.png", "release/datafiles/splash_2x.png"),
    ("animora_icon_16.png", "release/datafiles/icons/animora_16.png"),
    ("animora_icon_32.png", "release/datafiles/icons/animora_32.png"),
    ("animora_icon_64.png", "release/datafiles/icons/animora_64.png"),
    ("animora_icon_128.png", "release/datafiles/icons/animora_128.png"),
    ("animora_icon_256.png", "release/datafiles/icons/animora_256.png"),
    ("animora.ico", "release/windows/icons/blender.ico"),
    ("animora.icns", "release/datafiles/animora.icns"),
    ("animora_theme.xml", "release/datafiles/animora_theme.xml"),
]

STARTUP_COPY = (
    REPO_ROOT / "assets" / "startup" / "startup.blend",
    FORK_ROOT_DEFAULT / "release" / "datafiles" / "startup.blend",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def copy_assets(fork_root: Path, assets_root: Path, dry_run: bool) -> int:
    copied = 0
    for src_rel, dst_rel in ASSET_COPIES:
        src = assets_root / src_rel
        dst = fork_root / dst_rel
        if not src.exists():
            log.warning("Asset not found (skipping): %s", src)
            continue
        if dst.exists() and _sha256(src) == _sha256(dst):
            log.debug("Unchanged: %s", dst_rel)
            continue
        log.info("Copy asset: %s → %s", src_rel, dst_rel)
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied += 1

    # Startup .blend
    src_startup, dst_startup = STARTUP_COPY
    if src_startup.exists():
        if not dst_startup.exists() or _sha256(src_startup) != _sha256(dst_startup):
            log.info("Copy startup.blend")
            if not dry_run:
                dst_startup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_startup, dst_startup)
            copied += 1

    return copied


def patch_sources(fork_root: Path, dry_run: bool) -> int:
    patched_files = 0
    for glob in SOURCE_GLOBS:
        for src_file in fork_root.glob(glob):
            original = src_file.read_text(encoding="utf-8", errors="replace")
            patched = original
            for old, new in STRING_REPLACEMENTS:
                patched = patched.replace(old, new)
            if patched != original:
                log.info("Patch strings: %s", src_file.relative_to(fork_root))
                if not dry_run:
                    src_file.write_text(patched, encoding="utf-8")
                patched_files += 1
    return patched_files


def patch_cmake_app_name(fork_root: Path, dry_run: bool) -> None:
    """Patch CMakeLists.txt to set executable name to 'animora'."""
    cmake = fork_root / "CMakeLists.txt"
    if not cmake.exists():
        log.warning("CMakeLists.txt not found at %s", cmake)
        return
    text = cmake.read_text(encoding="utf-8")
    patched = re.sub(
        r'set\(EXECUTABLE_NAME\s+"blender"\)',
        'set(EXECUTABLE_NAME "animora")',
        text,
    )
    patched = re.sub(
        r'set\(EXECUTABLE_NAME\s+blender\)',
        'set(EXECUTABLE_NAME animora)',
        patched,
    )
    if patched != text:
        log.info("Patch CMakeLists.txt: EXECUTABLE_NAME → animora")
        if not dry_run:
            cmake.write_text(patched, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebrand Blender fork as Animora")
    parser.add_argument("--fork-root", type=Path, default=FORK_ROOT_DEFAULT)
    parser.add_argument("--assets-root", type=Path, default=ASSETS_ROOT_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Preview changes, write nothing")
    args = parser.parse_args()

    if not args.fork_root.exists():
        log.error(
            "blender-fork not found at %s. Run: git submodule update --init",
            args.fork_root,
        )
        sys.exit(1)

    log.info("=== Animora Rebrand%s ===", " (DRY RUN)" if args.dry_run else "")
    log.info("Fork root : %s", args.fork_root)
    log.info("Assets    : %s", args.assets_root)

    copied = copy_assets(args.fork_root, args.assets_root, args.dry_run)
    patched = patch_sources(args.fork_root, args.dry_run)
    patch_cmake_app_name(args.fork_root, args.dry_run)

    log.info("Done. Assets copied: %d  Source files patched: %d", copied, patched)


if __name__ == "__main__":
    main()
