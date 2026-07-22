"""
Animora installer staging script.

Reads build/windows/bin/  →  writes build/windows/animora-stage/  with:
  • Blender-named USER-VISIBLE files renamed to Animora-named files
    (the .exe itself, launcher, debug .cmd scripts, shell thumbnailer)
  • Blender-named FILES/FOLDERS THAT THE EXE REFERENCES BY NAME are
    renamed by `rename_runtime_assemblies()`:
        - blender_cpu_check.dll  → animora_cpu_check.dll
        - blender.shared/        → animora.shared/  (SxS private assembly: third-party libs)
        - blender.crt/           → animora.crt/     (SxS private assembly: VC++ runtime)
    The corresponding embedded references inside Animora.exe and
    Animora-launcher.exe are then binary-patched in place. The byte
    sequences `blender.crt`, `blender.shared`, `blender_cpu_check` each
    appear exactly once per PE (verified at staging time) — they live
    in the embedded XML manifest / IAT name string. The replacement
    strings are LENGTH-PRESERVING (animora.crt = blender.crt = 11
    bytes, animora.shared = blender.shared = 14 bytes, etc.) so no PE
    offsets shift and no signature regeneration is required.
  • Debug symbols (*.pdb), linker artifacts (*.exp, *.lib), and
    build-time tools (datatoc.exe, makesdna.exe, etc.) are EXCLUDED.
  • Windows system DLLs that leak into build/windows/bin/ from
    `C:\\Windows\\System32` (Microsoft's Mesa3D-on-Windows shim stack:
    opengl32.dll, libEGL.dll, vulkan_lvp.dll, vulkan_dzn.dll, etc.) are
    EXCLUDED. CRITICAL: shipping opengl32.dll next to Animora.exe makes
    Windows load OUR copy (DLL search order: app dir first) instead of
    the GPU driver's ICD, forcing Microsoft software OpenGL 1.1 → the
    "OpenGL 4.3 or higher required" error on machines that otherwise
    run vanilla Blender 5.1 fine. See SYSTEM_DLL_DENYLIST below.
  • Both SxS private-assembly manifests (blender.crt.manifest and
    blender.shared.manifest) are patched in-place to add the missing
    `processorArchitecture="amd64"` attribute. Without this, Windows
    rejects the activation context with ERROR_SXS_CANT_GEN_ACTCTX
    (14001) on 64-bit systems where the parent exe's manifest implies
    amd64 but the assembly's manifest doesn't declare a matching arch.

The Inno Setup script (installer/windows/inno/Animora.iss) sources
from animora-stage/ instead of bin/.

Run:
    python scripts/stage_for_installer.py
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from animora_config import BLENDER_VERSION

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("stage")

REPO_ROOT = Path(__file__).resolve().parent.parent
# The Windows cmake configure uses the multi-config "Visual Studio 17 2022"
# generator (see scripts/build.py CMAKE_PLATFORM_FLAGS), so the compiled
# output lands at build/windows/<config>/bin/<config>/ - the config segment
# appears twice (once for build.py's own build_dir layout, once for the
# generator's own per-config bin/ subfolder). Every build in this repo uses
# Release (CI, docs, build.py's default), so it's not exposed as a CLI flag
# here - override via ANIMORA_BUILD_CONFIG if that ever changes.
import os as _os

_CONFIG = _os.environ.get("ANIMORA_BUILD_CONFIG", "Release")
SRC_DIR = REPO_ROOT / "build" / "windows" / _CONFIG / "bin" / _CONFIG
DST_DIR = REPO_ROOT / "build" / "windows" / "animora-stage"
STARTUP_BLEND_SRC = REPO_ROOT / "blender-fork" / "release" / "datafiles" / "startup.blend"

# Files/dirs to rename. None = exclude.
# Mapping is keyed on basename relative to SRC_DIR (top-level only).
RENAMES: dict[str, str | None] = {
    "blender.exe":                          "Animora.exe",
    "blender-launcher.exe":                 "Animora-launcher.exe",
    # blender_cpu_check.dll — copied as-is here; rename_runtime_assemblies()
    # then renames it to animora_cpu_check.dll and patches Animora.exe's
    # IAT reference. We can't rename it at copy time because the binary
    # patch needs to run on the rename TARGET, not the source.
    "blender_cpu_check.lib":                None,
    "BlendThumb.dll":                       "AnimoraThumb.dll",
    "BlendThumb.lib":                       None,
    # blender.shared/ + blender.crt/ are also handled by
    # rename_runtime_assemblies(): folder rename + .manifest XML edit +
    # binary patch of Animora.exe + Animora-launcher.exe.
    "blender_debug_cycles.cmd":             "Animora_debug_cycles.cmd",
    "blender_debug_gpu.cmd":                "Animora_debug_gpu.cmd",
    "blender_debug_gpu_glitchworkaround.cmd": "Animora_debug_gpu_glitchworkaround.cmd",
    "blender_debug_log.cmd":                "Animora_debug_log.cmd",
    "blender_factory_startup.cmd":          "Animora_factory_startup.cmd",
    "blender_factory_startup_vulkan.cmd":   "Animora_factory_startup_vulkan.cmd",
    "blender_oculus.cmd":                   "Animora_oculus.cmd",
    "blender_startup_opengl.cmd":           "Animora_startup_opengl.cmd",
    "blender_startup_vulkan.cmd":           "Animora_startup_vulkan.cmd",
    "blender_system_info.cmd":              "Animora_system_info.cmd",
    "blender.exp":                          None,    # linker import-library export
    "blender.lib":                          None,    # linker import library
    "blender.pdb":                          None,    # debug symbols, ~130 MB
    "makesdna.pdb":                         None,
    "makesrna.pdb":                         None,
    "msgfmt.pdb":                           None,
    "datatoc.exe":                          None,    # build-time tool, not needed at runtime
    "makesdna.exe":                         None,
    "makesrna.exe":                         None,
    "msgfmt.exe":                           None,
    "shader_tool.exe":                      None,
    "zstd_compress.exe":                    None,
    # Internal scripts that reference 'blender_' inside their content
    # but the filenames themselves don't need renaming
    "BlendThumbCache.bat":                  None,    # build artifact if present
}

# Exclude any file matching these extensions anywhere in the tree
EXCLUDE_EXTENSIONS = {".pdb", ".exp"}
EXCLUDE_BASENAMES = {".gitignore"}

# Filename glob patterns to exclude entirely. The Inno installer sources
# from animora-stage/ so anything missing from staging is guaranteed not
# to ship to end users. We keep this list defensive even though the dev
# files don't currently sit anywhere the staging walk would pick them
# up — future restructures could change that, and a denylist here is
# cheap insurance (security audit M1).
EXCLUDE_GLOBS: tuple[str, ...] = (
    "dev_server.py",   # local-only auth bypass; MUST NOT ship
    "test_*.py",       # smoke tests for ai-backend
    "*_test.py",       # alternate naming
    "test_*.bat",      # any test bat scripts
    ".env",            # secrets — should never be in the staging tree
    ".env.local",
    ".env.production",
    ".env.staging",
    "*.log",           # build / dev logs
)

STAGING_REMOVE_GLOBS: tuple[str, ...] = (
    "Animora_debug_*.cmd",
    "Animora_factory_startup*.cmd",
    "Animora_startup_*.cmd",
    "Animora_system_info.cmd",
    "Animora_oculus.cmd",
    "readme.html",
)

STAGING_REMOVE_DIRS: tuple[str, ...] = (
    "scripts/templates_py",
    "scripts/templates_osl",
    "scripts/templates_toml",
)

# Windows system DLLs that leak into build/windows/bin/ from
# `C:\Windows\System32` (Microsoft's Mesa3D-on-Windows shim — see WSLg /
# DirectX-on-Linux work) and from the system codec stack. Shipping these
# alongside Animora.exe is catastrophic because Windows' DLL search order
# loads from the exe directory BEFORE System32. Three concrete failure
# modes we've seen:
#
#   1. opengl32.dll  — THE root cause of "OpenGL 4.3 or higher required"
#      on machines that run vanilla Blender 5.1 fine. The Mesa
#      opengl32.dll does not chain to the GPU driver's ICD; Blender
#      ends up on Microsoft software GL 1.1 and refuses to start.
#
#   2. vulkan_lvp.dll / vulkan_dzn.dll (+ their *_icd.x86_64.json
#      manifests) — Mesa software-Vulkan ICDs. With the manifests in
#      the exe dir the Vulkan loader picks software rendering even
#      when a real GPU driver is installed.
#
#   3. libEGL / libGLESv* / libgallium_wgl / d3d10warp / dxil /
#      spirv_to_dxil / clon12compiler / openclon12 / VkLayer_MESA_* /
#      msav1enchmft / msh264enchmft / msh265enchmft / va* /
#      vaon12_drv_video — Mesa GLES + Microsoft codec MFTs + libva
#      Windows shim. Not used by Blender, but their presence near
#      Animora.exe can confuse other apps and inflates the installer.
#
# Vanilla Blender 5.1 ships none of these. Strip them aggressively.
SYSTEM_DLL_DENYLIST: frozenset[str] = frozenset({
    # GPU stack — the actual culprit
    "opengl32.dll",
    "libEGL.dll",
    "libGLESv1_CM.dll",
    "libGLESv2.dll",
    "libgallium_wgl.dll",
    "vulkan_lvp.dll",
    "vulkan_dzn.dll",
    "lvp_icd.x86_64.json",
    "dzn_icd.x86_64.json",
    "VkLayer_MESA_anti_lag.dll",
    "VkLayer_MESA_anti_lag.json",
    "d3d10warp.dll",
    "dxil.dll",
    "spirv_to_dxil.dll",
    # OpenCL-on-D3D12 — not used by Blender
    "clon12compiler.dll",
    "openclon12.dll",
    # Windows Media Foundation video MFTs — not used by Blender
    "msav1enchmft.dll",
    "msh264enchmft.dll",
    "msh265enchmft.dll",
    # libva-on-D3D12 — not used by Blender
    "va.dll",
    "va_win32.dll",
    "vaon12_drv_video.dll",
})


def stage() -> int:
    if not SRC_DIR.exists():
        log.error("Source not found: %s", SRC_DIR)
        return 1

    if DST_DIR.exists():
        log.info("Removing existing staging directory: %s", DST_DIR)
        shutil.rmtree(DST_DIR)

    DST_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Staging %s  →  %s", SRC_DIR, DST_DIR)

    file_count = 0
    excluded_count = 0
    renamed_count = 0

    for entry in SRC_DIR.iterdir():
        name = entry.name

        # Defense-in-depth: drop any Windows system-DLL pollution that
        # leaked into build/windows/bin/ from C:\Windows\System32. These
        # MUST NOT ship — see SYSTEM_DLL_DENYLIST docstring above. The
        # critical one is opengl32.dll; the rest are mostly inert but
        # bloat the installer and confuse other apps' DLL resolution.
        if name in SYSTEM_DLL_DENYLIST:
            excluded_count += 1
            log.info("EXCLUDE  %s (system DLL pollution)", name)
            continue

        action = RENAMES.get(name, name)  # default = keep as-is

        if action is None:
            excluded_count += 1
            log.debug("EXCLUDE  %s", name)
            continue

        dst = DST_DIR / action
        if action != name:
            renamed_count += 1
            log.info("RENAME   %s  →  %s", name, action)

        if entry.is_dir():
            shutil.copytree(
                entry, dst,
                ignore=_ignore_factory(),
                dirs_exist_ok=False,
            )
            file_count += sum(1 for _ in dst.rglob("*") if _.is_file())
        else:
            if _should_exclude_file(entry):
                excluded_count += 1
                log.debug("EXCLUDE  %s (extension)", name)
                continue
            shutil.copy2(entry, dst)
            file_count += 1

    log.info("Done. Files staged: %d  Excluded: %d  Renamed: %d",
             file_count, excluded_count, renamed_count)

    # Sanity check — after rename_runtime_assemblies() runs, no
    # top-level item should still contain "blender" in its name.
    leaked = [p.name for p in DST_DIR.iterdir() if "blender" in p.name.lower()]
    if leaked:
        log.warning(
            "Top-level files still contain 'blender' in name (unexpected — "
            "rename_runtime_assemblies should have caught these): %s", leaked
        )
    else:
        log.info("OK: top-level staging has zero items containing 'blender' in name.")

    # Verify Animora.exe is present (the main rename target)
    staged_exe = DST_DIR / "Animora.exe"
    if not staged_exe.is_file():
        log.error("MISSING Animora.exe in staging — rename pipeline broken")
        return 1

    # Rename SxS assemblies + cpu-check DLL so no top-level item in the
    # install dir still says "blender." Must run before the sanity
    # check that follows (otherwise it would flag the remaining
    # blender.* items it itself is meant to relax).
    if not rename_runtime_assemblies(DST_DIR):
        return 1

    # Verify no system-DLL pollution slipped through (e.g., if a new
    # culprit appears we haven't added to SYSTEM_DLL_DENYLIST yet).
    # opengl32.dll next to Animora.exe is the single most damaging
    # regression — gate it explicitly.
    if (DST_DIR / "opengl32.dll").exists():
        log.error(
            "FATAL: opengl32.dll present in staging. Windows DLL search "
            "order will load this instead of the GPU driver's ICD, "
            "forcing software OpenGL 1.1 and breaking launch with "
            "'OpenGL 4.3 or higher required'. Remove it."
        )
        return 1

    # Verify the renamed SxS private assemblies are intact. Both sides
    # of the SxS chain (Animora.exe's embedded manifest AND the
    # assembly's own .manifest) must now spell "animora.<crt|shared>"
    # — rename_runtime_assemblies() handles both. We DO NOT add
    # processorArchitecture="amd64" to the assembly manifest: the
    # parent exe's manifest references the assembly WITHOUT it, and
    # Windows requires identity to match exactly. The no-arch chain
    # works wherever the system VC++ Redist is present; the installer
    # bundles VC_redist.x64.exe to guarantee that.
    for name in ("animora.crt", "animora.shared"):
        manifest = DST_DIR / name / f"{name}.manifest"
        if not manifest.is_file():
            log.error("Missing SxS manifest: %s", manifest)
            return 1
        log.info("OK: %s SxS private assembly present.", name)

    _copy_branded_startup_blend()
    patch_helper_scripts(DST_DIR)
    prune_runtime_payload(DST_DIR)
    patch_shipped_branding(DST_DIR)

    return 0


def _copy_branded_startup_blend() -> None:
    """Ensure the shipped runtime carries Animora's branded startup file."""
    if not STARTUP_BLEND_SRC.is_file():
        log.warning("Branded startup.blend not found at %s", STARTUP_BLEND_SRC)
        return

    dst = DST_DIR / BLENDER_VERSION / "datafiles" / "startup.blend"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STARTUP_BLEND_SRC, dst)
    log.info("Copy branded startup.blend: %s -> %s", STARTUP_BLEND_SRC, dst)


