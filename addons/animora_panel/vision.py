"""
Real-time Vision System — three levels of scene awareness.

Level 1: Continuous viewport stream (5–15 fps, perceptual-diff gated JPEG)
Level 2: Event-triggered HD PNG captures (selection, render, post-script,
         heartbeat — Phase 2)
Level 3: Scene graph JSON sync (debounced 500ms) — Phase 2 includes
         modifier params, vertex counts, keyframe counts, shader summary,
         world/HDRI metadata.
"""

from __future__ import annotations

import io
import logging
import struct
import time
from typing import TYPE_CHECKING

import bpy

if TYPE_CHECKING:
    from .ws_client import AnimoraWSClient

log = logging.getLogger("animora.vision")

_STREAM_MIN_INTERVAL = 1.0 / 15  # 15 fps max
_STREAM_PERCEPTUAL_THRESHOLD = 12.0  # mean abs pixel-diff threshold (0–255 scale)
_SCENE_GRAPH_DEBOUNCE = 0.5      # seconds

# Perceptual-diff: keep a tiny thumbnail of the last sent frame and compare
# the next candidate against it. Cheaper than hashing whole JPEGs, and
# stable against the 1-bit JPEG noise that breaks hash equality (the old
# behaviour resent every frame because hashes never matched).
_THUMB_W, _THUMB_H = 32, 18  # 32x18 grayscale comparison

_last_thumb: bytes | None = None
_last_stream_time: float = 0.0
_scene_graph_timer_handle = None
_handlers_registered = False

# Sprint 4F — exec-pause counter. Bumped by operators._atomic_run /
# operators._ScriptRunner (`begin_exec_pause()`) at the start of every
# atomic tool dispatch + every AST-split step, and decremented at the
# end (`end_exec_pause()`). While >0, depsgraph-triggered viewport
# streams + per-step scene_graph sends are SKIPPED. Without this,
# each atomic tool triggers 4-6 depsgraph updates (view_layer.update,
# tag_redraw, scene_graph serialize, ...) and each one fires a
# synchronous gpu.types.GPUOffScreen.draw_view3d(...) capture on the
# main thread. On a 6-tool build that's 30+ captures racing the main
# thread — the exact "Animora unresponsive" pattern the cofounder
# reported. With the pause active the depsgraph handler simply
# returns; we capture HD post-script at the script's natural end
# instead.
_exec_pause_depth: int = 0


def begin_exec_pause() -> None:
    """Suppress depsgraph-driven viewport captures while addon-side
    execution is in flight. Increment/decrement is balanced; reentrant."""
    global _exec_pause_depth
    _exec_pause_depth += 1


def end_exec_pause() -> None:
    """Counterpart to `begin_exec_pause`. Safe to call when depth is 0
    (no-op + logged at debug)."""
    global _exec_pause_depth
    if _exec_pause_depth <= 0:
        log.debug("end_exec_pause called when depth=0 — ignored")
        return
    _exec_pause_depth -= 1


def is_exec_paused() -> bool:
    return _exec_pause_depth > 0


# ---------------------------------------------------------------------------
# Level 1 — Continuous viewport stream
# ---------------------------------------------------------------------------

def capture_viewport_jpeg(width: int = 640, height: int = 360, quality: int = 60) -> bytes | None:
    """Render the active viewport to JPEG bytes using GPUOffScreen."""
    try:
        import gpu
        from gpu_extras.presets import draw_texture_2d

        offscreen = gpu.types.GPUOffScreen(width, height)
        context = bpy.context

        space = next(
            (
                s
                for area in context.screen.areas
                if area.type == "VIEW_3D"
                for s in area.spaces
                if s.type == "VIEW_3D"
            ),
            None,
        )
        if space is None:
            return None

        with offscreen.bind():
            offscreen.draw_view3d(
                scene=context.scene,
                view_layer=context.view_layer,
                view3d=space,
                region=next(
                    r for a in context.screen.areas if a.type == "VIEW_3D" for r in a.regions if r.type == "WINDOW"
                ),
                view_matrix=space.region_3d.view_matrix,
                projection_matrix=space.region_3d.window_matrix,
            )
            pixel_data = offscreen.texture_color.read()

        # Convert to PIL/image bytes
        try:
            from PIL import Image

            img = Image.frombytes("RGBA", (width, height), pixel_data.to_list(), "raw", "RGBA", 0, -1)
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
        except ImportError:
            # Fallback: use Blender's built-in image save
            tmp_img = bpy.data.images.new("_animora_tmp", width, height, float_buffer=False)
            tmp_img.pixels = [v / 255.0 for px in pixel_data.to_list() for v in px]
            buf = io.BytesIO()
            tmp_img.save_render(buf.name if hasattr(buf, "name") else "/tmp/_animora_frame.jpg")
            bpy.data.images.remove(tmp_img)
            return None

    except Exception as exc:
        log.debug("Viewport capture failed: %s", exc)
        return None


