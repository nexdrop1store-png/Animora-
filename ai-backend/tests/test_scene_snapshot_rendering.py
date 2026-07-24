"""
Bug 2 regression — the agentic loop must feed the model the CURRENT scene
hierarchy each iteration, not the scene graph baked into the system prompt
at turn start (that staleness is why "make the sofa smooth" touched only
one child mesh).

The addon attaches a compact name/type/parent snapshot to each mutating
tool_result; build_tool_result_message renders it ONCE per iteration as a
trailing text block. These are pure functions — no bpy, no live Blender.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("ANIMORA_ENV", "dev")
os.environ.setdefault("ANIMORA_LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")

_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.orchestrator.context_builder import (  # noqa: E402
    _format_scene_hierarchy,
    build_tool_result_message,
)

_SOFA = [
    {"name": "Sofa", "type": "EMPTY", "parent": None},
    {"name": "Sofa_Base", "type": "MESH", "parent": "Sofa"},
    {"name": "Cushion_L", "type": "MESH", "parent": "Sofa"},
    {"name": "Pillow", "type": "MESH", "parent": "Sofa"},
    {"name": "Floor", "type": "MESH", "parent": None},
]


def test_hierarchy_groups_children_under_parent():
    text = _format_scene_hierarchy(_SOFA)
    assert "Sofa (EMPTY)" in text
    # All three sofa child meshes are shown indented under the parent —
    # this is what lets "smooth the sofa" resolve to every constituent mesh.
    for child in ("Sofa_Base", "Cushion_L", "Pillow"):
        assert f"    - {child} (MESH)" in text
    assert "- Floor (MESH)" in text
    assert "authoritative" in text  # tells the model to prefer it over stale context


def test_empty_or_bad_snapshot_renders_nothing():
    assert _format_scene_hierarchy(None) == ""
    assert _format_scene_hierarchy([]) == ""
    assert _format_scene_hierarchy("not a list") == ""


def test_deeply_nested_object_not_dropped():
    # A grandchild (parent is itself a child, not a root) must still appear.
    snap = [
        {"name": "Rig", "type": "EMPTY", "parent": None},
        {"name": "Arm", "type": "EMPTY", "parent": "Rig"},
        {"name": "Hand", "type": "MESH", "parent": "Arm"},
    ]
    text = _format_scene_hierarchy(snap)
    assert "Hand" in text  # not silently dropped


def test_tool_result_message_appends_scene_block_once():
    outcomes = {
        "t1": {"tool_use_id": "t1", "is_error": False, "output": "made base",
               "scene_snapshot": [{"name": "A", "type": "MESH", "parent": None}]},
        "t2": {"tool_use_id": "t2", "is_error": False, "output": "made cushion",
               "scene_snapshot": _SOFA},  # freshest — should win
    }
    msg = build_tool_result_message(outcomes, ["t1", "t2"])
    blocks = msg["content"]
    tool_results = [b for b in blocks if b.get("type") == "tool_result"]
    scene_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(tool_results) == 2  # one per tool_use, order preserved
    assert len(scene_blocks) == 1  # exactly ONE scene block, not per-tool
    # Uses the freshest (t2) snapshot — the full sofa, not just "A".
    assert "Sofa" in scene_blocks[0]["text"]
    assert "Cushion_L" in scene_blocks[0]["text"]


def test_tool_result_message_no_snapshot_no_scene_block():
    outcomes = {"t1": {"tool_use_id": "t1", "is_error": False, "output": "ok"}}
    msg = build_tool_result_message(outcomes, ["t1"])
    assert all(b.get("type") != "text" for b in msg["content"])