# --- Runtime-assembly rename + PE binary patch ---------------------------
#
# After the bulk copy, three items still carry the "blender" name:
#   • blender.crt/         (SxS private assembly: VC++ runtime DLLs)
#   • blender.shared/      (SxS private assembly: third-party libs)
#   • blender_cpu_check.dll
#
# Each is referenced by EXACTLY ONE byte sequence inside Animora.exe
# (and the same names inside Animora-launcher.exe for the SxS pair):
#   - Animora.exe's embedded manifest XML names the SxS assemblies via
#     `<assemblyIdentity type="win32" name="blender.crt" .../>`
#   - The PE Import Address Table references blender_cpu_check.dll
#     by its filename string in the import directory
#
# The new names are LENGTH-PRESERVING:
#   blender.crt        (11) → animora.crt        (11)
#   blender.shared     (14) → animora.shared     (14)
#   blender_cpu_check  (17) → animora_cpu_check  (17)
#
# A length-preserving in-place bytes.replace() therefore leaves every
# PE offset, RVA, section size, and checksum byte position unchanged.
# (PE Optional Header checksum is set to 0 by Blender's link step, so
# we don't need to recompute it. If we ever start signing the binary,
# signing must happen AFTER this rename.)
ASSEMBLY_RENAMES: tuple[tuple[str, str], ...] = (
    ("blender.crt",       "animora.crt"),
    ("blender.shared",    "animora.shared"),
    ("blender_cpu_check", "animora_cpu_check"),
)

