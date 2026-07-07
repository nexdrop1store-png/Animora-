"""
Build Animora's default startup.blend (idempotent).

Target layout:

    +-------+--------------------------+------------+
    |       |                          | Outliner   |
    |  AI   |    Main 3D Viewport      +------------+
    |       |                          | Properties |
    +-------+--------------------------+------------+

Strategy:
  1. Take the truly stock factory settings as a base
  2. Locate the "Layout" descendant workspace (or already-renamed
     "Animora", or "Layout.001" from a previous run)
  3. Rename it to "Animora" and ensure it's the active workspace
  4. Purge any orphaned duplicate workspaces
  5. For each screen in the Animora workspace:
       a. Demote any existing ANIMORA areas back to VIEW_3D
       b. Consolidate all VIEW_3D areas into ONE via area_close()
          (idempotency: this undoes any accumulated cruft from prior runs)
       c. Split that single viewport vertically at factor=0.22 — the LEFT
          slice becomes the ANIMORA editor (~22% width)
  6. Save as startup.blend, copy to release/datafiles/

The Properties and Outliner editors on the right are NEVER touched — they
have area types PROPERTIES/OUTLINER, not VIEW_3D, so the consolidation
pass skips them entirely.

Run:
    Animora.exe --background --python scripts/build_default_startup.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
FORK_ROOT = REPO_ROOT / "blender-fork"
STARTUP_TARGET = FORK_ROOT / "release" / "datafiles" / "startup.blend"
LOG_TARGET = REPO_ROOT / "build" / "build_default_startup.log"


def _log(msg: str) -> None:
    """Write to a real file — Blender's headless stdout is unreliable on
    Windows so a side-channel log is the only way to see what happened."""
    LOG_TARGET.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_TARGET, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)

# Workspaces we expect to find as the "Layout" descendant (any of these names)
CANDIDATE_NAMES = ("Layout", "Animora", "Layout.001", "Layout.002")


def _find_base_workspace():
    for name in CANDIDATE_NAMES:
        ws = bpy.data.workspaces.get(name)
        if ws is not None:
            return ws
    return bpy.data.workspaces[0] if bpy.data.workspaces else None


def _purge_duplicate_layout_workspaces(keep):
    """Remove stale workspace duplicates left from previous build runs.

    `bpy.data.workspaces` doesn't expose a per-collection `.remove()` —
    use the cross-type `bpy.data.batch_remove([id, ...])` which works
    headlessly without needing an active window context.
    """
    stale = [
        ws for ws in list(bpy.data.workspaces)
        if ws is not keep
        and (ws.name.startswith("Layout.")
             or ws.name.startswith("Animora.")
             or ws.name in {"Layout", "Animation"})  # dupe Animation tab from earlier rounds
    ]
    if not stale:
        return
    # Make sure the active workspace is the keeper so removal doesn't orphan
    bpy.context.window.workspace = keep
    print(f"  purging {len(stale)} stale workspace(s): {[w.name for w in stale]}")
    try:
        bpy.data.batch_remove(stale)
    except Exception as exc:
        print(f"    batch_remove failed: {exc} — falling back to operator")
        for ws in stale:
            bpy.context.window.workspace = ws
            try:
                bpy.ops.workspace.delete()
            except Exception as e2:
                print(f"    could not delete {ws.name}: {e2}")
        bpy.context.window.workspace = keep


def _real_viewports(screen):
    """VIEW_3D areas with non-zero geometry.

    Blender's factory layout occasionally carries a 0x0 phantom VIEW_3D
    area (internal SDNA artefact). We must ignore those — they cannot be
    closed via `area_close` (no valid region/context) and would confuse
    any selection that relies on x-position.
    """
    return [a for a in screen.areas if a.type == "VIEW_3D" and a.width > 8 and a.height > 8]


def _consolidate_viewports(screen) -> None:
    """Merge all real VIEW_3D areas in `screen` down to a single one.

    Uses `bpy.ops.screen.area_close()` on the smallest sized viewport
    repeatedly until only one remains. Non-viewport editors (Properties,
    Outliner, Timeline, etc.) have different `.type` and are skipped.
    Zero-sized phantom areas are also skipped — they're not real areas
    to consolidate, just SDNA leftovers.
    """
    safety = 0
    while safety < 16:
        safety += 1
        vps = _real_viewports(screen)
        if len(vps) <= 1:
            return
        vps.sort(key=lambda a: a.width * a.height)
        smallest = vps[0]
        before = len(screen.areas)
        region = next(
            (r for r in smallest.regions if r.type == "WINDOW"),
            smallest.regions[0] if smallest.regions else None,
        )
        try:
            with bpy.context.temp_override(area=smallest, region=region):
                bpy.ops.screen.area_close()
        except Exception as exc:
            print(f"  area_close failed on viewport (x={smallest.x}): {exc}")
            return
        after = len(screen.areas)
        if after >= before:
            print(f"  area_close made no progress ({before} -> {after}); "
                  f"giving up consolidation to avoid infinite loop")
            return
        print(f"  consolidated viewport (x={smallest.x}) — {before} -> {after} areas")


def _dump_screen(screen, label: str) -> None:
    _log(f"  -- {label}: {len(screen.areas)} areas")
    for a in sorted(screen.areas, key=lambda x: (x.x, x.y)):
        _log(f"       {a.type:<14} x={a.x:<5} y={a.y:<5} w={a.width:<5} h={a.height}")


def _split_and_assign_animora(screen, factor: float = 0.22) -> bool:
    """Make sure `screen` ends up with exactly one ANIMORA area on the LEFT.

    Layout produced:  [ ANIMORA | rest of the screen unchanged ]
    """
    _dump_screen(screen, "BEFORE")

    # 1. Demote any leftover ANIMORA areas back to VIEW_3D.
    for a in list(screen.areas):
        if a.type == "ANIMORA":
            a.type = "VIEW_3D"

    # 2. Collapse all real VIEW_3D areas down to a single viewport.
    _consolidate_viewports(screen)
    _dump_screen(screen, "AFTER CONSOLIDATE")

    # 3. Pick the widest real viewport as the centre column to split.
    real = _real_viewports(screen)
    if not real:
        _log("WARNING: no sized VIEW_3D area to split — layout not modified")
        return False
    viewport = max(real, key=lambda a: a.width * a.height)
    _log(f"  picked viewport to split: x={viewport.x} w={viewport.width}")

    # Snapshot DNA pointers BEFORE the split so we can identify the new
    # area by set-diff afterwards. `area.as_pointer()` returns the C-side
    # ScrArea*, which is stable across the operator even when bpy
    # invalidates its Python wrappers.
    pre_split_ptrs = {a.as_pointer() for a in screen.areas}

    region = next(
        (r for r in viewport.regions if r.type == "WINDOW"),
        viewport.regions[0],
    )
    try:
        with bpy.context.temp_override(area=viewport, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=factor)
    except Exception as exc:
        _log(f"  area_split failed: {exc}")
        return False
    _dump_screen(screen, f"AFTER SPLIT (factor={factor})")

    # Identify the new area by pointer-diff. In --background mode this
    # area's reported geometry is 0x0 until a real GUI window loads the
    # file and runs the screen-geometry recalc — but the DNA tree is
    # already valid, and the area is the LEFT slice of a VERTICAL split
    # with factor=0.22 (Blender's area_split convention: the smaller
    # fraction goes to the NEW area when factor < 0.5).
    new_areas = [a for a in screen.areas if a.as_pointer() not in pre_split_ptrs]
    if not new_areas:
        _log("WARNING: area_split did not add any new area")
        return False
    if len(new_areas) > 1:
        _log(f"WARNING: area_split added {len(new_areas)} new areas (expected 1); "
             "picking the first")

    new_area = new_areas[0]
    new_area.type = "ANIMORA"
    _log(f"  -> set NEW (LEFT slice) area to ANIMORA "
         f"(ptr={new_area.as_pointer():#x})")
    _dump_screen(screen, "AFTER ASSIGN")
    return True


def build():
    # Reset log
    if LOG_TARGET.exists():
        LOG_TARGET.unlink()
    _log("=== build_default_startup.py ===")

    # Use the pristine on-disk startup.blend as the source. We CANNOT use
    # `read_factory_settings()` here because Blender's factory startup is
    # baked into the executable as a compile-time resource — running this
    # script after a previous broken build would re-read the cumulative
    # broken state. Opening the file directly bypasses the baked resource.
    if STARTUP_TARGET.is_file():
        _log(f"Opening source startup from disk: {STARTUP_TARGET}")
        bpy.ops.wm.open_mainfile(filepath=str(STARTUP_TARGET))
    else:
        _log("Source startup not on disk; falling back to factory settings")
        bpy.ops.wm.read_factory_settings(use_factory_startup_app_template_only=False)

    # The default Cube is load-bearing: the Sculpting workspace can only
    # enter Sculpt Mode with a sculptable mesh present (the addon's
    # sculpt_guard covers runtime deletion, but the factory scene must
    # start correct). A cube-less source produced the V1 "sculpting is
    # broken" report — fail the build rather than bake it.
    if bpy.data.objects.get("Cube") is None:
        _log("FAIL: default Cube missing from source startup scene")
        return 1

    base = _find_base_workspace()
    if base is None:
        _log("FAIL: no workspaces found in factory settings")
        return 1

    _log(f"Base workspace: '{base.name}'")
    if base.name != "Animora":
        base.name = "Animora"
        _log(f"  renamed -> '{base.name}'")

    _purge_duplicate_layout_workspaces(keep=base)

    bpy.context.window.workspace = base
    _log(f"  active workspace = '{bpy.context.window.workspace.name}'")

    import traceback
    for screen in base.screens:
        try:
            _split_and_assign_animora(screen, factor=0.22)
        except Exception:
            _log(f"EXCEPTION while processing screen {screen.name!r}:")
            _log(traceback.format_exc())

    bpy.context.preferences.view.use_save_prompt = True

    if bpy.data.objects.get("Cube") is None:
        _log("FAIL: default Cube lost during layout processing — not saving")
        return 1

    bpy.ops.wm.save_homefile()
    user_startup = Path(bpy.utils.user_resource("CONFIG")) / "startup.blend"
    STARTUP_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(user_startup, STARTUP_TARGET)
    _log(f"OK: wrote {STARTUP_TARGET} ({user_startup.stat().st_size:,} bytes)")
    _log(f"    workspaces in saved file: {[w.name for w in bpy.data.workspaces]}")
    return 0


if __name__ == "__main__":
    rc = build()
    # When this script is launched in NON-background (GUI) mode so that
    # `screen.area_split` actually has a real window to operate on, Blender
    # would otherwise sit on its event loop forever. Force a clean exit.
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        pass
    sys.exit(rc)
