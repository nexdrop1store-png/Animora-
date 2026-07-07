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

from animora_config import AI_PANEL_SRC, ANIMORA_VERSION, ai_panel_fork_dest

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
    # Unsupported-GPU error dialog (GHOST_WindowWin32.cc) — the Windows
    # MessageBox an old-GPU machine pops up. Title + ARM-branch prose.
    ("Blender - Unsupported Graphics Card Configuration",
     "Animora - Unsupported Graphics Card Configuration"),
    ("emulated x64 copy of Blender", "emulated x64 copy of Animora"),
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
    ('"Blender is free software"',         '"Animora is creative software"'),
    ('"Licensed under the GNU General Public License"',
     '"Built on open-source technology and licensed components"'),
    ('"Blender Website"',                  '"Animora Website"'),
    ('"Blender Store"',                    '"Animora Store"'),
    ('"The reference manual for this version of Blender"',
     '"The reference manual for this version of Animora"'),
    ('"Read about what\'s new in this version of Blender"',
     '"Read about what\'s new in this version of Animora"'),
    ('"Lists committers to Blender\'s source code"',
     '"Lists contributors to Animora"'),
    ('return "blender-" + version.replace(" ", "-").lower()',
     'return "animora-" + version.replace(" ", "-").lower()'),
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
    # 2026-07 coverage expansion — found by grepping the actual fork tree for
    # exact Python-side wording that didn't match any rule above (this table
    # is plain-substring, so near-miss wording silently passes through).
    ('"Blender Version"', '"Animora Version"'),
    ('"A restart of Blender is required"', '"A restart of Animora is required"'),
    ('"Open blend files with this Blender version"',
     '"Open blend files with this Animora version"'),
    ('"Blender IDs"', '"Animora IDs"'),
    ('"Load Factory Blender Settings"', '"Load Factory Animora Settings"'),
    ("\"Blender's official web-site\"", "\"Animora's official web-site\""),
    ('iface_("Import Blender {:d}.{:d} Preferences", "Operator")',
     'iface_("Import Animora {:d}.{:d} Preferences", "Operator")'),
    ('"The API reference manual for this version of Blender"',
     '"The API reference manual for this version of Animora"'),
    ('"Force reload the image if it is already opened elsewhere in Blender"',
     '"Force reload the image if it is already opened elsewhere in Animora"'),
    ('"Pixels per Blender Unit"', '"Pixels per Animora Unit"'),
    ('"Scale based on pixels per Blender Unit"',
     '"Scale based on pixels per Animora Unit"'),
    ('"Number of pixels per inch or Blender Unit"',
     '"Number of pixels per inch or Animora Unit"'),
    ('"the path in User Preferences > File is valid, and Blender has rights to launch it"',
     '"the path in User Preferences > File is valid, and Animora has rights to launch it"'),
    ('"Show Blender files in the File Browser"',
     '"Show Animora files in the File Browser"'),
    ('"Blender sub-process exited with error code {:d}"',
     '"Animora sub-process exited with error code {:d}"'),
    ('"Blender\'s extension repository not found!"',
     '"Animora\'s extension repository not found!"'),
    ('"Blender\'s extension repository must be enabled to install extensions!"',
     '"Animora\'s extension repository must be enabled to install extensions!"'),
    ('"Blender\'s extension repository must be refreshed!"',
     '"Animora\'s extension repository must be refreshed!"'),
    # NOTE: the generic "blender.org" -> "animora.tech" rule above ALSO fires on
    # bl_extension_ui.py's "...now available from extensions.blender.org", turning
    # it into "extensions.animora.tech" — correctly worded, but Animora almost
    # certainly has no extensions marketplace at that domain. Same class of issue
    # as the crash-dialog bug-report URL: a real infra/product decision (keep
    # pointing at Blender's actual Extensions Platform vs. standing up an Animora
    # one), not a string-substitution bug. Flagging, not deciding, here.
    # NOT renamed (checked, deliberately left alone):
    #   bl_operators/userpref.py:517 "This script was written for Blender
    #     version {:d}.{:d}.{:d}..." — genuine addon-compatibility metadata
    #     (bl_info["blender"]) about a THIRD-PARTY ADDON's declared target
    #     Blender version, not our branding. Renaming would be factually wrong.
    #   bl_operators/wm.py:3380 "Blender Dark" — matches the real theme
    #     preset filename Blender_Dark.xml (scripts/presets/interface_theme/).
    #     Renaming only this fallback label would desync from the preset's
    #     own file-derived display name the moment it's actually selected.
    #     Fixing this properly means renaming the .xml presets themselves
    #     (Blender_Dark.xml/Blender_Light.xml) — separate task, not done here.
]

