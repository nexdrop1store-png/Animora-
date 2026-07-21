"""
v1.1 hang-mitigation — resource heuristic coverage for quality_enforcer.py.

These heuristics (subdivision level cap, edit-mode number_cuts cap,
particle count cap, bare range() cap, boolean+dense-mesh escalation)
already existed in production before this test file did — this locks
each one down with a named test (reject-over-threshold + pass-at-
threshold), and adds coverage for the new boolean+dense-mesh-in-script
escalation added alongside the addon-side poly-count guard.

No bpy needed — validate_script() is pure AST/regex analysis.
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

from ai_backend.quality_enforcer import validate_script

# ── Subdivision Surface modifier `.levels` cap (>8 rejected) ─────────────


def test_subsurf_levels_over_cap_rejected():
    v = validate_script("mod.levels = 12\n")
    assert v.ok is False
    assert "Subdivision level" in v.reason


def test_subsurf_levels_at_cap_passes():
    v = validate_script("mod.levels = 8\n")
    assert v.ok is True


# ── Edit-mode mesh.subdivide `number_cuts` cap (>6 rejected) ─────────────


def test_subdivide_number_cuts_over_cap_rejected():
    v = validate_script("bpy.ops.mesh.subdivide(number_cuts=10)\n")
    assert v.ok is False
    assert "number_cuts=10" in v.reason


def test_subdivide_number_cuts_at_cap_passes():
    v = validate_script("bpy.ops.mesh.subdivide(number_cuts=6)\n")
    assert v.ok is True


# ── Particle count cap (>50,000 rejected) ────────────────────────────────


def test_particle_count_over_cap_rejected():
    v = validate_script("psys.settings.count = 100000\n")
    assert v.ok is False
    assert "Particle count" in v.reason


def test_particle_count_at_cap_passes():
    v = validate_script("psys.settings.count = 50000\n")
    assert v.ok is True


def test_particle_count_does_not_flag_unrelated_count_variable():
    # Regex targets `.count = N` (attribute assignment), not a bare
    # `count = N` local variable, to avoid false positives.
    v = validate_script("count = 100000\nprint(count)\n")
    assert v.ok is True


# ── Bare range() literal cap (>5000 rejected) ────────────────────────────


def test_bare_range_over_cap_rejected():
    v = validate_script("for i in range(200000):\n    pass\n")
    assert v.ok is False
    assert "range(200000)" in v.reason


def test_bare_range_at_cap_passes():
    v = validate_script("for i in range(5000):\n    pass\n")
    assert v.ok is True


# ── Boolean modifier: log-only in isolation, rejected when combined ──────
# with a dense mesh built in the SAME script (v1.1 escalation).


def test_bare_boolean_apply_is_log_only_not_rejected():
    script = (
        "mod = obj.modifiers.new('Cut', 'BOOLEAN')\n"
        "bpy.ops.object.modifier_apply(modifier=mod.name)\n"
    )
    v = validate_script(script)
    assert v.ok is True


def test_boolean_apply_after_high_subsurf_levels_rejected():
    script = (
        "mod.levels = 8\n"
        "bmod = obj.modifiers.new('Cut', 'BOOLEAN')\n"
        "bpy.ops.object.modifier_apply(modifier=bmod.name)\n"
    )
    v = validate_script(script)
    assert v.ok is False
    assert "dense mesh" in v.reason
    assert "BOOLEAN" in v.reason


def test_boolean_apply_after_high_number_cuts_rejected():
    script = (
        "bpy.ops.mesh.subdivide(number_cuts=6)\n"
        "bmod = obj.modifiers.new('Cut', 'BOOLEAN')\n"
        "bpy.ops.object.modifier_apply(modifier=bmod.name)\n"
    )
    v = validate_script(script)
    assert v.ok is False
    assert "dense mesh" in v.reason


def test_boolean_apply_after_low_subdivision_still_passes():
    # Below the escalation threshold (levels<5, cuts<4) — a boolean on a
    # lightly-subdivided mesh is common and legitimate; stays log-only.
    script = (
        "mod.levels = 2\n"
        "bmod = obj.modifiers.new('Cut', 'BOOLEAN')\n"
        "bpy.ops.object.modifier_apply(modifier=bmod.name)\n"
    )
    v = validate_script(script)
    assert v.ok is True
