"""
Non-destructive Blender → Animora rebrand script.

Copies Animora assets + the Animora AI panel source into the blender-fork
build staging area and patches string constants in C source files.
Designed to run before every cmake invocation. The fork tree is treated
as a BUILD ARTIFACT, not a source of truth — anything Animora-specific
that lives there at build time is copied in by this script. That means
upgrading to a new Blender release is "re-checkout the fork at the new
tag and re-run rebrand"; we never have to manually merge the AI panel.

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

from animora_config import AI_PANEL_SRC, ai_panel_fork_dest

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
    # AppData / ProgramData path (GHOST_SystemPathsWin32.cc). MUST come
    # before the generic "Blender Foundation" rule below, because that
    # rule would otherwise produce "\\Animora Technologies\\Blender\\"
    # — half-renamed and still user-visible in %APPDATA%. Both source
    # and target are length-preserving so the C++ stays clean.
    ('"\\\\Blender Foundation\\\\Blender\\\\"',
     '"\\\\Animora Technologies\\\\Animora\\\\"'),
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
    # User-visible "Blender file" strings (kept loader compat via ext_test array)
    ('"the Blender file"',              '"the Animora file"'),
    ('"Blender file"',                  '"Animora file"'),
    ('"blend-file"',                    '"anim-file"'),
    ('"a blend file"',                  '"an Animora file"'),
    ('"Choose a Blender"',              '"Choose an Animora"'),
    ('"open Blender"',                  '"open Animora"'),
    ('"This Blender"',                  '"This Animora"'),
    ('"Blender installation"',          '"Animora installation"'),
    ('"the Blender installation"',      '"the Animora installation"'),
    ('"Blender configuration"',         '"Animora configuration"'),
    # Asset library / file dialog tooltips
    ('"Full path to the Blender file"', '"Full path to the Animora file"'),
    ('"Full path to the Blender file containing the active asset"',
     '"Full path to the Animora file containing the active asset"'),
    # Status / info messages
    ('"Blender is now starting"',       '"Animora is now starting"'),
    ('"Restart Blender"',               '"Restart Animora"'),
    # IFACE_-wrapped UI labels (screen_ops, io_usd, render_view)
    ('IFACE_("Blender Version")',          'IFACE_("Animora Version")'),
    ('IFACE_("Blender Drivers Editor")',   'IFACE_("Animora Drivers Editor")'),
    ('IFACE_("Blender Info Log")',         'IFACE_("Animora Info Log")'),
    ('IFACE_("Blender Data")',             'IFACE_("Animora Data")'),
    ('IFACE_("Blender Render")',           'IFACE_("Animora Render")'),
    # Operator names + descriptions
    ('"About Blender"',                    '"About Animora"'),
    ('"Open a window with information about Blender"',
     '"Open a window with information about Animora"'),
    ('"Capture a picture of the whole Blender window"',
     '"Capture a picture of the whole Animora window"'),
    ('"Update the display of reports in Blender UI"',
     '"Update the display of reports in Animora UI"'),
    ('"Sample a color from the Blender window"',
     '"Sample a color from the Animora window"'),
    ('"Exit Blender after saving"',        '"Exit Animora after saving"'),
    # Error / status / system messages
    ('L"Blender has stopped working"',     'L"Animora has stopped working"'),
    ('"Blender requires a CPU with SSE42"','"Animora requires a CPU with SSE42"'),
    ('L"Blender Thumbnail Handler"',       'L"Animora Thumbnail Handler"'),
    # Eyedropper variants
    ('"Sample a color from the Blender Window"',
     '"Sample a color from the Animora Window"'),
]

# Source file globs whose string content we patch
SOURCE_GLOBS: list[str] = [
    "source/blender/blenkernel/*.c",
    "source/blender/blenkernel/*.cc",
    "source/blender/editors/space_info/*.c",
    "source/blender/editors/space_info/*.cc",
    "source/blender/editors/space_view3d/*.cc",
    "source/blender/editors/screen/*.cc",
    "source/blender/editors/interface/*.cc",
    "source/blender/editors/io/*.cc",
    "source/blender/editors/render/*.cc",
    "source/blender/editors/util/*.cc",
    "source/blender/windowmanager/intern/*.c",
    "source/blender/windowmanager/intern/*.cc",
    "source/blender/blenloader/intern/*.cc",
    "source/blender/io/usd/*.cc",
    "source/blender/python/intern/*.cc",
    "source/creator/*.c",
    "source/creator/*.cc",
    "intern/ghost/intern/*.cpp",
    "intern/ghost/intern/*.cc",
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


def inject_ai_panel(fork_root: Path, dry_run: bool) -> int:
    """Copy the canonical AI panel source (addons/animora_panel/) into the
    fork's built-in addons dir. Overwrites whatever is there — the fork
    copy is a build artifact. Returns the number of files copied.

    Skips __pycache__ to keep the staged tree clean (CPython recreates
    these at runtime anyway).
    """
    src = AI_PANEL_SRC
    dst = ai_panel_fork_dest(fork_root)

    if not src.is_dir():
        log.error("AI panel source missing: %s. Animora cannot ship.", src)
        sys.exit(1)

    log.info("Inject AI panel: %s → %s", src.relative_to(REPO_ROOT), dst.relative_to(fork_root))
    if dry_run:
        return sum(1 for _ in src.rglob("*") if _.is_file() and "__pycache__" not in _.parts)

    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return sum(1 for _ in dst.rglob("*") if _.is_file())


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
    panel_files = inject_ai_panel(args.fork_root, args.dry_run)
    patched = patch_sources(args.fork_root, args.dry_run)
    patch_cmake_app_name(args.fork_root, args.dry_run)

    log.info(
        "Done. Assets copied: %d  AI panel files: %d  Source files patched: %d",
        copied, panel_files, patched,
    )


if __name__ == "__main__":
    main()
