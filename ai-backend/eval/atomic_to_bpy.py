"""Translate captured atomic tool calls into a single, DIRECTLY EXECUTABLE
bpy script — for real headless rendering, not regex scoring.

This is a sibling to scoring.py's render_tool_calls_as_bpy(), which produces
text good enough for regex matching but NOT valid Python (it references
undefined names like `obj`/`nodes`/`light_obj`). This module exists because
Phase 0 of the AI-quality workstream needs REAL renders of what the agent
actually built, scored by a real vision call — the grounding rule ("critique
must be anchored to a real artifact") makes a text-approximation unusable
here.

Each translator function below mirrors the addon's real atomic-tool bodies
(addons/animora_panel/operators.py:_atomic_*) line-for-line where possible —
same bpy.data/bmesh calls, same defaults — MINUS the live-addon plumbing
those functions also carry (WS tool_result reporting, vision exec-pause,
chat history, presence-verify). That plumbing has no meaning in a headless,
single-process render pass; the bpy mutation itself is what must match.

Kept deliberately independent of operators.py (that module imports bpy at
call time from within a running Blender+addon session and calls addon-only
helpers like _send_tool_result/_find_view3d_context that don't exist
headless) — duplicating the ~10 short mutation bodies here is simpler and
safer than trying to import through/around a live-addon module.

Usage:
    from ai_backend.eval.atomic_to_bpy import tool_calls_to_bpy_script
    script = tool_calls_to_bpy_script(captured_tool_calls, real_script)
    # then exec `script` inside a real (possibly headless) Blender process
"""

from __future__ import annotations

from typing import Any

# bpy modifier kind -> real bpy type name. Mirrors operators.py's mapping.
_MODIFIER_KIND_TO_BPY: dict[str, str] = {
    "bevel": "BEVEL",
    "subdivision_surface": "SUBSURF",
    "array": "ARRAY",
    "mirror": "MIRROR",
    "solidify": "SOLIDIFY",
    "decimate": "DECIMATE",
    "screw": "SCREW",
    "wireframe": "WIREFRAME",
}

_PRIMITIVE_KINDS = {"cube", "sphere", "ico_sphere", "cylinder", "cone", "torus", "plane"}
_LIGHT_KINDS = {"SUN", "POINT", "SPOT", "AREA"}


def _fmt_vec3(v: Any, default: tuple[float, float, float]) -> str:
    t = tuple(v)[:3] if v else default
    t = tuple(t) + default[len(t):]  # pad short vectors with the default's tail
    return f"({t[0]!r}, {t[1]!r}, {t[2]!r})"


def _fmt_vec4(v: Any, default: tuple[float, float, float, float]) -> str:
    t = tuple(v)[:4] if v else default
    if len(t) == 3:
        t = (*t, 1.0)
    t = tuple(t) + default[len(t):]
    return f"({t[0]!r}, {t[1]!r}, {t[2]!r}, {t[3]!r})"


def _py_str(s: Any) -> str:
    """A safely-quoted Python string literal for embedding in generated code."""
    return repr(str(s))


def _create_primitive(inp: dict[str, Any]) -> str:
    kind = str(inp.get("kind", "")).lower()
    if kind not in _PRIMITIVE_KINDS:
        return f"# skipped create_primitive: unknown kind {kind!r}"
    name = str(inp.get("name", "")).strip() or kind.title()
    location = _fmt_vec3(inp.get("location"), (0.0, 0.0, 0.0))
    rotation = _fmt_vec3(inp.get("rotation"), (0.0, 0.0, 0.0))
    scale = _fmt_vec3(inp.get("scale"), (1.0, 1.0, 1.0))

    if kind == "torus":
        # bmesh has no torus generator (same constraint as production);
        # bpy.ops.mesh.primitive_torus_add's poll() only needs an active
        # view layer, which --background mode has, so this works headless.
        return (
            f"bpy.ops.mesh.primitive_torus_add(location={location}, rotation={rotation})\n"
            f"_obj = bpy.context.active_object\n"
            f"if _obj is not None:\n"
            f"    _obj.name = {_py_str(name)}\n"
            f"    _obj.scale = {scale}\n"
        )

    bmesh_call = {
        "cube": "bmesh.ops.create_cube(_bm, size=2.0)",
        "sphere": "bmesh.ops.create_uvsphere(_bm, u_segments=32, v_segments=16, radius=1.0)",
        "ico_sphere": "bmesh.ops.create_icosphere(_bm, subdivisions=2, radius=1.0)",
        "cylinder": ("bmesh.ops.create_cone(_bm, segments=32, radius1=1.0, radius2=1.0, "
                     "depth=2.0, cap_ends=True, cap_tris=False)"),
        "cone": ("bmesh.ops.create_cone(_bm, segments=32, radius1=1.0, radius2=0.0, "
                 "depth=2.0, cap_ends=True, cap_tris=False)"),
        "plane": "bmesh.ops.create_grid(_bm, x_segments=1, y_segments=1, size=1.0)",
    }[kind]
    return (
        f"_mesh = bpy.data.meshes.new(name={_py_str(name + '_Mesh')})\n"
        f"_bm = bmesh.new()\n"
        f"try:\n"
        f"    {bmesh_call}\n"
        f"    _bm.to_mesh(_mesh)\n"
        f"finally:\n"
        f"    _bm.free()\n"
        f"_mesh.update()\n"
        f"_obj = bpy.data.objects.new({_py_str(name)}, _mesh)\n"
        f"_obj.location = {location}\n"
        f"_obj.rotation_euler = {rotation}\n"
        f"_obj.scale = {scale}\n"
        f"(bpy.context.collection or bpy.context.scene.collection).objects.link(_obj)\n"
    )


