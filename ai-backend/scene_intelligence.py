"""
Scene Intelligence Engine.

Converts raw scene graph snapshots into human-readable context strings
that are prepended to every LLM system prompt.
"""

from __future__ import annotations

from typing import Any


def build_scene_context(graph: dict[str, Any], prev_graph: dict[str, Any] | None = None) -> str:
    if not graph:
        return "No scene information available."

    lines: list[str] = []

    objects = graph.get("objects", [])
    selected = [o for o in objects if o.get("selected")]
    visible = [o for o in objects if o.get("visible")]

    lines.append(f"Scene: {graph.get('scene_name', 'Untitled')} | Frame: {graph.get('frame_current', 0)}")
    lines.append(f"Objects: {len(objects)} total, {len(visible)} visible, {len(selected)} selected")

    active = graph.get("active_object")
    if active:
        active_obj = next((o for o in objects if o["name"] == active), None)
        if active_obj:
            mods = active_obj.get("modifiers", [])
            mats = active_obj.get("materials", [])
            lines.append(
                f"Active object: {active} ({active_obj['type']})"
                + (f" | Modifiers: {', '.join(mods)}" if mods else "")
                + (f" | Materials: {', '.join(m for m in mats if m)}" if mats else "")
            )

    lines.append(f"Mode: {graph.get('mode', 'OBJECT')}")

    render = graph.get("render", {})
    if render:
        lines.append(
            f"Render: {render.get('engine', '?')} | "
            f"{render.get('resolution_x', '?')}x{render.get('resolution_y', '?')}"
        )

    if prev_graph:
        changed = _diff_objects(prev_graph.get("objects", []), objects)
        if changed:
            lines.append(f"Recent changes: {changed}")

    return "\n".join(lines)


def _diff_objects(prev: list[dict], curr: list[dict]) -> str:
    prev_names = {o["name"] for o in prev}
    curr_names = {o["name"] for o in curr}
    added = curr_names - prev_names
    removed = prev_names - curr_names
    parts = []
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    return "; ".join(parts) if parts else ""


def estimate_task_complexity(user_message: str, scene_graph: dict) -> float:
    """Heuristic 0–1 score. High score → route to Opus."""
    score = 0.0
    msg_lower = user_message.lower()

    complex_keywords = [
        "rig", "rigging", "armature", "shader graph", "geometry nodes",
        "simulation", "fluid", "smoke", "particles", "physics",
        "full scene", "entire character", "procedural", "complex",
    ]
    for kw in complex_keywords:
        if kw in msg_lower:
            score += 0.15

    # Long messages tend to be complex
    if len(user_message) > 300:
        score += 0.2

    # Many objects = complex scene
    obj_count = len(scene_graph.get("objects", []))
    if obj_count > 50:
        score += 0.2
    elif obj_count > 20:
        score += 0.1

    return min(score, 1.0)