# PEs that embed at least one of the above strings. Each file is opened
# once, patched in memory, written back atomically.
PE_PATCH_TARGETS: tuple[str, ...] = (
    "Animora.exe",
    "Animora-launcher.exe",
    # animora_cpu_check.dll itself, post-rename — patched so its own
    # PE-internal version-info / module name strings stay consistent.
    "animora_cpu_check.dll",
)


def rename_runtime_assemblies(stage_root: Path) -> bool:
    """Rename the SxS assemblies + cpu-check DLL and patch every PE
    that references them by name. Returns False on any error so the
    caller can abort staging."""

    log.info("--- Renaming runtime assemblies (blender.* → animora.*) ---")

    # 1. Rename blender_cpu_check.dll → animora_cpu_check.dll. This
    #    happens FIRST so the binary-patch step below finds the renamed
    #    file via its new name.
    src_dll = stage_root / "blender_cpu_check.dll"
    dst_dll = stage_root / "animora_cpu_check.dll"
    if src_dll.exists():
        src_dll.rename(dst_dll)
        log.info("RENAME   blender_cpu_check.dll  →  animora_cpu_check.dll")
    elif not dst_dll.exists():
        log.error("Neither blender_cpu_check.dll nor animora_cpu_check.dll found in staging.")
        return False

    # 2. Rename the two SxS private-assembly folders + the .manifest
    #    file each contains. Then string-replace the assemblyIdentity
    #    name attribute inside each manifest.
    for old, new in (("blender.crt", "animora.crt"),
                     ("blender.shared", "animora.shared")):
        old_dir = stage_root / old
        new_dir = stage_root / new
        if old_dir.exists():
            old_dir.rename(new_dir)
            log.info("RENAME   %s/  →  %s/", old, new)
        elif not new_dir.exists():
            log.error("Missing SxS assembly: neither %s nor %s present.", old, new)
            return False

        old_man = new_dir / f"{old}.manifest"
        new_man = new_dir / f"{new}.manifest"
        if old_man.exists():
            old_man.rename(new_man)
            log.info("RENAME   %s.manifest  →  %s.manifest", old, new)

        text = new_man.read_text(encoding="utf-8")
        rewritten = text.replace(f'name="{old}"', f'name="{new}"')
        if rewritten == text:
            log.warning(
                "Manifest %s did not contain `name=%r` — already renamed, or "
                "the upstream manifest format changed.", new_man.name, old,
            )
        else:
            new_man.write_text(rewritten, encoding="utf-8")
            log.info("Patch manifest XML: name=%r  →  name=%r (%s)", old, new, new_man.name)

    # 3. Binary-patch each PE that references the old names.
    for pe_name in PE_PATCH_TARGETS:
        pe_path = stage_root / pe_name
        if not pe_path.exists():
            log.warning("PE target %s not in staging (skip)", pe_name)
            continue

        data = pe_path.read_bytes()
        original_len = len(data)
        replacements = 0
        for old, new in ASSEMBLY_RENAMES:
            old_b = old.encode("ascii")
            new_b = new.encode("ascii")
            if len(old_b) != len(new_b):
                log.error("Rename %s → %s is not length-preserving — refusing to patch.", old, new)
                return False
            count = data.count(old_b)
            if count == 0:
                continue
            if count > 1:
                # Multiple hits would mean we'd patch debug strings or
                # similar — refuse rather than corrupt the PE.
                log.error(
                    "PE %s contains %d occurrences of %r — expected 0 or 1. "
                    "Aborting to avoid corrupting unrelated strings.",
                    pe_name, count, old,
                )
                return False
            data = data.replace(old_b, new_b)
            replacements += 1
            log.info("Patch PE  %s: %s → %s", pe_name, old, new)

        if replacements == 0:
            log.info("Patch PE  %s: no occurrences (already patched).", pe_name)
            continue

        if len(data) != original_len:
            log.error("PE patch changed file size — should be impossible with length-preserving renames.")
            return False
        pe_path.write_bytes(data)

    if not patch_launcher_target(stage_root):
        return False

    # 4. Patch Python's sitecustomize.py. Blender's bundled Python uses this
    #    to add the private shared-library folder to DLL/search paths. Since
    #    staging renames blender.shared/ to animora.shared/, leaving this text
    #    untouched produces a startup warning and can break USD/MaterialX
    #    library discovery.
    sitecustomize_matches = list(
        stage_root.glob("*/python/lib/site-packages/sitecustomize.py")
    )
    if sitecustomize_matches:
        sitecustomize = sitecustomize_matches[0]
        text = sitecustomize.read_text(encoding="utf-8")
        rewritten = text.replace('"blender.shared"', '"animora.shared"')
        if rewritten != text:
            sitecustomize.write_text(rewritten, encoding="utf-8")
            log.info("Patch Python sitecustomize: blender.shared → animora.shared")

    log.info("--- Runtime-assembly rename complete ---")
    return True


