"""
Anthropic tool definitions exposed to the LLM.

Kept in its own module so the streaming code stays focused on streaming, and
so persona modules (Phase 4) can override the tool list per-persona without
touching streaming.

## Tool design (Sprint 4D — MCP pivot, post-cofounder feedback)

The cofounder's dev-user recordings (`recordings/dev-user/`, 34 prompts)
showed three classes of failure stemming from a single-actuator design:

  • The pre-script wait felt like a hang — Opus streaming a 32k script
    can take 60-90s before the addon sees anything.
  • The exec phase froze the viewport — one big bpy run blocks the
    Blender main thread.
  • Any miss = total miss — banned imports / runtime errors meant no
    partial scene to fall back on.

We pivoted to the BlenderMCP shape (open-source ahujasid/blender-mcp):
many small TYPED atomic ops + one ESCAPE HATCH for procedural code.

The tool surface is now:

  ── Inspect (read-only, free to call mid-build) ──
  get_scene_info, get_object_info, viewport_screenshot

  ── Create ──
  create_primitive, create_light, create_camera

  ── Modify ──
  set_transform, add_modifier, apply_material, set_parent,
  delete_object, duplicate_object

  ── Environment ──
  set_world

  ── Escape hatch ──
  execute_animora_code  (the renamed execute_blender_script — same body,
                         same quality_enforcer gate, same AST-split runner.
                         The model is taught to prefer atomic ops and only
                         reach for this when nothing in the atomic suite
                         covers the case.)

  ── Existing supporting tools (unchanged) ──
  render_preview, render_final, suggest_next_steps, request_final_review,
  use_asset

Each atomic op is dispatched the same way as before
(`streaming.py:_on_tool_call` → `main.py:send_tool_call` → addon WS
`tool_call` → `operators.py:_on_tool_call`). The addon-side dispatcher
runs a per-tool handler; each handler is a 10-30-line bpy wrapper that
returns immediately, posts a one-line confirmation to the chat, and
sends `tool_result` with the scene_graph diff. Because each handler is
small and yields the main thread, the viewport stays responsive and
the user sees geometry appearing in real time — the perception fix
the cofounder asked for.
"""

from __future__ import annotations

from typing import Any

# ── Atomic primitive kinds — single source of truth for the enum ───────
# Used by `create_primitive`. Addon's _atomic_create_primitive handler
# maps each value to its bpy.ops.mesh.primitive_*_add call.
_PRIMITIVE_KINDS = ["cube", "sphere", "ico_sphere", "cylinder", "cone", "torus", "plane"]
_LIGHT_KINDS = ["sun", "point", "spot", "area"]
_MODIFIER_KINDS = [
    "bevel", "subdivision_surface", "array", "mirror", "solidify",
    "decimate", "screw", "wireframe",
]

