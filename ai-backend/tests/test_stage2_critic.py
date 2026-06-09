"""
Stage 2 — Automatic Critic verification tests.

VERIFY criterion from the training brief: "critic reliably flags planted
defects (bad scale, floating objects, n-gons, blown lighting, flat
composition) and passes clean references."

This suite plants each scene-data-checkable defect into a mock scene graph
(the shape `vision.serialize_scene_graph()` returns) and asserts the critic
flags it; then asserts a clean reference scene passes. Two of the brief's
defects — n-gons and blown lighting — require pixel/face data the scene
graph doesn't carry; those stay in the vision layer (quality.py) and are
asserted here to be DECLARED in the rubric but not scored by the scene-data
critic (so the rubric stays a single source of truth).

Run:
    pytest ai-backend/tests/test_stage2_critic.py -v
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

from ai_backend.orchestrator.critic import (
    RUBRICS,
    CriticReport,
    run_scene_critic,
)


# ── Scene-graph fixtures ───────────────────────────────────────────────
def _obj(name, type="MESH", location=(0, 0, 0), scale=(1, 1, 1),
         materials=None, parent=None, visible=True):
    """Build one scene-graph object entry matching serialize_scene_graph."""
    return {
        "name": name,
        "type": type,
        "location": list(location),
        "rotation": [0, 0, 0],
        "scale": list(scale),
        "visible": visible,
        "selected": False,
        "modifiers": [],
        "parent": parent,
        "materials": materials if materials is not None else [],
    }


def _scene(objects: list[dict]) -> dict:
    return {
        "scene_name": "Scene",
        "frame_current": 1,
        "objects": objects,
        "active_object": objects[0]["name"] if objects else None,
        "mode": "OBJECT",
        "render": {"engine": "BLENDER_EEVEE"},
        "world": {"name": "World"},
    }


def _clean_couch_scene() -> dict:
    """A reference scene that should PASS: 6 named, materialed, spread,
    grounded mesh parts + a light, sane scale."""
    return _scene([
        _obj("Couch_Base", location=(0, 0, 0.2), materials=["GreyLinen"]),
        _obj("Couch_Back", location=(0, 0.35, 0.55), materials=["GreyLinen"]),
        _obj("Couch_Arm_L", location=(-1.0, 0, 0.45), materials=["GreyLinen"]),
        _obj("Couch_Arm_R", location=(1.0, 0, 0.45), materials=["GreyLinen"]),
        _obj("Couch_Cushion_1", location=(-0.6, 0, 0.45), materials=["GreyLinen"]),
        _obj("Couch_Cushion_2", location=(0.6, 0, 0.45), materials=["GreyLinen"]),
        _obj("KeyLight", type="LIGHT", location=(3, -3, 4)),
        _obj("HeroCamera", type="CAMERA", location=(5, -5, 2)),
    ])


# ── Defect: bad scale ───────────────────────────────────────────────────
def test_flags_extreme_scale():
    scene = _scene([
        _obj("Cushion", location=(0, 0, 0.5), scale=(900, 1, 1),
             materials=["Fabric"]),
    ])
    report = run_scene_critic(scene)
    f = next(x for x in report.findings if x.check_id == "scale_sanity")
    assert not f.passed, "extreme 900× scale should fail scale_sanity"
    assert "Cushion" in f.objects
    assert not report.passed  # scale is an ERROR-severity check


def test_passes_sane_scale():
    scene = _scene([_obj("Slab", scale=(2.0, 0.5, 0.1), materials=["Wood"])])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "scale_sanity")
    assert f.passed


# ── Defect: floating object ─────────────────────────────────────────────
def test_flags_floating_object():
    scene = _scene([
        _obj("Ground", location=(0, 0, 0), materials=["Sand"]),
        _obj("FloatingRock", location=(0, 0, 12.0), materials=["Stone"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "grounded_placement")
    assert not f.passed
    assert "FloatingRock" in f.objects
    assert "Ground" not in f.objects  # grounded object is fine


def test_parented_object_exempt_from_floating():
    # A high object that's parented is positioned deliberately — not flagged.
    scene = _scene([
        _obj("Tower_Base", location=(0, 0, 0), materials=["Stone"]),
        _obj("Tower_Top", location=(0, 0, 30.0), parent="Tower_Base",
             materials=["Stone"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "grounded_placement")
    assert f.passed, "parented high object should be exempt"


# ── Defect: missing material (the grey-couch failure) ──────────────────
def test_flags_missing_material():
    scene = _scene([
        _obj("Sand", location=(0, 0, 0), materials=["SandMat"]),
        _obj("Ocean", location=(0, 5, 0), materials=[]),       # grey!
        _obj("Shoreline", location=(0, 2, 0), materials=[None]),  # empty slot
    ])
    report = run_scene_critic(scene)
    f = next(x for x in report.findings if x.check_id == "materials_present")
    assert not f.passed
    assert "Ocean" in f.objects and "Shoreline" in f.objects
    assert "Sand" not in f.objects
    assert not report.passed  # materials is ERROR-severity


def test_passes_all_materialed():
    scene = _scene([
        _obj("A", location=(1, 0, 0), materials=["MatA"]),
        _obj("B", location=(-1, 0, 0), materials=["MatB"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "materials_present")
    assert f.passed


def test_materials_check_can_be_disabled():
    scene = _scene([_obj("Grey", materials=[])])
    report = run_scene_critic(scene, require_materials=False)
    assert not any(x.check_id == "materials_present" for x in report.findings)


# ── Defect: default names ───────────────────────────────────────────────
def test_flags_default_names():
    scene = _scene([
        _obj("Cube", location=(0, 0, 0), materials=["M"]),
        _obj("Cube.001", location=(2, 0, 0), materials=["M"]),
        _obj("CouchBase", location=(4, 0, 0), materials=["M"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "meaningful_names")
    assert not f.passed
    assert "Cube" in f.objects
    assert "Cube.001" in f.objects        # .001 suffix stripped before compare
    assert "CouchBase" not in f.objects


# ── Defect: too few elements for a scene ────────────────────────────────
def test_flags_scene_element_count():
    # "build a beach" → 3 planes. With expected_min_objects=6 → fails.
    scene = _scene([
        _obj("Sand", location=(0, 0, 0), materials=["S"]),
        _obj("Ocean", location=(0, 6, 0), materials=["O"]),
        _obj("Shoreline", location=(0, 3, 0), materials=["Sh"]),
    ])
    report = run_scene_critic(scene, expected_min_objects=6)
    f = next(x for x in report.findings if x.check_id == "scene_element_count")
    assert not f.passed
    assert not report.passed  # element count is ERROR-severity


def test_single_asset_min_one_passes():
    scene = _scene([_obj("RedSphere", location=(0, 0, 0), materials=["Red"])])
    f = next(x for x in run_scene_critic(scene, expected_min_objects=1).findings
             if x.check_id == "scene_element_count")
    assert f.passed


# ── Defect: flat composition (origin heap) ──────────────────────────────
def test_flags_flat_composition():
    scene = _scene([
        _obj("A", location=(0, 0, 0), materials=["M"]),
        _obj("B", location=(0, 0, 0), materials=["M"]),
        _obj("C", location=(0.01, 0, 0), materials=["M"]),
        _obj("D", location=(0, 0.01, 0), materials=["M"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "placement_variety")
    assert not f.passed, "all objects heaped at origin should flag flat composition"


def test_passes_spread_composition():
    scene = _scene([
        _obj("A", location=(-3, 0, 0), materials=["M"]),
        _obj("B", location=(0, 4, 0), materials=["M"]),
        _obj("C", location=(3, -2, 1), materials=["M"]),
    ])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "placement_variety")
    assert f.passed


# ── Defect: no light (finished scene) ───────────────────────────────────
def test_flags_no_light_when_required():
    scene = _scene([_obj("A", location=(1, 0, 0), materials=["M"])])
    f = next(x for x in run_scene_critic(scene, require_light=True).findings
             if x.check_id == "light_present")
    assert not f.passed


def test_light_optional_by_default():
    scene = _scene([_obj("A", location=(1, 0, 0), materials=["M"])])
    f = next(x for x in run_scene_critic(scene).findings if x.check_id == "light_present")
    assert f.passed  # not required by default → info-level pass


# ── Clean reference passes ──────────────────────────────────────────────
def test_clean_reference_passes():
    report = run_scene_critic(_clean_couch_scene(), expected_min_objects=5,
                              require_light=True)
    assert report.passed, f"clean couch should pass; got: {report.actionable_text()}"
    assert report.score == 1.0
    assert not report.errors
    assert not report.warnings


def test_score_drops_with_defects():
    clean = run_scene_critic(_clean_couch_scene(), expected_min_objects=5)
    # Same scene but strip all materials → score must drop below clean.
    broken_objs = []
    for o in _clean_couch_scene()["objects"]:
        o = dict(o)
        if o["type"] == "MESH":
            o["materials"] = []
        broken_objs.append(o)
    broken = run_scene_critic(_scene(broken_objs), expected_min_objects=5)
    assert broken.score < clean.score
    assert not broken.passed


# ── Rubric single-source-of-truth ───────────────────────────────────────
def test_rubric_declares_vision_only_checks():
    """n-gons (topology_clean) and blown lighting (lighting_exposure) are
    declared in the rubric but marked scene_data=False — they belong to the
    vision layer, not this deterministic critic."""
    by_id = {r.check_id: r for r in RUBRICS}
    assert by_id["topology_clean"].scene_data is False
    assert by_id["lighting_exposure"].scene_data is False
    assert by_id["material_read"].scene_data is False
    # And the scene-data ones are marked True:
    assert by_id["scale_sanity"].scene_data is True
    assert by_id["materials_present"].scene_data is True
    assert by_id["scene_element_count"].scene_data is True


def test_actionable_text_lists_failures():
    scene = _scene([_obj("Cube", location=(0, 0, 0), materials=[])])
    report = run_scene_critic(scene, expected_min_objects=6)
    text = report.actionable_text()
    # Should mention the failing checks for the CORRECT step to consume.
    assert "materials_present" in text
    assert "scene_element_count" in text
    assert "meaningful_names" in text