def patch_launcher_target(stage_root: Path) -> bool:
    """Patch the windowed launcher to spawn Animora.exe after staging rename."""

    launcher = stage_root / "Animora-launcher.exe"
    if not launcher.exists():
        log.warning("Animora-launcher.exe not in staging (skip launcher target patch)")
        return True

    old = "blender.exe".encode("utf-16le")
    new = "Animora.exe".encode("utf-16le")
    if len(old) != len(new):
        log.error("Launcher target rename is not length-preserving.")
        return False

    data = launcher.read_bytes()
    count = data.count(old)
    if count == 0:
        if new in data:
            log.info("Patch launcher target: already points at Animora.exe")
            return True
        log.error("Animora-launcher.exe did not contain the expected blender.exe target.")
        return False
    if count > 1:
        log.error(
            "Animora-launcher.exe contains %d UTF-16 blender.exe references; expected 1.",
            count,
        )
        return False

    launcher.write_bytes(data.replace(old, new))
    log.info("Patch launcher target: blender.exe -> Animora.exe")
    return True


def patch_helper_scripts(stage_root: Path) -> None:
    """Patch renamed Windows helper scripts to call the renamed main binary."""

    for script in stage_root.glob("Animora*.cmd"):
        text = script.read_text(encoding="utf-8")
        rewritten = text.replace(r"%~dp0\blender", r"%~dp0\Animora")
        rewritten = rewritten.replace("Starting blender", "Starting Animora")
        rewritten = rewritten.replace("Starting Blender", "Starting Animora")
        if rewritten != text:
            script.write_text(rewritten, encoding="utf-8")
            log.info("Patch helper script: %s", script.name)


