"""
Harness modernization — atomic-tool scoring bridge.

The eval's benchmarks + structural counters were written for the legacy
bpy-script era. `render_tool_calls_as_bpy` translates atomic tool calls
into the bpy-equivalent text those regexes understand, so an atomic build
scores exactly as the equivalent script would. These tests lock that
bridge: a build expressed as atomic tools must satisfy the same checks a
correct script would, and the stale-regex false-failures are gone.

Run:
    pytest ai-backend/tests/test_eval_atomic_scoring.py -v
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

from ai_backend.eval.benchmarks import Benchmark  # noqa: E402
from ai_backend.eval.scoring import (  # noqa: E402
    count_distinct_objects,
    count_distinct_positions,
    count_light_sources,
    has_material_setup,
    has_material_variety,
    has_meaningful_name,
    has_modifiers,
    render_tool_calls_as_bpy,
    score_against_benchmark,
)


def _tc(tool_name, **inp):
    return {"name": tool_name, "input": inp}


# ── primitive-kind translation ─────────────────────────────────────────
def test_cube_renders_to_primitive_cube_add():
    text = render_tool_calls_as_bpy([_tc("create_primitive", kind="cube",
                                         name="Seat", location=[0, 0, 0])])
    assert "primitive_cube_add(" in text
    assert 'obj.name = "Seat"' in text
    assert has_meaningful_name(text)


def test_sphere_synonym_maps_to_uv_sphere():
    text = render_tool_calls_as_bpy([_tc("create_primitive", kind="sphere",
                                         name="Ball", location=[0, 0, 0])])
    assert "primitive_uv_sphere_add(" in text


def test_forbidden_op_does_not_appear_for_cube_only_build():
    # A cube benchmark forbids uv_sphere; a cube-only atomic build must
    # not trip it (the regression the benchmark was built to catch).
    text = render_tool_calls_as_bpy([_tc("create_primitive", kind="cube",
                                         name="Box", location=[0, 0, 0])])
    assert "primitive_uv_sphere_add(" not in text


# ── structural counters work off atomic calls ──────────────────────────
def test_distinct_objects_and_positions_from_atomic():
    calls = [
        _tc("create_primitive", kind="cube", name="Seat", location=[0, 0, 0.4]),
        _tc("create_primitive", kind="cube", name="Back", location=[0, -0.4, 1.0]),
        _tc("create_primitive", kind="cylinder", name="Leg1", location=[0.4, 0.4, 0.2]),
    ]
    text = render_tool_calls_as_bpy(calls)
    assert count_distinct_objects(text) == 3
    assert count_distinct_positions(text) == 3


def test_material_setup_and_variety_from_atomic():
    calls = [
        _tc("apply_material", object="Seat", base_color=[0.8, 0.1, 0.1, 1]),
        _tc("apply_material", object="Frame", base_color=[0.1, 0.1, 0.1, 1]),
    ]
    text = render_tool_calls_as_bpy(calls)
    assert has_material_setup(text)
    assert has_material_variety(text)          # two distinct colours


def test_same_color_everywhere_is_single_material():
    calls = [
        _tc("apply_material", object="A", base_color=[0.5, 0.5, 0.5, 1]),
        _tc("apply_material", object="B", base_color=[0.5, 0.5, 0.5, 1]),
    ]
    text = render_tool_calls_as_bpy(calls)
    assert has_material_setup(text)
    assert not has_material_variety(text)      # the grey-on-everything catch


def test_lights_and_modifiers_from_atomic():
    calls = [
        _tc("create_light", kind="area", name="Key", location=[2, -2, 3], energy=500),
        _tc("create_light", kind="sun", name="Sun", location=[0, 0, 10], energy=3),
        _tc("add_modifier", object="Seat", kind="bevel"),
    ]
    text = render_tool_calls_as_bpy(calls)
    assert count_light_sources(text) == 2
    assert has_modifiers(text)


# ── end-to-end: a full atomic build passes its benchmark ───────────────
def test_atomic_sofa_passes_benchmark():
    # Mirrors the live furniture.sofa.modern: 8+ parts, materials,
    # variety. Should PASS the same benchmark that false-failed before.
    bench = Benchmark(
        name="furniture.sofa.modern", prompt="build a modern sofa",
        required_ops=(), required_named=True, require_material=True,
        min_distinct_objects=8, require_material_variety=True,
    )
    calls = []
    for i in range(8):
        calls.append(_tc("create_primitive", kind="cube", name=f"Part{i}",
                         location=[i * 0.5, 0, 0.3]))
    calls.append(_tc("apply_material", object="Part0", base_color=[0.2, 0.3, 0.6, 1]))
    calls.append(_tc("apply_material", object="Part1", base_color=[0.1, 0.1, 0.1, 1]))
    text = render_tool_calls_as_bpy(calls)
    verdict = score_against_benchmark(bench, text)
    assert verdict.ok, f"expected PASS, got notes: {verdict.notes}"


def test_material_numeric_params_render():
    # Industrial shelving benchmark asserts metallic=1.0 — must match an
    # atomic apply_material that set it (the false-fail this fixes).
    text = render_tool_calls_as_bpy([
        _tc("apply_material", object="Frame", base_color=[0.5, 0.5, 0.5, 1],
            metallic=1.0, roughness=0.3)])
    import re
    assert re.search(r"metallic\s*=\s*1\.0", text)
    assert re.search(r"roughness\s*=\s*0\.3", text)


def test_escape_hatch_script_still_scored():
    # A real bpy script (escape hatch) is concatenated by the runner; the
    # bridge leaves it untouched and the legacy regexes still apply.
    bench = Benchmark(name="primitive.cube", prompt="cube",
                      required_ops=(r"primitive_cube_add\(",), required_named=True)
    script = 'bpy.ops.mesh.primitive_cube_add()\nobj.name = "Hero"'
    verdict = score_against_benchmark(bench, script)
    assert verdict.ok