def _create_light(inp: dict[str, Any]) -> str:
    kind = str(inp.get("kind") or inp.get("type") or "").upper()
    if kind not in _LIGHT_KINDS:
        return f"# skipped create_light: unknown kind {kind!r}"
    name = str(inp.get("name", "")).strip() or f"{kind.title()}Light"
    location = _fmt_vec3(inp.get("location"), (0.0, 0.0, 5.0))
    rotation = _fmt_vec3(inp.get("rotation"), (0.0, 0.0, 0.0))
    energy = float(inp.get("energy", 1000))
    color = _fmt_vec3(inp.get("color"), (1.0, 1.0, 1.0))
    size = float(inp.get("size", 1.0))
    size_line = f"_light_data.size = {size!r}\n" if kind == "AREA" else ""
    return (
        f"_light_data = bpy.data.lights.new(name={_py_str(name + '_Data')}, type={_py_str(kind)})\n"
        f"_light_data.energy = {energy!r}\n"
        f"_light_data.color = {color}\n"
        f"{size_line}"
        f"_obj = bpy.data.objects.new({_py_str(name)}, _light_data)\n"
        f"_obj.location = {location}\n"
        f"_obj.rotation_euler = {rotation}\n"
        f"(bpy.context.collection or bpy.context.scene.collection).objects.link(_obj)\n"
    )


def _create_camera(inp: dict[str, Any]) -> str:
    name = str(inp.get("name", "")).strip() or "Camera"
    location = _fmt_vec3(inp.get("location"), (7.0, -7.0, 5.0))
    rotation = _fmt_vec3(inp.get("rotation"), (1.1, 0.0, 0.78))
    focal_length = float(inp.get("focal_length", 50))
    set_active = bool(inp.get("set_active", True))
    active_line = "bpy.context.scene.camera = _obj\n" if set_active else ""
    return (
        f"_cam_data = bpy.data.cameras.new(name={_py_str(name + '_Data')})\n"
        f"_cam_data.lens = {focal_length!r}\n"
        f"_obj = bpy.data.objects.new({_py_str(name)}, _cam_data)\n"
        f"_obj.location = {location}\n"
        f"_obj.rotation_euler = {rotation}\n"
        f"(bpy.context.collection or bpy.context.scene.collection).objects.link(_obj)\n"
        f"{active_line}"
    )


def _set_transform(inp: dict[str, Any]) -> str:
    name = str(inp.get("name", "")).strip()
    if not name:
        return "# skipped set_transform: missing name"
    lines = [f"_obj = bpy.data.objects.get({_py_str(name)})", "if _obj is not None:"]
    body: list[str] = []
    if inp.get("location") is not None:
        body.append(f"    _obj.location = {_fmt_vec3(inp['location'], (0.0, 0.0, 0.0))}")
    if inp.get("rotation") is not None:
        body.append(f"    _obj.rotation_euler = {_fmt_vec3(inp['rotation'], (0.0, 0.0, 0.0))}")
    if inp.get("scale") is not None:
        body.append(f"    _obj.scale = {_fmt_vec3(inp['scale'], (1.0, 1.0, 1.0))}")
    if not body:
        body = ["    pass"]
    return "\n".join(lines + body) + "\n"