BLENDER_TOOLS: list[dict[str, Any]] = [
    # ── Inspect ─────────────────────────────────────────────────────────
    {
        "name": "get_scene_info",
        "description": (
            "List every object currently in the user's scene with its "
            "type, location, rotation, scale, modifier stack, and "
            "materials. Call this BEFORE making changes to an existing "
            "scene so you know what's already there. Cheap; the scene "
            "graph is already in memory."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "viewport_screenshot",
        "description": (
            "Capture the current viewport as a JPEG and return it. Use "
            "AFTER a sequence of atomic ops to verify what you built "
            "actually looks right. Cheaper than render_preview — no "
            "Cycles samples, just the viewport draw."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    # ── Create ──────────────────────────────────────────────────────────
    {
        "name": "create_primitive",
        "description": (
            "Add a primitive mesh to the scene at a typed location/"
            "rotation/scale, with a meaningful name (rule #6 — never "
            "leave 'Cube.001' in the Outliner). This is your first "
            "choice for any new geometry; combine multiple primitives "
            "+ modifiers + materials to build complex shapes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": _PRIMITIVE_KINDS,
                    "description": "Which primitive to add.",
                },
                "name": {
                    "type": "string",
                    "description": "Outliner name. MUST match what the user asked for (e.g. 'Wheel_FL', 'TableTop'), not 'Cube'.",
                },
                "location": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "World-space (x, y, z) in metres.",
                },
                "rotation": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "Euler XYZ in radians. Defaults to (0,0,0).",
                },
                "scale": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "Non-uniform scale. Defaults to (1,1,1).",
                },
            },
            "required": ["kind", "name", "location"],
        },
    },
    {
        "name": "create_light",
        "description": (
            "Add a light to the scene. For three-point lighting setups, "
            "call this three times (key, fill, rim) with different "
            "positions and energies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": _LIGHT_KINDS},
                "name": {"type": "string", "description": "e.g. 'KeyLight', 'FillLight', 'SunLight'."},
                "location": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "rotation": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "energy": {
                    "type": "number",
                    "description": "Power in watts (point/spot) or W/m² (sun/area). Typical: sun=3-5, point=500-2000, spot=500-2000, area=100-1000.",
                },
                "color": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "(r, g, b) in linear 0-1. Defaults to (1,1,1).",
                },
                "size": {
                    "type": "number",
                    "description": "Area-light size in metres (area-only). Defaults to 1.",
                },
            },
            "required": ["kind", "name", "location", "energy"],
        },
    },
    {
        "name": "create_camera",
        "description": "Add a camera to the scene. Set as active if `set_active=true`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "location": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "rotation": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "focal_length": {
                    "type": "number",
                    "description": "Lens length in mm. Defaults to 50. Hero shots: 85. Wide environment: 24-35.",
                },
                "set_active": {
                    "type": "boolean",
                    "description": "Make this the active scene camera. Defaults to true.",
                },
            },
            "required": ["name", "location", "rotation"],
        },
    },
    # ── Modify ──────────────────────────────────────────────────────────
    {
        "name": "set_transform",
        "description": (
            "Move / rotate / scale an existing object. Only the axes you "
            "specify are changed — unspecified axes keep their current "
            "value. Use this to reposition after creation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "location": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "rotation": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
                "scale": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_modifier",
        "description": (
            "Add a modifier to an existing object (non-destructive — "
            "rule #2). Params are typed per modifier kind: bevel takes "
            "width/segments; subdivision_surface takes levels; array "
            "takes count + relative_offset; mirror takes axis; solidify "
            "takes thickness; decimate takes ratio; screw takes axis + "
            "angle + steps; wireframe takes thickness."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "kind": {"type": "string", "enum": _MODIFIER_KINDS},
                "params": {
                    "type": "object",
                    "description": "Kind-specific params (bevel: {width, segments}, subdivision_surface: {levels}, etc.).",
                },
            },
            "required": ["object", "kind"],
        },
    },
    {
        "name": "apply_material",
        "description": (
            "Apply a Principled BSDF material to an object. Creates the "
            "material if `name` is new, reuses if it already exists. "
            "For complex PBR (textures, normal maps, mix shaders), use "
            "execute_animora_code instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Material name. Defaults to f'Mat_{object}'.",
                },
                "base_color": {
                    "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                    "description": "RGBA in linear 0-1.",
                },
                "roughness": {"type": "number", "description": "0 = mirror, 1 = chalk. Defaults to 0.5."},
                "metallic": {"type": "number", "description": "0 = dielectric, 1 = metal."},
                "emission": {
                    "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                    "description": "RGBA emission color. Defaults to black.",
                },
                "emission_strength": {"type": "number"},
                "alpha": {"type": "number", "description": "0 = transparent, 1 = opaque."},
            },
            "required": ["object", "base_color"],
        },
    },
    {
        "name": "set_parent",
        "description": "Parent one object to another (e.g., wheels → car body).",
        "input_schema": {
            "type": "object",
            "properties": {
                "child": {"type": "string"},
                "parent": {"type": "string"},
                "keep_transform": {
                    "type": "boolean",
                    "description": "Preserve world transform when re-parenting. Defaults to true.",
                },
            },
            "required": ["child", "parent"],
        },
    },
    {
        "name": "delete_object",
        "description": "Remove an object from the scene by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "duplicate_object",
        "description": "Clone an object (linked-mesh) with a new name and optional location offset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "new_name": {"type": "string"},
                "location_offset": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "(dx, dy, dz) offset from source. Defaults to (0,0,0).",
                },
            },
            "required": ["source", "new_name"],
        },
    },
    # ── Environment ─────────────────────────────────────────────────────
    {
        "name": "set_world",
        "description": (
            "Set the world environment — solid color, HDRI, or strength. "
            "For HDRI use the `use_asset` tool with an HDRI asset_id; "
            "this tool is for direct color / strength tweaks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "color": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "Background RGB in linear 0-1.",
                },
                "strength": {"type": "number", "description": "World background strength. Defaults to 1."},
            },
        },
    },
    # ── Escape hatch ────────────────────────────────────────────────────
    # The renamed execute_blender_script. Same behavior, same gate, same
    # AST-split runner on the addon side. The master prompt teaches the
    # model to use atomic ops first and only reach for this when the
    # atomic surface can't express what's needed.
    {
        "name": "execute_animora_code",
        "description": (
            "ESCAPE HATCH — run a Python bpy script when no atomic tool "
            "above covers what you need. Reach for this for: complex "
            "procedural geometry (loft / bridge / bmesh edits), Geometry "
            "Nodes graphs, custom shader node setups, animation "
            "keyframing, particle / physics setup, sculpting brushes, "
            "anything the typed atomic ops can't express. For ordinary "
            "'add a sphere, scale it, give it a red material' work, "
            "prefer create_primitive + set_transform + apply_material — "
            "the user sees those land in the viewport instantly; a big "
            "bpy script takes seconds-to-minutes and the viewport stays "
            "frozen while it runs.\n\n"
            "Security rules still apply: the script must NOT import "
            "os/subprocess/sys/shutil/socket/urllib/requests/httpx or "
            "call open()/eval()/exec()/compile()/__import__. It runs in "
            "the user's active session with access to bpy, bmesh, "
            "mathutils, math, and random."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Valid Python bpy script. Will be syntax-checked and statically analysed before execution.",
                },
                "intent_summary": {
                    "type": "string",
                    "description": "One-line description of what this script does, used as the undo-stack label the user sees (e.g. 'Add palm tree cluster').",
                },
            },
            "required": ["script", "intent_summary"],
        },
    },
    {
        "name": "get_object_info",
        "description": "Query detailed information about a named object in the current scene — vertex count, modifier stack, material list, transform, parent/children.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "render_preview",
        "description": (
            "Trigger a fast preview render (Cycles, 32 samples, denoised) and "
            "return the result image. Use this BEFORE showing the user a result "
            "to verify quality (artist's-eye check). Cheap — call freely."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "render_final",
        "description": (
            "Trigger a full-quality render (Cycles, 256+ samples, denoised, "
            "Filmic/AgX) and return the result image. This is the render the "
            "user actually sees. Only call AFTER render_preview + artist's-eye "
            "check have confirmed the scene is at maximum quality."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suggest_next_steps",
        "description": "Show the user a list of 2-5 suggested follow-up actions as one-click chips in the panel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 5,
                    "description": "Short imperative phrases (e.g. 'Add a sunset HDRI', 'Scatter rocks along the shore').",
                }
            },
            "required": ["steps"],
        },
    },
    # ── Quality Plan Sprint 2B: checkpoint signaling ────────────────────
    # The agentic loop runs the artist's-eye check only at "checkpoints"
    # to avoid Sonnet vision spend on every iteration. The model signals
    # a checkpoint by calling `request_final_review` — typically AFTER
    # the last meaningful build step, BEFORE deciding to end the turn.
    # The orchestrator treats this as an explicit "please verify now"
    # event and runs the artist's-eye check on that iteration.
    {
        "name": "request_final_review",
        "description": (
            "Call this AFTER you believe the asset / scene is finished and "
            "ready for the user. The Animora quality system runs an "
            "artist's-eye verification on the current viewport state. If "
            "issues are found, you'll get a revision request next iteration; "
            "if everything passes, the loop ends cleanly. Use this instead "
            "of just emitting an end_turn text — calling this tool gives "
            "the system a chance to catch quality problems before the user "
            "sees the result. No arguments."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    # ── Quality Plan Sprint 3: asset-first building ────────────────────
    # `use_asset` lets the model drop in a vetted CC0 PolyHaven asset
    # (HDRI / texture / mesh) instead of hand-coding details from
    # primitives. The orchestrator intercepts the tool_call, fetches
    # the file from PolyHaven's CDN (cached after first use), and
    # dispatches a load_asset directive to the addon. The addon
    # applies the asset to the active scene appropriately for its kind:
    #   - HDRI → world environment texture
    #   - texture → Principled BSDF material on the named target
    #   - mesh → linked-append into the current collection
    {
        "name": "use_asset",
        "description": (
            "Drop a vetted CC0 PolyHaven asset into the current scene. "
            "USE THIS FIRST whenever the available-assets list contains a "
            "match for what the SPEC asks for — hand-built approximations "
            "of textures, HDRIs, and reference meshes almost never beat "
            "the curated asset. Animora fetches the file from PolyHaven "
            "(cached locally after first use, ~5-15 MB) and the addon "
            "applies it to the active scene. For HDRI: sets the world "
            "environment. For texture: applies to the named target as a "
            "Principled BSDF material. For mesh: links the asset into the "
            "current collection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "The catalog id (e.g. 'hdri.golden_hour_field', 'texture.weathered_oak', 'mesh.modern_chair'). Must match one of the IDs listed in the available-assets block.",
                },
                "target": {
                    "type": "string",
                    "description": "Optional. For textures, the object name to apply the material to (e.g. 'Floor', 'Table'). For meshes, the location override as 'x,y,z'. Ignored for HDRIs.",
                },
            },
            "required": ["asset_id"],
        },
    },
]