def _make_thumb(jpeg_bytes: bytes) -> bytes | None:
    """Decode the JPEG and downsample to a tiny grayscale strip for diffing."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        img = img.convert("L").resize((_THUMB_W, _THUMB_H), Image.BILINEAR)
        return img.tobytes()
    except Exception:
        return None


def _perceptual_diff(a: bytes, b: bytes) -> float:
    """Mean absolute pixel difference (0–255). Higher = more different."""
    if len(a) != len(b):
        return 255.0
    total = 0
    for x, y in zip(a, b):
        total += abs(x - y)
    return total / max(len(a), 1)


def _should_send_frame(jpeg_bytes: bytes) -> bool:
    """Rate-limit + perceptual-diff gate. Replaces the old hash-equality
    check which was unreliable (JPEG encoders produce 1-bit noise even
    between identical viewports, so hashes never matched and every frame
    was sent regardless)."""
    global _last_thumb, _last_stream_time

    now = time.monotonic()
    if now - _last_stream_time < _STREAM_MIN_INTERVAL:
        return False

    thumb = _make_thumb(jpeg_bytes)
    if thumb is None:
        # PIL not available — fall back to time-only gating
        _last_stream_time = now
        return True

    if _last_thumb is not None:
        diff = _perceptual_diff(_last_thumb, thumb)
        if diff < _STREAM_PERCEPTUAL_THRESHOLD:
            return False

    _last_thumb = thumb
    _last_stream_time = now
    return True


def stream_viewport_frame(client: "AnimoraWSClient") -> None:
    """Push a viewport frame to the backend unless paused or unchanged."""
    if not client.connected:
        return
    # Sprint 4F — skip ALL depsgraph-driven captures while addon-side
    # execution is in flight. Captures are synchronous, main-thread,
    # and expensive (~30-200ms on a complex scene); chaining them per
    # depsgraph update during a multi-tool build is the dominant cause
    # of the "Animora unresponsive" perception.
    if is_exec_paused():
        return
    # Honour backend backpressure — server flips this via pause_stream /
    # resume_stream control messages when its buffer is full.
    if getattr(client, "stream_paused", False):
        return
    # Sprint 4F — cheap rate-limit gate BEFORE the expensive capture.
    # The old order ran `capture_viewport_jpeg` first then checked the
    # 15-fps rate limit on the encoded bytes, meaning we paid the full
    # offscreen draw + JPEG encode + thumbnail cost on every depsgraph
    # update only to drop the frame. Moving the time check up means a
    # tight loop of scene changes doesn't burn the main thread on
    # captures we'd never send anyway.
    now = time.monotonic()
    if now - _last_stream_time < _STREAM_MIN_INTERVAL:
        return
    jpeg = capture_viewport_jpeg()
    if jpeg is None or not _should_send_frame(jpeg):
        return
    # Binary frame: 13-byte header (>BHHd: type + width + height + ts) + JPEG
    header = struct.pack(">BHHd", 0x01, 640, 360, time.time())
    client.send_binary(header + jpeg)


# ---------------------------------------------------------------------------
# Level 2 — Event-triggered HD capture
# ---------------------------------------------------------------------------

def capture_hd_png(client: "AnimoraWSClient", trigger: str = "selection_change") -> None:
    """Capture and send a high-resolution viewport image to the backend.

    Triggers (Phase 2 set):
        selection_change  — user selected a different object/component
        render_complete   — F12 / final render finished
        post_script       — AI tool execution just finished (mandatory;
                            fuels the artist's-eye quality check)
        mode_change       — Object/Edit/Sculpt/Pose switch
        heartbeat         — periodic 30s capture for context freshness
    """
    if not client.connected:
        return
    jpeg = capture_viewport_jpeg(width=1920, height=1080, quality=95)
    if jpeg is None:
        return
    import base64
    client.send_json({
        "type": "hd_capture",
        "trigger": trigger,
        "timestamp": time.time(),
        "width": 1920,
        "height": 1080,
        "data": base64.b64encode(jpeg).decode(),
    })
    log.debug("Sent HD capture (trigger=%s, %d bytes)", trigger, len(jpeg))


def capture_post_script_hd() -> None:
    """Public convenience for operators.py — call after script execution."""
    from . import ws_client
    capture_hd_png(ws_client.client, trigger="post_script")


def capture_post_script_hd_bytes() -> tuple[bytes, str] | None:
    """Phase 8 — capture the post-exec viewport and return (jpeg_bytes,
    media_type) WITHOUT sending it as a separate WebSocket message.

    Used by operators.py to embed the HD capture directly inside the
    `tool_result` message payload so the agentic loop's next iteration
    can attach it as image content. Avoids the prior split-message
    correlation problem where tool_result and hd_capture arrived
    separately with no shared tool_use_id linking them.

    Returns None if capture is unavailable (no 3D viewport, off-screen
    buffer fails, etc.) — the caller should still send the tool_result
    without an image and let the model proceed with text-only feedback.
    """
    jpeg = capture_viewport_jpeg(width=1920, height=1080, quality=95)
    if jpeg is None:
        return None
    return jpeg, "image/jpeg"


# ---------------------------------------------------------------------------
# Level 3 — Scene graph serialization
# ---------------------------------------------------------------------------

def serialize_scene_graph() -> dict:
    """Snapshot the current scene for the backend's Scene Intelligence
    Engine. Phase 2 emits richer per-object data — modifier params, vertex
    counts, keyframe counts, shader summary — so the LLM can reason about
    structure without making round-trip queries."""
    scene = bpy.context.scene

    objects = []
    for obj in scene.objects:
        entry: dict = {
            "name": obj.name,
            "type": obj.type,
            "location": list(obj.location),
            "rotation": list(obj.rotation_euler),
            "scale": list(obj.scale),
            "visible": obj.visible_get(),
            "selected": obj.select_get(),
            "modifiers": _serialize_modifiers(obj),
            "parent": obj.parent.name if obj.parent else None,
        }

        # Per-object data extensions (Phase 2)
        data = obj.data
        if data is not None:
            if hasattr(data, "materials"):
                entry["materials"] = [m.name if m else None for m in data.materials]
                entry["material_shaders"] = _summarize_shaders(data.materials)
            if hasattr(data, "vertices"):
                try:
                    entry["vertex_count"] = len(data.vertices)
                except Exception:
                    pass
            if hasattr(data, "polygons"):
                try:
                    entry["polygon_count"] = len(data.polygons)
                except Exception:
                    pass

        # Animation data — keyframe count across all fcurves
        anim = obj.animation_data
        if anim and anim.action:
            try:
                kf = sum(len(fc.keyframe_points) for fc in anim.action.fcurves)
                entry["keyframe_count"] = kf
                entry["action_name"] = anim.action.name
            except Exception:
                pass

        objects.append(entry)

    render = scene.render
    cycles = getattr(scene, "cycles", None)
    render_block: dict = {
        "engine": render.engine,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "film_transparent": render.film_transparent,
    }
    if cycles is not None:
        try:
            render_block["samples"] = cycles.samples
        except Exception:
            pass

    world_block = _summarize_world(scene.world)

    return {
        "scene_name": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "objects": objects,
        "active_object": bpy.context.active_object.name if bpy.context.active_object else None,
        "mode": bpy.context.mode,
        "render": render_block,
        "world": world_block,
    }


def _serialize_modifiers(obj) -> list[dict]:
    """Return modifier list with the most-interesting parameter inlined.

    Phase 2 shape: [{"type": "SUBSURF", "name": "Subdivision", "levels": 2}, ...]
    Older addon shipped: ["SUBSURF", ...] — both shapes are handled by the
    backend's scene_intelligence._describe_object.
    """
    out: list[dict] = []
    for m in obj.modifiers:
        entry: dict = {"type": m.type, "name": m.name}
        # Inline the most-asked-about param per modifier type
        for attr in (
            "levels", "render_levels",                  # Subsurf, Multires
            "thickness",                                # Solidify
            "count", "use_relative_offset",             # Array
            "angle_limit",                              # Decimate
            "ratio",                                    # Decimate ratio mode
            "strength", "mid_level",                    # Displace
            "axis",                                     # Mirror
        ):
            if hasattr(m, attr):
                try:
                    val = getattr(m, attr)
                    if isinstance(val, (int, float, bool, str)):
                        entry[attr] = val
                        break  # first interesting one wins
                except Exception:
                    pass
        out.append(entry)
    return out


def _summarize_shaders(materials) -> list[dict]:
    """For each material, summarize the shader graph head node type."""
    out: list[dict] = []
    for mat in materials:
        if mat is None:
            out.append({"name": None})
            continue
        summary: dict = {"name": mat.name, "use_nodes": mat.use_nodes}
        if mat.use_nodes and mat.node_tree:
            output = next(
                (n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"),
                None,
            )
            if output:
                surface_input = output.inputs.get("Surface")
                if surface_input and surface_input.is_linked:
                    src = surface_input.links[0].from_node
                    summary["surface_shader"] = src.type
        out.append(summary)
    return out


def _summarize_world(world) -> dict:
    """Detect HDRI environment + background color."""
    if world is None:
        return {}
    block: dict = {"name": world.name, "use_hdri": False}
    if world.use_nodes and world.node_tree:
        for node in world.node_tree.nodes:
            if node.type == "TEX_ENVIRONMENT" and node.image:
                block["use_hdri"] = True
                block["hdri_name"] = node.image.name
                break
            if node.type == "BACKGROUND":
                try:
                    block["background_color"] = list(node.inputs["Color"].default_value)
                    block["background_strength"] = node.inputs["Strength"].default_value
                except Exception:
                    pass
    return block


def send_scene_graph(client: "AnimoraWSClient") -> None:
    if not client.connected:
        return
    graph = serialize_scene_graph()
    client.send_json({"type": "scene_graph", "timestamp": time.time(), "graph": graph})


# ---------------------------------------------------------------------------
# Blender handlers
# ---------------------------------------------------------------------------

def _on_depsgraph_update(scene, depsgraph):
    from . import ws_client

    if ws_client.client.connected:
        stream_viewport_frame(ws_client.client)
        _schedule_scene_graph_send(ws_client.client)


def _on_selection_change(scene):
    from . import ws_client

    capture_hd_png(ws_client.client, trigger="selection_change")


def _on_render_complete(scene):
    from . import ws_client

    capture_hd_png(ws_client.client, trigger="render_complete")


def _schedule_scene_graph_send(client: "AnimoraWSClient") -> None:
    global _scene_graph_timer_handle

    def _send_deferred():
        send_scene_graph(client)
        return None

    # Cancel existing pending timer and reschedule (debounce)
    if bpy.app.timers.is_registered(_send_deferred):
        bpy.app.timers.unregister(_send_deferred)
    bpy.app.timers.register(_send_deferred, first_interval=_SCENE_GRAPH_DEBOUNCE)


_HEARTBEAT_INTERVAL_SEC = 30.0
_last_mode_seen: str = ""


def _heartbeat_tick():
    """Periodic HD capture — keeps the backend's vision context fresh
    even when the user is just looking, not editing."""
    from . import ws_client
    if ws_client.client.connected:
        capture_hd_png(ws_client.client, trigger="heartbeat")
    return _HEARTBEAT_INTERVAL_SEC  # reschedule


def _mode_check_tick():
    """Poll for mode changes (Object/Edit/Sculpt/etc.). Blender has no
    direct mode-change handler, so we poll cheaply."""
    global _last_mode_seen
    from . import ws_client
    try:
        mode = bpy.context.mode
    except AttributeError:
        return 1.0
    if mode != _last_mode_seen and _last_mode_seen != "":
        if ws_client.client.connected:
            capture_hd_png(ws_client.client, trigger="mode_change")
    _last_mode_seen = mode
    return 1.0


def register() -> None:
    global _handlers_registered, _last_mode_seen
    if not _handlers_registered:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
        bpy.app.handlers.render_complete.append(_on_render_complete)
        try:
            _last_mode_seen = bpy.context.mode
        except AttributeError:
            _last_mode_seen = ""
        bpy.app.timers.register(_heartbeat_tick, first_interval=_HEARTBEAT_INTERVAL_SEC)
        bpy.app.timers.register(_mode_check_tick, first_interval=1.0)
        _handlers_registered = True


def unregister() -> None:
    global _handlers_registered
    if _handlers_registered:
        if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
        if _on_render_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(_on_render_complete)
        for fn in (_heartbeat_tick, _mode_check_tick):
            if bpy.app.timers.is_registered(fn):
                bpy.app.timers.unregister(fn)
        _handlers_registered = False
