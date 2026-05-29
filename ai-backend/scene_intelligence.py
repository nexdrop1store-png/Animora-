"""
Scene Intelligence Engine.

Converts raw scene-graph snapshots into the structured context that gets
attached to LLM calls. Two main outputs:

  build_scene_context(graph, prev_graph)
      Human-readable prose summary suitable for inlining into the master
      prompt's {scene_context} placeholder. ~5–20 lines.

  build_scene_context_block(graph, prev_graph)
      Dict with the prose summary AND a machine-readable diff (for use
      cases like quality enforcement that need structured signals, not
      just text). Used by the Phase 2 context_builder.

  estimate_task_complexity(user_message, scene_graph)
      Heuristic 0–1 score routed into the model selector.

The serializer on the addon side (animora_panel/vision.py) is the source
of truth for the graph's shape. Phase 2 extends what fields the serializer
emits (modifier params, shader summary, keyframe counts); the consumers in
this file gracefully handle missing fields so an older addon talking to a
newer backend still works.
"""

from __future__ import annotations

from typing import Any

from .scene_diff import diff_json, diff_text

# Keywords that strongly suggest the user wants something Opus-tier.
_COMPLEX_KEYWORDS = (
    "rig", "rigging", "armature", "shader graph", "geometry nodes",
    "simulation", "fluid", "smoke", "particles", "physics",
    "full scene", "entire character", "procedural", "destruction",
    "fracture", "cloth", "hair", "groom",
)

# Dense-scene keywords — push complexity higher even without other signals.
_DENSE_SCENE_KEYWORDS = (
    "forest", "beach", "city", "town", "village", "jungle",
    "landscape", "environment", "world",
)


def build_scene_context(graph: dict[str, Any], prev_graph: dict[str, Any] | None = None) -> str:
    """Prose summary inlined into the master prompt."""
    if not graph:
        return "No scene information available yet."

    lines: list[str] = []
    objects = graph.get("objects", [])
    selected = [o for o in objects if o.get("selected")]
    visible = [o for o in objects if o.get("visible")]

    lines.append(
        f"Scene: {graph.get('scene_name', 'Untitled')} | Frame: {graph.get('frame_current', 0)}"
    )
    lines.append(
        f"Objects: {len(objects)} total, {len(visible)} visible, {len(selected)} selected"
    )

    active = graph.get("active_object")
    if active:
        active_obj = next((o for o in objects if o.get("name") == active), None)
        if active_obj:
            lines.append(_describe_object(active_obj, label="Active object"))

    if selected and (not active or len(selected) > 1):
        for obj in selected[:3]:
            if obj.get("name") != active:
                lines.append(_describe_object(obj, label="Selected"))
        if len(selected) > 3:
            lines.append(f"  …and {len(selected) - 3} more selected.")

    lines.append(f"Mode: {graph.get('mode', 'OBJECT')}")

    render = graph.get("render", {})
    if render:
        eng = render.get("engine", "?")
        res = f"{render.get('resolution_x', '?')}x{render.get('resolution_y', '?')}"
        samples = render.get("samples")
        bits = [f"Render: {eng} | {res}"]
        if samples is not None:
            bits.append(f"{samples} samples")
        if render.get("film_transparent"):
            bits.append("transparent film")
        lines.append(" | ".join(bits))

    world = graph.get("world", {})
    if world and world.get("use_hdri"):
        lines.append(f"World: HDRI '{world.get('hdri_name', '?')}'")

    if prev_graph:
        change_summary = diff_text(prev_graph, graph)
        if change_summary and change_summary != "No changes since last snapshot.":
            lines.append("Recent changes:")
            for change_line in change_summary.splitlines():
                lines.append("  " + change_line)

    return "\n".join(lines)


def _describe_object(obj: dict[str, Any], label: str) -> str:
    name = obj.get("name", "?")
    type_ = obj.get("type", "?")
    parts = [f"{label}: {name} ({type_})"]

    mods = obj.get("modifiers", [])
    if mods:
        # Phase 2: modifiers may be either ["SUBSURF", "MIRROR"] (old) or
        # [{"type": "SUBSURF", "levels": 2}, ...] (new). Handle both.
        rendered_mods: list[str] = []
        for m in mods:
            if isinstance(m, dict):
                t = m.get("type", "?")
                # Pluck the most-interesting param to display
                key_param = next(
                    (f"{k}={v}" for k, v in m.items() if k not in ("type", "name") and isinstance(v, (int, float, str, bool))),
                    None,
                )
                rendered_mods.append(f"{t}({key_param})" if key_param else t)
            else:
                rendered_mods.append(str(m))
        parts.append("modifiers: " + ", ".join(rendered_mods))

    mats = obj.get("materials", [])
    real_mats = [m for m in mats if m]
    if real_mats:
        parts.append("materials: " + ", ".join(real_mats))

    vc = obj.get("vertex_count")
    if isinstance(vc, int) and vc > 0:
        parts.append(f"{vc:,} verts")

    kf = obj.get("keyframe_count")
    if isinstance(kf, int) and kf > 0:
        parts.append(f"{kf} keyframes")

    return "  " + " | ".join(parts)


def build_scene_context_block(
    graph: dict[str, Any],
    prev_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Returns both the prose summary and the structured diff. Used by
    the Phase 2 context_builder when assembling the LLM message stack."""
    return {
        "text": build_scene_context(graph, prev_graph),
        "diff": diff_json(prev_graph, graph) if graph else None,
        "object_count": len(graph.get("objects", [])) if graph else 0,
        "mode": graph.get("mode", "OBJECT") if graph else "OBJECT",
    }


def estimate_task_complexity(user_message: str, scene_graph: dict) -> float:
    """Heuristic 0–1 score. High → route to Opus.

    Phase 4: if the orchestrator stuffed the intent classifier's
    `complexity_estimate` into the scene_graph dict under
    `__intent_complexity`, prefer that — it's a more informed signal
    than this keyword heuristic. The keyword path remains for any
    code path that calls this directly without an intent.
    """
    classifier_score = scene_graph.get("__intent_complexity")
    if isinstance(classifier_score, (int, float)):
        return max(0.0, min(1.0, float(classifier_score)))

    score = 0.0
    msg_lower = user_message.lower()

    for kw in _COMPLEX_KEYWORDS:
        if kw in msg_lower:
            score += 0.15

    for kw in _DENSE_SCENE_KEYWORDS:
        if kw in msg_lower:
            score += 0.20

    if len(user_message) > 300:
        score += 0.2
    elif len(user_message) > 600:
        score += 0.35  # genuinely complex multi-paragraph request

    obj_count = len(scene_graph.get("objects", []))
    if obj_count > 50:
        score += 0.2
    elif obj_count > 20:
        score += 0.1

    return min(score, 1.0)
