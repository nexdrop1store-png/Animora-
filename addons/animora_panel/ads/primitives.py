"""
Animora Design System — GPU draw primitives.

Low-level shape helpers built on Blender's `gpu` + `gpu_extras.batch`
modules. Each function takes pixel-space coordinates relative to the
current region (origin = bottom-left, matching Blender's GPU convention)
and an RGBA colour tuple, and emits one batch.

Caller is responsible for:
  • having a valid GL context (called from inside a POST_PIXEL handler
    on a registered SpaceType)
  • saving/restoring any GPU state they need preserved — these helpers
    set blend + line width as needed and reset to defaults afterward.

Why hand-rolled instead of a UI library? Blender's bpy.types.Panel
machinery doesn't let us style backgrounds at all — fills are determined
by the theme. Drawing directly via `gpu` is how panel chrome extensions
(border_glow, Hard Ops HUDs, etc.) achieve custom visuals on top of the
bpy-drawn content. ADS centralises those calls so widgets don't each
roll their own.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("animora.ads")

# ── GPU module accessor (lazy + cached) ────────────────────────────────

_gpu_mod = None
_batch_for_shader = None


def _gpu():
    """Resolve `gpu` + `gpu_extras.batch.batch_for_shader`.

    Returns (gpu_module, batch_for_shader) or None if unavailable (e.g.
    running outside Blender). Cached after first success so we don't
    re-import on every draw call."""
    global _gpu_mod, _batch_for_shader
    if _gpu_mod is not None:
        return _gpu_mod, _batch_for_shader
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except ImportError:
        return None
    _gpu_mod = gpu
    _batch_for_shader = batch_for_shader
    return _gpu_mod, _batch_for_shader


# ── Geometry helpers ───────────────────────────────────────────────────

def _rounded_corner_points(cx: float, cy: float, radius: float,
                           start_angle: float, end_angle: float,
                           segments: int = 8) -> list[tuple[float, float]]:
    """Sample points along a circular arc from start_angle to end_angle
    (both in radians, CCW). Inclusive of both endpoints. Used to build
    rounded corners on rectangles."""
    if segments < 1:
        segments = 1
    pts = []
    for i in range(segments + 1):
        t = i / segments
        a = start_angle + (end_angle - start_angle) * t
        pts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))
    return pts


def _rounded_rect_outline(x: float, y: float, w: float, h: float,
                          radius: float) -> list[tuple[float, float]]:
    """Return the outline polyline (closed) of a rounded rectangle.

    Coordinate convention: (x, y) is the bottom-left corner; (x+w, y+h)
    is the top-right corner. Radius is clamped to half the shorter side."""
    r = max(0.0, min(radius, min(w, h) * 0.5))
    if r <= 0.5:
        # Degenerate — emit a plain rectangle as 4 corners
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]

    pts: list[tuple[float, float]] = []
    # Bottom-right corner: arc from -90° to 0°
    pts.extend(_rounded_corner_points(x + w - r, y + r, r,
                                      -math.pi / 2, 0.0))
    # Top-right corner: arc from 0° to 90°
    pts.extend(_rounded_corner_points(x + w - r, y + h - r, r,
                                      0.0, math.pi / 2))
    # Top-left corner: arc from 90° to 180°
    pts.extend(_rounded_corner_points(x + r, y + h - r, r,
                                      math.pi / 2, math.pi))
    # Bottom-left corner: arc from 180° to 270°
    pts.extend(_rounded_corner_points(x + r, y + r, r,
                                      math.pi, math.pi * 1.5))
    # Close the loop
    pts.append(pts[0])
    return pts


# ── Public draw primitives ─────────────────────────────────────────────

def line(x0: float, y0: float, x1: float, y1: float,
         color: tuple[float, float, float, float],
         width: float = 1.0) -> None:
    """Draw a single line segment."""
    g = _gpu()
    if g is None:
        return
    gpu, batch_for_shader = g

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": [(x0, y0), (x1, y1)]})

    gpu.state.line_width_set(width)
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def rounded_rect_outline(x: float, y: float, w: float, h: float,
                         radius: float,
                         color: tuple[float, float, float, float],
                         width: float = 1.0) -> None:
    """Draw an unfilled rounded-rect outline.

    Cheap (line strip). Use this for accent borders and chip outlines."""
    g = _gpu()
    if g is None or w <= 0 or h <= 0:
        return
    gpu, batch_for_shader = g

    pts = _rounded_rect_outline(x, y, w, h, radius)
    # Build line-strip pairs
    segs: list[tuple[float, float]] = []
    for i in range(len(pts) - 1):
        segs.append(pts[i])
        segs.append(pts[i + 1])

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": segs})

    gpu.state.line_width_set(width)
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def horizontal_strip(x: float, y: float, w: float, h: float,
                     color: tuple[float, float, float, float]) -> None:
    """Draw a solid filled horizontal strip. Use this for accent bands.

    POST_PIXEL handlers draw OVER bpy content; large solid fills will
    cover up text. Reserve for thin strips (h < 8 px) or use a low alpha
    in `color` so the underlying UI remains visible."""
    g = _gpu()
    if g is None or w <= 0 or h <= 0:
        return
    gpu, batch_for_shader = g

    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    indices = [(0, 1, 2), (0, 2, 3)]

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": verts}, indices=indices)

    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def vertical_gradient_strip(x: float, y: float, w: float, h: float,
                            color_bottom: tuple[float, float, float, float],
                            color_top: tuple[float, float, float, float]) -> None:
    """Draw a vertically-graduated filled strip.

    Uses the SMOOTH_COLOR built-in shader so each vertex carries its own
    colour and the GPU interpolates across the quad. Good for soft glow
    underlights where one edge fades to transparent."""
    g = _gpu()
    if g is None or w <= 0 or h <= 0:
        return
    gpu, batch_for_shader = g

    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    colors = [color_bottom, color_bottom, color_top, color_top]
    indices = [(0, 1, 2), (0, 2, 3)]

    shader = gpu.shader.from_builtin("SMOOTH_COLOR")
    batch = batch_for_shader(
        shader, "TRIS",
        {"pos": verts, "color": colors},
        indices=indices,
    )

    gpu.state.blend_set("ALPHA")
    shader.bind()
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def rounded_rect_fill(x: float, y: float, w: float, h: float,
                      radius: float,
                      color: tuple[float, float, float, float]) -> None:
    """Draw a FILLED rounded rectangle (triangle fan around the centroid).

    This is the message-bubble / glass-card primitive. It draws behind
    bpy widgets only when called from a 'PRE_VIEW' handler (the native
    ANIMORA region dispatches PRE_VIEW between background clear and the
    widget pass); from POST_PIXEL it covers content — use low alpha."""
    g = _gpu()
    if g is None or w <= 0 or h <= 0:
        return
    gpu, batch_for_shader = g

    outline = _rounded_rect_outline(x, y, w, h, radius)
    center = (x + w * 0.5, y + h * 0.5)
    verts = [center] + outline  # outline is already a closed loop
    indices = [(0, i, i + 1) for i in range(1, len(verts) - 1)]

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": verts}, indices=indices)

    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def image_texture(texture, x: float, y: float, w: float, h: float,
                  alpha: float = 1.0) -> None:
    """Draw a gpu.types.GPUTexture as a quad at (x, y, w, h), bottom-left
    origin. Native-resolution — no upscaling blur (unlike template_icon).
    `alpha` multiplies the texture for fade transitions."""
    g = _gpu()
    if g is None or w <= 0 or h <= 0 or texture is None:
        return
    gpu, batch_for_shader = g

    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
    indices = [(0, 1, 2), (0, 2, 3)]

    if alpha >= 0.999:
        shader = gpu.shader.from_builtin("IMAGE")
        batch = batch_for_shader(
            shader, "TRIS", {"pos": verts, "texCoord": uvs}, indices=indices,
        )
        gpu.state.blend_set("ALPHA")
        shader.bind()
        shader.uniform_sampler("image", texture)
        batch.draw(shader)
        gpu.state.blend_set("NONE")
    else:
        # IMAGE_COLOR multiplies the sampled texel by a uniform colour —
        # (1,1,1,alpha) gives a uniform fade.
        shader = gpu.shader.from_builtin("IMAGE_COLOR")
        batch = batch_for_shader(
            shader, "TRIS", {"pos": verts, "texCoord": uvs}, indices=indices,
        )
        gpu.state.blend_set("ALPHA")
        shader.bind()
        shader.uniform_sampler("image", texture)
        shader.uniform_float("color", (1.0, 1.0, 1.0, alpha))
        batch.draw(shader)
        gpu.state.blend_set("NONE")


# Set by load_gpu_texture on failure so callers that need diagnosability
# (e.g. onboarding's all-textures-failed breadcrumb) can retrieve the
# actual exception text without changing this function's return shape.
last_texture_error: str | None = None


def load_gpu_texture(path: str):
    """Load a PNG into a GPUTexture via a bpy.data.image. Returns
    (texture, image) or (None, None). Keep a reference to `image` — the
    GPUTexture borrows its data."""
    global last_texture_error
    try:
        import bpy
        import gpu
    except ImportError:
        return None, None
    try:
        img = bpy.data.images.load(path, check_existing=True)
        tex = gpu.texture.from_image(img)
        return tex, img
    except Exception as exc:
        last_texture_error = f"{path}: {exc}"
        log.warning("load_gpu_texture failed for %s: %s", path, exc)
        return None, None


def soft_shadow_rounded(x: float, y: float, w: float, h: float,
                        radius: float, spread: float = 6.0,
                        alpha: float = 0.16, layers: int = 3) -> None:
    """Fake drop shadow: stacked translucent black rounded fills expanding
    outward. Cheap and resolution-independent."""
    if layers < 1:
        return
    for i in range(layers, 0, -1):
        grow = spread * i / layers
        a = alpha * (layers - i + 1) / (layers * 2)
        rounded_rect_fill(
            x - grow, y - grow, w + grow * 2, h + grow * 2,
            radius + grow, (0.0, 0.0, 0.0, a),
        )


def text(x: float, y: float, size: float,
         color: tuple[float, float, float, float], value: str,
         *, font_id: int = 0) -> float:
    """Draw text via blf at (x, y) baseline. Returns the drawn width.

    Custom typography for GPU-drawn chrome (uses Blender's UI font by
    default — pass a blf.load()ed font_id for a shipped TTF)."""
    try:
        import blf
    except ImportError:
        return 0.0
    blf.size(font_id, size)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0.0)
    blf.draw(font_id, value)
    width, _h = blf.dimensions(font_id, value)
    return width


def text_width(size: float, value: str, *, font_id: int = 0) -> float:
    """Measure text width without drawing (layout passes)."""
    try:
        import blf
    except ImportError:
        return 0.0
    blf.size(font_id, size)
    width, _h = blf.dimensions(font_id, value)
    return width


def region_size() -> tuple[int, int] | None:
    """Return the current region's (width, height) in pixels, or None if
    no region is active (e.g. called outside a draw handler)."""
    try:
        import bpy
        region = bpy.context.region
        if region is None or region.width < 10 or region.height < 10:
            return None
        return region.width, region.height
    except Exception:
        return None
