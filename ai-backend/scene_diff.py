"""
Scene-graph diff.

Computes a compact, human-readable + machine-readable diff between two
scene-graph snapshots. The diff goes into the LLM context so the model can
react to what just changed instead of re-reading a 50-object graph every
turn.

Two outputs:

  diff_text(prev, curr)  -> str
      Short, prose-style summary suitable for inlining into a system or
      user message. ~5–30 lines for typical edits. Truncated at 2 KB.

  diff_json(prev, curr)  -> dict
      Structured per-field changes. Use when downstream consumers need to
      reason programmatically (e.g. quality enforcer deciding which checks
      apply, telemetry tagging "modifier_added" events).

Design notes:
- Scene graphs are unordered lists of objects keyed by name. We treat the
  name as identity. Renames look like (deleted name=A) + (added name=B);
  detecting renames properly requires tracking object pointers across
  snapshots — deferred until pointer tracking is added to the addon-side
  serializer in Phase 3.
- Floating-point compares use a tolerance (1e-5) so jittery snapshots from
  Blender's gizmo handler don't flag "modified" every frame.
- The text form is bounded; if too many objects changed (>15) we summarize
  ("23 objects modified") instead of listing each.
"""

from __future__ import annotations

from typing import Any

_FLOAT_TOL = 1e-5
_MAX_LISTED_CHANGES = 15
_MAX_TEXT_LEN = 2048


def _index_by_name(objs: list[dict]) -> dict[str, dict]:
    return {o.get("name", ""): o for o in objs if o.get("name")}


def _floats_changed(a: list | tuple, b: list | tuple) -> bool:
    if len(a) != len(b):
        return True
    return any(abs(float(x) - float(y)) > _FLOAT_TOL for x, y in zip(a, b))


def _object_field_diffs(prev: dict, curr: dict) -> dict[str, Any]:
    """Return the subset of fields that differ between two object dicts."""
    diffs: dict[str, Any] = {}

    for field in ("type", "visible", "selected"):
        if prev.get(field) != curr.get(field):
            diffs[field] = {"from": prev.get(field), "to": curr.get(field)}

    for field in ("location", "rotation", "scale"):
        a, b = prev.get(field) or [], curr.get(field) or []
        if _floats_changed(a, b):
            diffs[field] = {"from": list(a), "to": list(b)}

    pm, cm = prev.get("modifiers", []), curr.get("modifiers", [])
    if pm != cm:
        diffs["modifiers"] = {"from": pm, "to": cm}

    pmat, cmat = prev.get("materials", []), curr.get("materials", [])
    if pmat != cmat:
        diffs["materials"] = {"from": pmat, "to": cmat}

    return diffs


def diff_json(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    """Structured diff. Always returns the same shape regardless of input."""
    result: dict[str, Any] = {
        "added": [],
        "removed": [],
        "modified": [],
        "scene": {},
        "mode_changed": False,
        "frame_changed": False,
        "active_object_changed": False,
    }

    if prev is None:
        result["added"] = [o.get("name", "?") for o in curr.get("objects", [])]
        result["scene"] = {"name": curr.get("scene_name", "")}
        return result

    prev_idx = _index_by_name(prev.get("objects", []))
    curr_idx = _index_by_name(curr.get("objects", []))

    prev_names = set(prev_idx.keys())
    curr_names = set(curr_idx.keys())

    result["added"] = sorted(curr_names - prev_names)
    result["removed"] = sorted(prev_names - curr_names)

    for name in sorted(prev_names & curr_names):
        diffs = _object_field_diffs(prev_idx[name], curr_idx[name])
        if diffs:
            result["modified"].append({"name": name, "changes": diffs})

    if prev.get("mode") != curr.get("mode"):
        result["mode_changed"] = True
        result["mode"] = {"from": prev.get("mode"), "to": curr.get("mode")}
    if prev.get("frame_current") != curr.get("frame_current"):
        result["frame_changed"] = True
        result["frame"] = {"from": prev.get("frame_current"), "to": curr.get("frame_current")}
    if prev.get("active_object") != curr.get("active_object"):
        result["active_object_changed"] = True
        result["active_object"] = {"from": prev.get("active_object"), "to": curr.get("active_object")}

    return result


def diff_text(prev: dict[str, Any] | None, curr: dict[str, Any]) -> str:
    """Prose-style diff, capped at ~2 KB for LLM context."""
    j = diff_json(prev, curr)
    lines: list[str] = []

    if prev is None:
        lines.append(f"Initial snapshot of scene '{j['scene'].get('name', '?')}' with {len(j['added'])} object(s).")
        if j["added"]:
            lines.append("  Objects: " + ", ".join(j["added"][:_MAX_LISTED_CHANGES]))
            if len(j["added"]) > _MAX_LISTED_CHANGES:
                lines.append(f"  …and {len(j['added']) - _MAX_LISTED_CHANGES} more.")
        return "\n".join(lines)[:_MAX_TEXT_LEN]

    if j["added"]:
        if len(j["added"]) <= _MAX_LISTED_CHANGES:
            lines.append(f"Added: {', '.join(j['added'])}")
        else:
            lines.append(f"Added {len(j['added'])} objects (e.g. {', '.join(j['added'][:5])}…)")

    if j["removed"]:
        if len(j["removed"]) <= _MAX_LISTED_CHANGES:
            lines.append(f"Removed: {', '.join(j['removed'])}")
        else:
            lines.append(f"Removed {len(j['removed'])} objects (e.g. {', '.join(j['removed'][:5])}…)")

    mods = j["modified"]
    if mods:
        if len(mods) > _MAX_LISTED_CHANGES:
            lines.append(f"Modified {len(mods)} objects (showing first {_MAX_LISTED_CHANGES}):")
            mods = mods[:_MAX_LISTED_CHANGES]
        for entry in mods:
            change_names = sorted(entry["changes"].keys())
            lines.append(f"  {entry['name']}: {', '.join(change_names)}")

    if j["mode_changed"]:
        lines.append(f"Mode changed: {j['mode']['from']} → {j['mode']['to']}")
    if j["frame_changed"]:
        lines.append(f"Frame changed: {j['frame']['from']} → {j['frame']['to']}")
    if j["active_object_changed"]:
        lines.append(f"Active object: {j['active_object']['from']} → {j['active_object']['to']}")

    if not lines:
        return "No changes since last snapshot."
    return ("\n".join(lines))[:_MAX_TEXT_LEN]
