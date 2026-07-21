"""
v1.2 — SPEC-builder latency heuristic (spec.py::should_skip_spec_for_trivial_prompt).

Founder decision (v1.x plan): skip the ~20s SPEC call for genuinely
trivial single-primitive asks, keep it for multi-part/descriptive
requests. No API calls — pure function, no bpy needed.
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
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.orchestrator.spec import should_skip_spec_for_trivial_prompt


# ── Trivial prompts: skip SPEC ────────────────────────────────────────────


def test_make_a_cube_skips_spec():
    assert should_skip_spec_for_trivial_prompt("make a cube", 0.55) is True


def test_add_a_sphere_skips_spec():
    assert should_skip_spec_for_trivial_prompt("add a sphere", 0.55) is True


def test_create_a_red_cube_skips_spec():
    assert should_skip_spec_for_trivial_prompt("create a red cube", 0.55) is True


def test_build_a_cylinder_skips_spec():
    assert should_skip_spec_for_trivial_prompt("build a cylinder 2m tall", 0.55) is True


# ── Descriptive / multi-part prompts: keep SPEC ──────────────────────────


def test_cozy_living_room_keeps_spec():
    assert should_skip_spec_for_trivial_prompt(
        "build a cozy living room with warm lighting", 0.55,
    ) is False


def test_hero_car_keeps_spec():
    assert should_skip_spec_for_trivial_prompt(
        "build me a hero Lamborghini Urus, studio-shot quality", 0.55,
    ) is False


def test_beach_scene_keeps_spec():
    assert should_skip_spec_for_trivial_prompt(
        "make a beach scene with palm trees and water", 0.55,
    ) is False


def test_short_but_non_primitive_prompt_keeps_spec():
    # Short (<= word ceiling) but doesn't mention a bare primitive noun
    # — e.g. a character/creature ask — should still get planned.
    assert should_skip_spec_for_trivial_prompt("model a low-poly dragon", 0.55) is False


def test_high_complexity_estimate_keeps_spec_even_if_short_and_primitive_worded():
    # A real (non-fast-path) Haiku classification above the ceiling
    # overrides the noun heuristic entirely.
    assert should_skip_spec_for_trivial_prompt("make a cube", 0.9) is False


def test_empty_prompt_keeps_spec():
    # Defensive: an empty message shouldn't be treated as trivial-skip
    # (nothing meaningful to pattern-match against).
    assert should_skip_spec_for_trivial_prompt("", 0.55) is False


def test_long_prompt_mentioning_primitive_keeps_spec():
    # Over the word-count ceiling even though it mentions "cube" —
    # length alone signals a more elaborate ask worth planning.
    prompt = (
        "make a cube that has been weathered and rusted over decades "
        "sitting abandoned in a post-apocalyptic desert wasteland"
    )
    assert should_skip_spec_for_trivial_prompt(prompt, 0.55) is False