def _add_modifier(inp: dict[str, Any]) -> str:
    target = str(inp.get("object", "")).strip()
    kind = str(inp.get("kind", "")).lower()
    bpy_type = _MODIFIER_KIND_TO_BPY.get(kind)
    if not target or bpy_type is None:
        return f"# skipped add_modifier: object={target!r} kind={kind!r}"
    params = inp.get("params") or {}
    param_lines = []
    for k, v in params.items():
        if isinstance(k, str) and k.isidentifier():
            param_lines.append(
                f"try:\n    _mod.{k} = {v!r}\nexcept Exception:\n    pass"
            )
    param_block = ("\n".join(param_lines) + "\n") if param_lines else ""
    return (
        f"_obj = bpy.data.objects.get({_py_str(target)})\n"
        f"if _obj is not None:\n"
        f"    _mod = _obj.modifiers.new(name={_py_str(kind.title())}, type={_py_str(bpy_type)})\n"
        + "\n".join(f"    {ln}" for ln in param_block.splitlines())
        + ("\n" if param_block else "")
    )


def _apply_material(inp: dict[str, Any]) -> str:
    target = str(inp.get("object", "")).strip()
    if not target:
        return "# skipped apply_material: missing object"
    mat_name = str(inp.get("name", "")).strip() or f"Mat_{target}"
    base_color = _fmt_vec4(inp.get("base_color"), (0.8, 0.8, 0.8, 1.0))
    roughness = float(inp.get("roughness", 0.5))
    metallic = float(inp.get("metallic", 0.0))
    alpha = float(inp.get("alpha", 1.0))
    emission = inp.get("emission")
    emission_strength = float(inp.get("emission_strength", 0.0))

    emission_block = ""
    if emission is not None:
        em = _fmt_vec4(emission, (0.0, 0.0, 0.0, 1.0))
        emission_block = (
            f'    if "Emission Color" in _bsdf.inputs:\n'
            f"        _bsdf.inputs['Emission Color'].default_value = {em}\n"
            f'        if "Emission Strength" in _bsdf.inputs:\n'
            f"            _bsdf.inputs['Emission Strength'].default_value = {emission_strength!r}\n"
        )

    return (
        f"_obj = bpy.data.objects.get({_py_str(target)})\n"
        f"if _obj is not None and _obj.type == 'MESH':\n"
        f"    _mat = bpy.data.materials.get({_py_str(mat_name)}) or bpy.data.materials.new(name={_py_str(mat_name)})\n"
        f"    _mat.use_nodes = True\n"
        f"    _bsdf = _mat.node_tree.nodes.get('Principled BSDF')\n"
        f"    if _bsdf is not None:\n"
        f"        _bsdf.inputs['Base Color'].default_value = {base_color}\n"
        f"        _bsdf.inputs['Roughness'].default_value = {roughness!r}\n"
        f"        _bsdf.inputs['Metallic'].default_value = {metallic!r}\n"
        f"        if 'Alpha' in _bsdf.inputs:\n"
        f"            _bsdf.inputs['Alpha'].default_value = {alpha!r}\n"
        f"{emission_block}"
        f"    try:\n"
        f"        _mat.diffuse_color = {base_color}\n"
        f"    except Exception:\n"
        f"        pass\n"
        f"    if _obj.data.materials:\n"
        f"        _obj.data.materials[0] = _mat\n"
        f"    else:\n"
        f"        _obj.data.materials.append(_mat)\n"
    )


def _set_parent(inp: dict[str, Any]) -> str:
    child_name = str(inp.get("child", "")).strip()
    parent_name = str(inp.get("parent", "")).strip()
    if not child_name or not parent_name:
        return "# skipped set_parent: missing child/parent"
    keep_transform = bool(inp.get("keep_transform", True))
    if keep_transform:
        body = (
            "    _world = _child.matrix_world.copy()\n"
            "    _child.parent = _parent\n"
            "    _child.matrix_world = _world\n"
        )
    else:
        body = "    _child.parent = _parent\n"
    return (
        f"_child = bpy.data.objects.get({_py_str(child_name)})\n"
        f"_parent = bpy.data.objects.get({_py_str(parent_name)})\n"
        f"if _child is not None and _parent is not None:\n"
        f"{body}"
    )