def prune_runtime_payload(stage_root: Path) -> None:
    """Remove non-production helpers and upstream docs from the shipped tree."""

    version_root = _find_version_root(stage_root)
    for pattern in STAGING_REMOVE_GLOBS:
        for target in stage_root.glob(pattern):
            if target.exists():
                target.unlink()
                log.info("Prune runtime file: %s", target.name)

    if not version_root:
        return

    scripts_root = version_root / "scripts"
    for rel in STAGING_REMOVE_DIRS:
        target = version_root / rel
        if target.exists():
            shutil.rmtree(target)
            log.info("Prune runtime dir: %s", target.relative_to(stage_root))


def patch_shipped_branding(stage_root: Path) -> None:
    """Patch remaining user-visible Blender copy in the staged runtime."""

    version_root = _find_version_root(stage_root)
    if not version_root:
        return

    replacements = (
        ("Blender is free software", "Animora is creative software"),
        (
            "Licensed under the GNU General Public License",
            "Built on open-source technology and licensed components",
        ),
        ("Blender Store", "Animora Store"),
        ("Blender Website", "Animora Website"),
        ("blender-", "animora-"),
        ("This version of Blender", "This version of Animora"),
        ("version of Blender", "version of Animora"),
        ("contributors to Blender", "contributors to Animora"),
    )
    targets = [
        version_root / "scripts" / "startup" / "bl_operators" / "wm.py",
        version_root / "scripts" / "startup" / "bl_ui" / "space_topbar.py",
    ]
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rewritten = text
        for old, new in replacements:
            rewritten = rewritten.replace(old, new)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            log.info("Patch shipped branding: %s", path.relative_to(stage_root))


def _find_version_root(stage_root: Path) -> Path | None:
    candidates = [p for p in stage_root.iterdir() if p.is_dir() and p.name[:1].isdigit()]
    if not candidates:
        return None
    return sorted(candidates)[0]




def _ignore_factory():
    def _ignore(_dir, files):
        return [f for f in files if _should_exclude_name(f)]
    return _ignore


def _should_exclude_name(name: str) -> bool:
    import fnmatch
    if name in EXCLUDE_BASENAMES:
        return True
    suffix = Path(name).suffix.lower()
    if suffix in EXCLUDE_EXTENSIONS:
        return True
    for pattern in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(name.lower(), pattern.lower()):
            return True
    return False


def _should_exclude_file(path: Path) -> bool:
    return _should_exclude_name(path.name)


if __name__ == "__main__":
    sys.exit(stage())