# Source file globs whose string content we patch
SOURCE_GLOBS: list[str] = [
    "source/blender/blenkernel/*.c",
    "source/blender/blenkernel/*.cc",
    # Scoped to this one file, NOT "blenlib/intern/*.cc" (147 files) — that
    # broader glob also matches noise.cc's algorithm-citation URL comment
    # (blender.org rewritten to animora.tech in a doc-reference link, which
    # would then point nowhere). system_win32.cc is the only file in this
    # directory with a real user-facing string (the crash dialog headline).
    "source/blender/blenlib/intern/system_win32.cc",
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

# Python UI files where user-visible Blender links live. SOURCE_GLOBS above is
# C/C++ ONLY, so these were never patched — which is why the splash
# "Donate to Blender"/"What's New" buttons and the blender.org Help links
# survived every prior rebrand.
UI_PY_FILES: list[str] = [
    "scripts/startup/bl_operators/wm.py",      # WM_MT_splash (splash footer)
    "scripts/startup/bl_ui/space_topbar.py",   # Help menu links
    "scripts/startup/bl_ui/space_userpref.py",       # status bar / GPU / file-assoc labels
    "scripts/startup/bl_ui/space_filebrowser.py",    # asset filter labels
    "scripts/startup/bl_operators/image_as_planes.py",  # Import Images as Planes tooltips
    "scripts/startup/bl_operators/image.py",         # external editor launch error
    "scripts/startup/bl_operators/file.py",          # File Browser filter tooltip
    "scripts/startup/bl_operators/userpref.py",      # addon compatibility / asset sub-process
    "scripts/startup/bl_operators/assets.py",        # asset library refresh sub-process
    "scripts/addons_core/bl_pkg/bl_extension_ui.py", # Get Extensions repo-error messages
]

# Targeted UI fixes the generic string table cannot do: the splash uses
# `url_open_preset` with `.type = 'FUND'/'RELEASE_NOTES'/'BLENDER'`, which
# resolve to blender.org regardless of the button label — so we rewrite the
# whole operator call to a direct animora.tech url_open. Plus blanket
# blender.org → animora.tech repoints for any remaining Help links.
UI_PY_REPLACEMENTS: list[tuple[str, str]] = [
    ('"wm.url_open_preset", text="Donate to Blender", icon=\'FUND\').type = \'FUND\'',
     '"wm.url_open", text="Support Animora", icon=\'FUND\').url = "https://animora.tech"'),
    ('"wm.url_open_preset", text="What\'s New", icon=\'URL\').type = \'RELEASE_NOTES\'',
     '"wm.url_open", text="What\'s New", icon=\'URL\').url = "https://animora.tech/whats-new"'),
    ('"wm.url_open_preset", text="Blender Website", icon=\'URL\').type = \'BLENDER\'',
     '"wm.url_open", text="Animora Website", icon=\'URL\').url = "https://animora.tech"'),
    ("https://www.blender.org/support/", "https://animora.tech/support"),
    ("https://www.blender.org/support", "https://animora.tech/support"),
    ("https://www.blender.org/community/", "https://animora.tech/community"),
    ("https://www.blender.org/get-involved/", "https://animora.tech"),
    ("https://devtalk.blender.org", "https://animora.tech"),
    ("https://www.blender.org", "https://animora.tech"),
    # Topbar app-menu button (left of "File"): drop the Blender logo icon —
    # Animora must not expose Blender branding anywhere in the UI.
    ('layout.menu("TOPBAR_MT_blender", text="", icon=\'BLENDER\')',
     'layout.menu("TOPBAR_MT_blender", text="Animora")'),
]

# ---------------------------------------------------------------------------
# Product version: the About dialog + splash corner must show ANIMORA's
# version (animora_config.ANIMORA_VERSION), not Blender's compiled 5.x.x.
# Each entry: (file, [match alternatives], replacement). The FIRST alternative
# matches pristine upstream source; the SECOND is a sentinel matching our own
# previous output, so a later ANIMORA_VERSION bump re-applies over a fork tree
# that was already rebranded. A file matching NEITHER means upstream reshaped
# the code after a Blender upgrade — patch_product_version() warns loudly.
# ---------------------------------------------------------------------------
PRODUCT_VERSION_PATCHES: list[tuple[str, list[str], str]] = [
    (
        # About dialog (WM_MT_splash_about): "Version: 5.1.1" → "Version 1.0"
        "scripts/startup/bl_operators/wm.py",
        [
            r'text=iface_\("Version: \{:s\}"\)\.format\(bpy\.app\.version_string\)',
            r'text="Version [0-9][^"]*"',
        ],
        f'text="Version {ANIMORA_VERSION}"',
    ),
    (
        # Splash corner label: BKE_blender_version_string() → "1.0"
        "source/blender/windowmanager/intern/wm_splash_screen.cc",
        [
            r"BKE_blender_version_string\(\)",
            r'"[0-9][^"]*" /\*ANIMORA_VERSION\*/',
        ],
        f'"{ANIMORA_VERSION}" /*ANIMORA_VERSION*/',
    ),
]

# Asset copy map: src (relative to assets/branding) → dst (relative to fork root)
ASSET_COPIES: list[tuple[str, str]] = [
    ("splash.png", "release/datafiles/splash.png"),
    ("splash_2x.png", "release/datafiles/splash_2x.png"),
    # Source names match the actual files in assets/branding/ (animora_<n>.png,
    # NOT animora_icon_<n>.png — that earlier mismatch silently skipped every
    # icon, leaving Blender's icons in place).
    ("animora_16.png", "release/datafiles/icons/animora_16.png"),
    ("animora_32.png", "release/datafiles/icons/animora_32.png"),
    ("animora_64.png", "release/datafiles/icons/animora_64.png"),
    ("animora_128.png", "release/datafiles/icons/animora_128.png"),
    ("animora_256.png", "release/datafiles/icons/animora_256.png"),
    # The .exe's embedded icon is NOT copied here. winblender.rc's APPICON
    # resource references winblender.ico by exact filename (confirmed via
    # grep — nothing in the build reads a file named "blender.ico"), and
    # that file + winblenderfile.ico + winblender.rc are already branded
    # directly in the fork tree (captured by the native patch in
    # patches/animora-native-full.patch, not by this copy step). A prior
    # version of this table copied animora.ico to a "blender.ico" path
    # that nothing consumed — dead weight, removed rather than renamed.
    # animora.icns (macOS dock icon) intentionally omitted: not in the repo yet
    # (macOS packaging is later). The Animora dark theme ("Refined Indigo") ships
    # inside the AI panel addon — addons/animora_panel/theme.py applies it once
    # per THEME_VERSION at startup — so no theme asset is copied here.
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
                    src_file.write_text(patched, encoding="utf-8", newline="\n")
                patched_files += 1
    return patched_files


def patch_ui_python(fork_root: Path, dry_run: bool) -> int:
    """Patch the Python UI files (splash + Help menu) the C-only SOURCE_GLOBS
    never touched: applies the generic Blender→Animora table plus the
    splash-specific preset rewrites. This is what removes 'Donate to Blender'
    / repoints 'What's New' + the blender.org Help links."""
    patched_files = 0
    for rel in UI_PY_FILES:
        src_file = fork_root / rel
        if not src_file.is_file():
            log.warning("UI file not found (skipping): %s", rel)
            continue
        original = src_file.read_text(encoding="utf-8", errors="replace")
        patched = original
        for old, new in STRING_REPLACEMENTS:
            patched = patched.replace(old, new)
        for old, new in UI_PY_REPLACEMENTS:
            patched = patched.replace(old, new)
        if patched != original:
            log.info("Patch UI python: %s", rel)
            if not dry_run:
                src_file.write_text(patched, encoding="utf-8", newline="\n")
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


def patch_product_version(fork_root: Path, dry_run: bool) -> int:
    """Show ANIMORA_VERSION in the About dialog + splash corner instead of
    Blender's compiled version. Regex with a sentinel alternative so a later
    version bump re-applies over an already-rebranded fork tree (see
    PRODUCT_VERSION_PATCHES). Returns the number of files patched."""
    patched_files = 0
    for rel, patterns, replacement in PRODUCT_VERSION_PATCHES:
        src_file = fork_root / rel
        if not src_file.is_file():
            log.warning("Version-patch target not found (skipping): %s", rel)
            continue
        original = src_file.read_text(encoding="utf-8", errors="replace")
        patched = original
        matched = False
        for pattern in patterns:
            if re.search(pattern, patched):
                matched = True
                patched = re.sub(pattern, replacement.replace("\\", "\\\\"), patched)
                break
        if not matched:
            log.warning(
                "Version patch matched NOTHING in %s — upstream layout changed "
                "after a Blender upgrade? The About/splash will show Blender's "
                "version until PRODUCT_VERSION_PATCHES is updated.", rel,
            )
            continue
        if patched != original:
            log.info("Patch product version (%s): %s", ANIMORA_VERSION, rel)
            if not dry_run:
                src_file.write_text(patched, encoding="utf-8", newline="\n")
            patched_files += 1
        else:
            log.debug("Product version already current in %s", rel)
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
            cmake.write_text(patched, encoding="utf-8", newline="\n")


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
    ui_patched = patch_ui_python(args.fork_root, args.dry_run)
    version_patched = patch_product_version(args.fork_root, args.dry_run)
    patch_cmake_app_name(args.fork_root, args.dry_run)

    log.info(
        "Done. Assets copied: %d  AI panel files: %d  Source patched: %d  "
        "UI py patched: %d  Version patched: %d",
        copied, panel_files, patched, ui_patched, version_patched,
    )


if __name__ == "__main__":
    main()