def _delete_object(inp: dict[str, Any]) -> str:
    name = str(inp.get("name", "")).strip()
    if not name:
        return "# skipped delete_object: missing name"
    return (
        f"_obj = bpy.data.objects.get({_py_str(name)})\n"
        f"if _obj is not None:\n"
        f"    bpy.data.objects.remove(_obj, do_unlink=True)\n"
    )


def _duplicate_object(inp: dict[str, Any]) -> str:
    source_name = str(inp.get("source", "")).strip()
    if not source_name:
        return "# skipped duplicate_object: missing source"
    new_name = str(inp.get("new_name", "")).strip() or f"{source_name}_dup"
    offset = tuple(inp.get("location_offset") or (0.0, 0.0, 0.0))[:3]
    return (
        f"_src = bpy.data.objects.get({_py_str(source_name)})\n"
        f"if _src is not None:\n"
        f"    _new = _src.copy()\n"
        f"    if _src.data is not None:\n"
        f"        _new.data = _src.data\n"
        f"    _new.name = {_py_str(new_name)}\n"
        f"    _new.location = (_src.location[0] + {offset[0]!r}, "
        f"_src.location[1] + {offset[1]!r}, _src.location[2] + {offset[2]!r})\n"
        f"    (bpy.context.collection or bpy.context.scene.collection).objects.link(_new)\n"
    )


def _set_world(inp: dict[str, Any]) -> str:
    color = inp.get("color")
    strength = inp.get("strength")
    lines = [
        "_world = bpy.context.scene.world",
        "if _world is None:",
        "    _world = bpy.data.worlds.new('World')",
        "    bpy.context.scene.world = _world",
        "_world.use_nodes = True",
        "_bg = _world.node_tree.nodes.get('Background')",
        "if _bg is not None:",
    ]
    body = []
    if color is not None:
        c = _fmt_vec4(color, (0.05, 0.05, 0.05, 1.0))
        body.append(f"    _bg.inputs['Color'].default_value = {c}")
    if strength is not None:
        body.append(f"    _bg.inputs['Strength'].default_value = {float(strength)!r}")
    if not body:
        body = ["    pass"]
    return "\n".join(lines + body) + "\n"


# Tool name -> translator. Read-only / meta tools (get_scene_info,
# viewport_screenshot, request_final_review, use_asset, load_asset) don't
# mutate the scene and are intentionally omitted — nothing to render.
_TRANSLATORS = {
    "create_primitive": _create_primitive,
    "create_light": _create_light,
    "create_camera": _create_camera,
    "set_transform": _set_transform,
    "add_modifier": _add_modifier,
    "apply_material": _apply_material,
    "set_parent": _set_parent,
    "delete_object": _delete_object,
    "duplicate_object": _duplicate_object,
    "set_world": _set_world,
}


def tool_calls_to_bpy_script(tool_calls: list[dict[str, Any]], real_script: str = "") -> str:
    """Build ONE directly-executable bpy script reproducing everything the
    agent did: every atomic tool call translated to real bpy.data/bmesh
    calls (in order), plus the raw script from any execute_animora_code /
    execute_blender_script call appended verbatim (that path already emits
    valid bpy, no translation needed).

    Each atomic translation is wrapped in its own try/except so one bad
    tool call (e.g. referencing an object the agent never actually created
    due to a naming mismatch) can't take down the whole render — matches
    the spirit of the addon's per-tool error isolation, just headless.
    """
    lines: list[str] = [
        "import bpy",
        "import bmesh",
        "",
    ]
    for i, tc in enumerate(tool_calls):
        name = tc.get("name", "")
        inp = tc.get("input") or {}
        translator = _TRANSLATORS.get(name)
        if translator is None:
            continue  # execute_* handled separately below; everything else is read-only/meta
        snippet = translator(inp)
        lines.append(f"# --- tool call {i}: {name} ---")
        lines.append("try:")
        lines.extend(f"    {ln}" for ln in snippet.splitlines())
        lines.append("except Exception as _exc:")
        lines.append(f"    print('[atomic_to_bpy] tool call {i} ({name}) failed:', _exc)")
        lines.append("")

    if real_script.strip():
        lines.append("# --- escape-hatch script (execute_animora_code/execute_blender_script) ---")
        lines.append("try:")
        lines.extend(f"    {ln}" for ln in real_script.splitlines())
        lines.append("except Exception as _exc:")
        lines.append("    print('[atomic_to_bpy] escape-hatch script failed:', _exc)")
        lines.append("")

    return "\n".join(lines)
