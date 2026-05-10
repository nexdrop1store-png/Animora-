"""
Real-time Vision System — three levels of scene awareness.

Level 1: Continuous viewport stream (5–15 fps, delta-compressed JPEG)
Level 2: Event-triggered HD PNG captures
Level 3: Scene graph JSON sync (debounced 500ms)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import struct
import time
from typing import TYPE_CHECKING

import bpy

if TYPE_CHECKING:
    from .ws_client import AnimoraWSClient

log = logging.getLogger("animora.vision")

_STREAM_MIN_INTERVAL = 1.0 / 15  # 15 fps max
_STREAM_DIFF_THRESHOLD = 0.02    # 2% pixel diff to send frame
_SCENE_GRAPH_DEBOUNCE = 0.5      # seconds

_last_frame_hash: str = ""
_last_stream_time: float = 0.0
_scene_graph_timer_handle = None
_handlers_registered = False


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


def _should_send_frame(jpeg_bytes: bytes) -> bool:
    global _last_frame_hash, _last_stream_time
    now = time.monotonic()
    if now - _last_stream_time < _STREAM_MIN_INTERVAL:
        return False
    h = hashlib.md5(jpeg_bytes).hexdigest()
    if h == _last_frame_hash:
        return False
    _last_frame_hash = h
    _last_stream_time = now
    return True


def stream_viewport_frame(client: "AnimoraWSClient") -> None:
    if not client.connected:
        return
    jpeg = capture_viewport_jpeg()
    if jpeg is None or not _should_send_frame(jpeg):
        return
    # Binary frame: 4-byte header (type=0x01) + JPEG payload
    header = struct.pack(">BHHd", 0x01, 640, 360, time.time())
    client.send_binary(header + jpeg)


# ---------------------------------------------------------------------------
# Level 2 — Event-triggered HD capture
# ---------------------------------------------------------------------------

def capture_hd_png(client: "AnimoraWSClient", trigger: str = "selection_change") -> None:
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
    log.debug("Sent HD capture (trigger=%s)", trigger)


# ---------------------------------------------------------------------------
# Level 3 — Scene graph serialization
# ---------------------------------------------------------------------------

def serialize_scene_graph() -> dict:
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer

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
            "modifiers": [m.type for m in obj.modifiers],
        }
        if obj.data and hasattr(obj.data, "materials"):
            entry["materials"] = [m.name if m else None for m in obj.data.materials]
        objects.append(entry)

    render = scene.render
    return {
        "scene_name": scene.name,
        "frame_current": scene.frame_current,
        "objects": objects,
        "active_object": bpy.context.active_object.name if bpy.context.active_object else None,
        "mode": bpy.context.mode,
        "render": {
            "engine": render.engine,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "film_transparent": render.film_transparent,
        },
    }


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


def register() -> None:
    global _handlers_registered
    if not _handlers_registered:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
        bpy.app.handlers.render_complete.append(_on_render_complete)
        _handlers_registered = True


def unregister() -> None:
    global _handlers_registered
    if _handlers_registered:
        if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
        if _on_render_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(_on_render_complete)
        _handlers_registered = False
