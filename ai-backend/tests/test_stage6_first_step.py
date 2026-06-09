"""
Stage 6 — First-step hardening verification.

Two surfaces:
  • the deterministic `first_step_diagnosis` (verdict + human reason) that
    backs both the eval metric (Stage 7) and the runtime gate, and
  • the gate-decision predicate the streaming loop keys on (verdict is
    False AND something was created AND no escape hatch).

All offline: mock tool-call dicts, no API credits, no Blender. The full
live gate (message injection + next_tool_choice) is exercised in the panel
smoke; here we lock the deterministic decision it depends on.

Run:
    pytest ai-backend/tests/test_stage6_first_step.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("ANIMORA_ENV", "dev")

_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.orchestrator.critic import (  # noqa: E402
    first_step_diagnosis,
    first_step_ok,
)


def _tc(tool_name, **inp):
    return {"name": tool_name, "input": inp}


# ── Sound foundations ──────────────────────────────────────────────────
def test_sane_first_create_is_ok():
    calls = [_tc("create_primitive", kind="cube", name="Base",
                 location=[0, 0, 0], scale=[1.5, 1.5, 0.4])]
    verdict, reason = first_step_diagnosis(calls)
    assert verdict is True
    assert reason == ""


def test_read_only_then_create_is_ok():
    # Inspecting first is fine; the first *real* action is the create.
    calls = [
        _tc("get_scene_info"),
        _tc("viewport_screenshot"),
        _tc("create_primitive", kind="plane", name="Ground",
            location=[0, 0, 0], scale=[10, 10, 1]),
    ]
    assert first_step_diagnosis(calls)[0] is True


# ── Bad foundations — each carries a targeted reason ───────────────────
def test_exploded_scale_flagged_with_reason():
    calls = [_tc("create_primitive", kind="cube", name="Base",
                 location=[0, 0, 0], scale=[900, 1, 1])]
    verdict, reason = first_step_diagnosis(calls)
    assert verdict is False
    assert "exploded" in reason
    assert "900" in reason  # the actual offending magnitude is surfaced


def test_microscopic_scale_flagged_with_reason():
    calls = [_tc("create_primitive", kind="plane", name="Base",
                 location=[0, 0, 0], scale=[0.0005, 0.0005, 1])]
    verdict, reason = first_step_diagnosis(calls)
    assert verdict is False
    assert "microscop" in reason


def test_opening_with_material_flagged():
    verdict, reason = first_step_diagnosis(
        [_tc("apply_material", object="X", base_color=[1, 0, 0, 1])])
    assert verdict is False
    assert "apply_material" in reason and "before any geometry" in reason


def test_opening_with_parent_flagged():
    verdict, reason = first_step_diagnosis(
        [_tc("set_parent", child="A", parent="B")])
    assert verdict is False
    assert "parent" in reason


def test_opening_with_transform_flagged():
    verdict, reason = first_step_diagnosis(
        [_tc("set_transform", object="A", location=[1, 0, 0])])
    assert verdict is False
    assert "set_transform" in reason


# ── Unjudgable foundations → None (gate must NOT fire) ─────────────────
def test_escape_hatch_is_none():
    verdict, reason = first_step_diagnosis(
        [_tc("execute_animora_code", script="import bpy")])
    assert verdict is None
    assert reason == ""


def test_no_steps_is_none():
    assert first_step_diagnosis([])[0] is None


# ── first_step_ok stays a thin verdict wrapper (Stage 7 metric) ────────
def test_first_step_ok_matches_diagnosis_verdict():
    for calls in (
        [_tc("create_primitive", kind="cube", scale=[1, 1, 1])],
        [_tc("create_primitive", kind="cube", scale=[900, 1, 1])],
        [_tc("execute_animora_code", script="x")],
        [],
    ):
        assert first_step_ok(calls) is first_step_diagnosis(calls)[0]


# ── The gate-decision predicate the streaming loop keys on ─────────────
def _gate_would_fire(turn_tool_calls, *, atomic_create_count,
                     used_escape_hatch, already_attempted):
    """Mirror of the streaming First-Step Gate guard (sans iteration
    bound, which is loop bookkeeping not a quality decision)."""
    verdict, _reason = first_step_diagnosis(turn_tool_calls)
    return (
        verdict is False
        and atomic_create_count > 0
        and not used_escape_hatch
        and not already_attempted
    )


def test_gate_fires_on_exploded_foundation():
    calls = [_tc("create_primitive", kind="cube", scale=[900, 1, 1])]
    assert _gate_would_fire(calls, atomic_create_count=1,
                            used_escape_hatch=False, already_attempted=False)


def test_gate_silent_on_sound_foundation():
    calls = [_tc("create_primitive", kind="cube", scale=[1.5, 1.5, 0.4])]
    assert not _gate_would_fire(calls, atomic_create_count=1,
                                used_escape_hatch=False, already_attempted=False)


def test_gate_silent_for_escape_hatch():
    # Bad-looking inputs but via the opaque hatch → verdict None → silent.
    calls = [_tc("execute_animora_code", script="bpy.ops...")]
    assert not _gate_would_fire(calls, atomic_create_count=0,
                                used_escape_hatch=True, already_attempted=False)


def test_gate_is_single_shot():
    calls = [_tc("create_primitive", kind="cube", scale=[900, 1, 1])]
    assert not _gate_would_fire(calls, atomic_create_count=1,
                                used_escape_hatch=False, already_attempted=True)


def test_gate_silent_when_nothing_created():
    # A material-first opener with atomic_create_count 0 (the apply_material
    # didn't create geometry) → don't nag about a foundation that was never
    # attempted as a create.
    calls = [_tc("apply_material", object="X", base_color=[1, 0, 0, 1])]
    assert not _gate_would_fire(calls, atomic_create_count=0,
                                used_escape_hatch=False, already_attempted=False)
