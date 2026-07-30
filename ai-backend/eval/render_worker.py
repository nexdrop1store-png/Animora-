"""Headless render worker — runs INSIDE a real Animora/Blender process.

Not part of the ai_backend Python package (it never imports it) — this
script is handed to `Animora.exe --background --python render_worker.py --
<script_path> <output_dir> <task_name>` and does ONLY three things:
  1. exec() the translated bpy script (see atomic_to_bpy.py) in a clean
     scene, so the render reflects exactly what the agent built.
  2. Auto-frame a camera (if the agent didn't create/aim one) so every
     object is actually visible — otherwise "the model didn't add a
     camera" would render nothing but silently fail to point that out.
  3. Render 3 viewpoints (front-3/4, side-3/4, top) via CYCLES on the CPU
     device — deliberately NOT EEVEE, so this never depends on whether
     Mesa's OpenGL path is behaving on this machine; Cycles CPU only
     needs bpy, no GL context at all.

Prints ONE JSON line to stdout as the last line — {"ok": bool,
"renders": [paths...], "errors": [...]} — the harness parses that; every
other stdout/stderr line is diagnostic noise from bpy/the exec'd script.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy

# ── Fixed render settings — kept small/fast per the Phase 0 plan's
# "keep tasks small (<60s each)" instruction. 64 samples + OIDN denoise is
# the same "fast preview" tier docs/AI_ARCHITECTURE.md describes for
# quality-check renders (as opposed to the 256+ sample user-facing final).
_RENDER_SAMPLES = 64
_RESOLUTION = 640
_VIEWPOINTS = (
    # (name, azimuth_deg, elevation_deg)
    ("front_three_quarter", 45.0, 25.0),
    ("side_three_quarter", 135.0, 25.0),
    ("top_down", 0.0, 80.0),
)


def _scene_bounds() -> tuple[tuple[float, float, float], float]:
    """Return (center, radius) of a sphere containing every mesh object's
    world-space bounding box. Falls back to a 4m default if the scene has
    no mesh geometry (nothing was built, or only lights/camera exist)."""
    import mathutils

    min_v = mathutils.Vector((math.inf, math.inf, math.inf))
    max_v = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        found = True
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ mathutils.Vector(corner)
            min_v.x, min_v.y, min_v.z = min(min_v.x, world_co.x), min(min_v.y, world_co.y), min(min_v.z, world_co.z)
            max_v.x, max_v.y, max_v.z = max(max_v.x, world_co.x), max(max_v.y, world_co.y), max(max_v.z, world_co.z)
    if not found:
        return (0.0, 0.0, 0.0), 4.0
    center = (min_v + max_v) / 2.0
    radius = max((max_v - min_v).length / 2.0, 0.5)
    return (center.x, center.y, center.z), radius


def _ensure_default_lighting() -> None:
    """If the agent's script created no lights at all, add one neutral
    sun so the scene isn't rendered pitch black — that would make every
    task's render equally useless to the vision scorer regardless of
    what geometry work actually happened. Mirrors the addon's own
    world/light defaults, not a quality judgment about the agent's work
    (the vision scorer sees whatever lighting the agent itself set up
    when it set up any)."""
    has_light = any(obj.type == "LIGHT" for obj in bpy.context.scene.objects)
    if has_light:
        return
    light_data = bpy.data.lights.new(name="EvalDefaultSun_Data", type="SUN")
    light_data.energy = 3.0
    obj = bpy.data.objects.new("EvalDefaultSun", light_data)
    obj.rotation_euler = (0.9, 0.0, 0.6)
    (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)


def _place_camera_for_viewpoint(azimuth_deg: float, elevation_deg: float) -> None:
    center, radius = _scene_bounds()
    distance = radius * 3.0 + 2.0  # generous margin so nothing clips
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    x = center[0] + distance * math.cos(el) * math.sin(az)
    y = center[1] - distance * math.cos(el) * math.cos(az)
    z = center[2] + distance * math.sin(el)

    cam_data = bpy.data.cameras.get("EvalCam_Data") or bpy.data.cameras.new("EvalCam_Data")
    cam_obj = bpy.data.objects.get("EvalCam")
    if cam_obj is None:
        cam_obj = bpy.data.objects.new("EvalCam", cam_data)
        (bpy.context.collection or bpy.context.scene.collection).objects.link(cam_obj)
    cam_obj.location = (x, y, z)

    # Aim at the scene center via a tracking constraint rather than manual
    # euler math — simpler and can't get the rotation sign wrong.
    for c in list(cam_obj.constraints):
        cam_obj.constraints.remove(c)
    target = bpy.data.objects.get("EvalCamTarget")
    if target is None:
        target = bpy.data.objects.new("EvalCamTarget", None)
        (bpy.context.collection or bpy.context.scene.collection).objects.link(target)
    target.location = center
    track = cam_obj.constraints.new(type="TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"

    bpy.context.scene.camera = cam_obj


def _configure_render() -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = _RENDER_SAMPLES
    scene.cycles.use_denoising = True
    scene.render.resolution_x = _RESOLUTION
    scene.render.resolution_y = _RESOLUTION
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "AgX" if "AgX" in [
        t.identifier for t in bpy.types.ColorManagedViewSettings.bl_rna.properties["view_transform"].enum_items
    ] else "Standard"


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    if len(argv) < 3:
        print(json.dumps({"ok": False, "renders": [], "errors": ["usage: script_path output_dir task_name"]}))
        return 1
    script_path, output_dir, task_name = argv[0], argv[1], argv[2]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []

    # Clean slate — start from an empty scene, not whatever the bundled
    # startup.blend contains, so every task starts from the same baseline.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    script_text = Path(script_path).read_text(encoding="utf-8")
    try:
        exec(compile(script_text, f"<eval:{task_name}>", "exec"), {"__name__": "__eval_script__"})
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a worker boundary
        errors.append(f"script execution raised {type(exc).__name__}: {exc}")

    try:
        bpy.context.view_layer.update()
    except Exception as exc:
        errors.append(f"view_layer.update failed: {exc}")

    _ensure_default_lighting()
    _configure_render()

    renders: list[str] = []
    for name, az, el in _VIEWPOINTS:
        try:
            _place_camera_for_viewpoint(az, el)
            out_path = out_dir / f"{task_name}__{name}.png"
            bpy.context.scene.render.filepath = str(out_path)
            bpy.ops.render.render(write_still=True)
            renders.append(str(out_path))
        except Exception as exc:
            errors.append(f"render '{name}' failed: {type(exc).__name__}: {exc}")

    result = {"ok": len(renders) > 0, "renders": renders, "errors": errors}
    # The one line the harness actually parses — print LAST and alone.
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
