"""
Stage 3A — Critic-driven correction loop verification.

The correction step lives inside stream_response's agentic loop (an
`elif` after the count-based rescues). Rather than spin up the whole
async loop + mock the Anthropic SDK, we test the two things that make
the step correct:

  1. The DECISION PREDICATE — given the gating conditions
     (get_live_scene_graph available, execution intent, not escape
     hatch, attempts left, iterations left, something was created),
     the step runs the critic and corrects ONLY when the critic
     returns errors.

  2. The CRITIC BEHAVIOR on live scenes — a defective live scene
     yields errors (→ correction fires); a clean one yields none
     (→ no correction). This reuses run_scene_critic, already covered
     in test_stage2_critic.py, but exercises it through the exact
     parameters the correction step passes (require_materials=True,
     require_light=is_hero, expected_min_objects per scene/asset).

Run:
    pytest ai-backend/tests/test_stage3_correction.py -v
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

from ai_backend.orchestrator import streaming as streaming_mod
from ai_backend.orchestrator.critic import (
    reconstruct_scene_graph,
    run_scene_critic,
    score_tool_calls,
    select_best_candidate,
)


def _tc(tool_name, **inp):
    return {"name": tool_name, "input": inp}


def _obj(name, type="MESH", location=(0, 0, 0), scale=(1, 1, 1),
         materials=None, parent=None):
    return {
        "name": name, "type": type, "location": list(location),
        "rotation": [0, 0, 0], "scale": list(scale), "visible": True,
        "selected": False, "modifiers": [], "parent": parent,
        "materials": materials if materials is not None else [],
    }


def _scene(objs):
    return {"objects": objs, "mode": "OBJECT"}


# ── The correction step is wired into stream_response ──────────────────
def test_streaming_imports_critic():
    """The correction step depends on run_scene_critic being imported
    into the streaming module."""
    assert hasattr(streaming_mod, "run_scene_critic")
    # And the bounded-attempts constant exists with a sane default.
    src = (Path(streaming_mod.__file__).read_text(encoding="utf-8")
           if streaming_mod.__file__ else "")
    assert "_MAX_CRITIC_CORRECTIONS = 2" in src
    assert "critic.correction.triggered" in src
    assert "get_live_scene_graph" in src


def test_stream_response_accepts_live_scene_callback():
    """stream_response must accept the get_live_scene_graph kwarg so
    main.py can feed the live scene to the critic."""
    import inspect
    sig = inspect.signature(streaming_mod.stream_response)
    assert "get_live_scene_graph" in sig.parameters


# ── Decision predicate (mirrors the elif gating in the loop) ───────────
def _would_correct(*, has_callback, is_execution_intent, used_escape_hatch,
                   attempts, max_attempts, iteration, max_iterations,
                   create_count, live_scene, is_hero=False, is_scene=False):
    """Pure re-implementation of the correction step's gate + critic
    decision, so we can unit-test the branching deterministically."""
    if not (
        has_callback
        and is_execution_intent
        and not used_escape_hatch
        and attempts < max_attempts
        and iteration < max_iterations - 1
        and create_count > 0
    ):
        return False
    if not (isinstance(live_scene, dict) and live_scene.get("objects")):
        return False
    report = run_scene_critic(
        live_scene,
        require_materials=True,
        require_light=is_hero,
        expected_min_objects=(6 if is_scene else 1),
    )
    return (not report.passed) and bool(report.errors)


def test_defective_scene_triggers_correction():
    # Grey couch: 4 meshes, no materials → materials_present error.
    live = _scene([
        _obj("Couch_Base", location=(0, 0, 0.2)),
        _obj("Couch_Back", location=(0, 0.3, 0.5)),
        _obj("Couch_Arm_L", location=(-1, 0, 0.4)),
        _obj("Couch_Arm_R", location=(1, 0, 0.4)),
    ])
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=4, live_scene=live, is_hero=True,
    ) is True


def test_clean_scene_no_correction():
    live = _scene([
        _obj("Couch_Base", location=(0, 0, 0.2), materials=["Fabric"]),
        _obj("Couch_Back", location=(0, 0.3, 0.5), materials=["Fabric"]),
        _obj("Couch_Arm_L", location=(-1, 0, 0.4), materials=["Fabric"]),
        _obj("Couch_Arm_R", location=(1, 0, 0.4), materials=["Fabric"]),
        _obj("KeyLight", type="LIGHT", location=(3, -3, 4)),
    ])
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=4, live_scene=live, is_hero=True,
    ) is False


def test_escape_hatch_skips_correction():
    live = _scene([_obj("Anything")])  # grey, but escape hatch used
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=True,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=1, live_scene=live,
    ) is False


def test_attempts_exhausted_skips_correction():
    live = _scene([_obj("Grey")])  # defective
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=2, max_attempts=2, iteration=1, max_iterations=5,
        create_count=1, live_scene=live,
    ) is False


def test_last_iteration_skips_correction():
    live = _scene([_obj("Grey")])
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=4, max_iterations=5,
        create_count=1, live_scene=live,
    ) is False


def test_no_callback_skips_correction():
    live = _scene([_obj("Grey")])
    assert _would_correct(
        has_callback=False, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=1, live_scene=live,
    ) is False


def test_empty_live_scene_skips_correction():
    # Before the addon pushes any graph, the callback returns {} — no-op.
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=4, live_scene={},
    ) is False


def test_floating_object_triggers_correction():
    # Catches what the count-based rescues can't: a floating object.
    live = _scene([
        _obj("Ground", location=(0, 0, 0), materials=["Sand"]),
        _obj("FloatingRock", location=(0, 0, 15.0), materials=["Stone"]),
        _obj("Tree", location=(2, 1, 0), materials=["Bark"]),
    ])
    # materials are all present, but grounded_placement is a WARNING not
    # an error — so floating alone does NOT trigger (errors-only gate).
    # This documents the intended behavior: warnings inform, errors gate.
    report = run_scene_critic(live, require_materials=True)
    assert any(f.check_id == "grounded_placement" and not f.passed
               for f in report.findings)
    # grounded_placement is a warning → not in errors → no correction.
    assert _would_correct(
        has_callback=True, is_execution_intent=True, used_escape_hatch=False,
        attempts=0, max_attempts=2, iteration=1, max_iterations=5,
        create_count=3, live_scene=live,
    ) is False  # warnings don't gate; only errors trigger correction


# ── 3B — Scene reconstruction from tool calls ──────────────────────────
def test_reconstruct_create_primitive():
    calls = [
        _tc("create_primitive", kind="cube", name="Seat",
            location=[0, 0, 0.45], scale=[0.5, 0.5, 0.05]),
    ]
    g = reconstruct_scene_graph(calls)
    assert len(g["objects"]) == 1
    o = g["objects"][0]
    assert o["name"] == "Seat" and o["type"] == "MESH"
    assert o["location"] == [0, 0, 0.45]
    assert o["scale"] == [0.5, 0.5, 0.05]
    assert o["materials"] == []  # no material applied yet


def test_reconstruct_apply_material():
    calls = [
        _tc("create_primitive", kind="cube", name="Seat", location=[0, 0, 0]),
        _tc("apply_material", object="Seat", name="Oak",
            base_color=[0.3, 0.18, 0.1, 1.0]),
    ]
    g = reconstruct_scene_graph(calls)
    assert g["objects"][0]["materials"] == ["Oak"]


def test_reconstruct_set_transform_and_parent():
    calls = [
        _tc("create_primitive", kind="cube", name="Base", location=[0, 0, 0]),
        _tc("create_primitive", kind="cylinder", name="Leg", location=[0, 0, 0]),
        _tc("set_transform", name="Leg", location=[1, 1, 0.2]),
        _tc("set_parent", child="Leg", parent="Base"),
    ]
    g = reconstruct_scene_graph(calls)
    leg = next(o for o in g["objects"] if o["name"] == "Leg")
    assert leg["location"] == [1, 1, 0.2]
    assert leg["parent"] == "Base"


def test_reconstruct_light_and_camera():
    calls = [
        _tc("create_light", kind="sun", name="Key", location=[3, 3, 5], energy=4),
        _tc("create_camera", name="HeroCam", location=[5, -5, 2]),
    ]
    g = reconstruct_scene_graph(calls)
    types = {o["name"]: o["type"] for o in g["objects"]}
    assert types["Key"] == "LIGHT"
    assert types["HeroCam"] == "CAMERA"


def test_reconstruct_delete_and_duplicate():
    calls = [
        _tc("create_primitive", kind="cube", name="A", location=[0, 0, 0]),
        _tc("apply_material", object="A", name="Red", base_color=[1, 0, 0, 1]),
        _tc("duplicate_object", source="A", new_name="B",
            location_offset=[2, 0, 0]),
        _tc("create_primitive", kind="cube", name="C", location=[5, 0, 0]),
        _tc("delete_object", name="C"),
    ]
    g = reconstruct_scene_graph(calls)
    names = {o["name"] for o in g["objects"]}
    assert names == {"A", "B"}  # C deleted
    b = next(o for o in g["objects"] if o["name"] == "B")
    assert b["location"] == [2, 0, 0]  # A at origin + offset
    assert b["materials"] == ["Red"]   # cloned material


def test_reconstruct_escape_hatch_marks_partial():
    calls = [_tc("execute_animora_code", script="import bpy\n...")]
    g = reconstruct_scene_graph(calls)
    assert g.get("_reconstruction_partial") is True


def test_score_tool_calls_grey_build_fails():
    # 4 primitives, no materials → materials_present error.
    calls = [
        _tc("create_primitive", kind="cube", name="Base", location=[0, 0, 0.2]),
        _tc("create_primitive", kind="cube", name="Back", location=[0, 0.3, 0.5]),
        _tc("create_primitive", kind="cube", name="ArmL", location=[-1, 0, 0.4]),
        _tc("create_primitive", kind="cube", name="ArmR", location=[1, 0, 0.4]),
    ]
    report = score_tool_calls(calls, require_materials=True)
    assert not report.passed
    assert "materials_present" in [f.check_id for f in report.errors]


def test_score_tool_calls_full_build_passes():
    calls = [
        _tc("create_primitive", kind="cube", name="Base", location=[0, 0, 0.2]),
        _tc("create_primitive", kind="cube", name="Back", location=[0, 0.3, 0.5]),
        _tc("create_primitive", kind="cube", name="ArmL", location=[-1, 0, 0.4]),
        _tc("create_primitive", kind="cube", name="ArmR", location=[1, 0, 0.4]),
        _tc("apply_material", object="Base", name="Fabric", base_color=[0.4, 0.4, 0.5, 1]),
        _tc("apply_material", object="Back", name="Fabric", base_color=[0.4, 0.4, 0.5, 1]),
        _tc("apply_material", object="ArmL", name="Fabric", base_color=[0.4, 0.4, 0.5, 1]),
        _tc("apply_material", object="ArmR", name="Fabric", base_color=[0.4, 0.4, 0.5, 1]),
    ]
    report = score_tool_calls(calls, require_materials=True)
    assert report.passed


# ── 3B — Best-of-N selection ───────────────────────────────────────────
def _grey_build():
    return [
        _tc("create_primitive", kind="cube", name="A", location=[0, 0, 0]),
        _tc("create_primitive", kind="cube", name="B", location=[2, 0, 0]),
    ]


def _materialed_build():
    return [
        _tc("create_primitive", kind="cube", name="A", location=[0, 0, 0]),
        _tc("create_primitive", kind="cube", name="B", location=[2, 0, 0]),
        _tc("apply_material", object="A", name="Red", base_color=[1, 0, 0, 1]),
        _tc("apply_material", object="B", name="Blue", base_color=[0, 0, 1, 1]),
    ]


def test_best_of_n_picks_highest_score():
    candidates = [_grey_build(), _materialed_build(), _grey_build()]
    result = select_best_candidate(candidates)
    assert result.n == 3
    assert result.best_index == 1  # the materialed build
    assert result.best_report.passed
    assert result.best_score >= result.mean_score >= result.worst_score


def test_best_of_n_all_equal_prefers_lower_index():
    candidates = [_materialed_build(), _materialed_build()]
    result = select_best_candidate(candidates)
    # Both identical → tie on score+passed+mesh_count → lower index wins.
    assert result.best_index == 0


def test_best_of_n_empty():
    result = select_best_candidate([])
    assert result.best_index == -1
    assert result.n == 0


def test_best_of_n_stats():
    candidates = [_grey_build(), _materialed_build()]
    result = select_best_candidate(candidates)
    # mean is between worst and best
    assert result.worst_score <= result.mean_score <= result.best_score
    assert len(result.all_reports) == 2


# ── 3C — Demonstration library ─────────────────────────────────────────
import os as _os  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402

from ai_backend.orchestrator.demonstrations import (  # noqa: E402
    DemonstrationLibrary,
    capture_enabled,
)


def _lib(tmp_path):
    return DemonstrationLibrary(root_dir=tmp_path / "demos")


def test_capture_disabled_by_default(tmp_path):
    # Without the env flag, capture is a no-op even for a perfect build.
    lib = _lib(tmp_path)
    captured = lib.capture(
        prompt="build a couch", intent="hard_surface_model",
        tool_calls=_materialed_build(), critic_score=1.0,
        critic_passed=True, mesh_count=2,
    )
    assert captured is False
    assert lib.all() == []


def test_capture_only_exemplary_builds(tmp_path):
    with _patch.dict(_os.environ, {"ANIMORA_CAPTURE_DEMOS": "1"}):
        lib = _lib(tmp_path)
        # Failing build → not captured.
        assert lib.capture(
            prompt="build a couch", intent="hard_surface_model",
            tool_calls=_grey_build(), critic_score=0.5,
            critic_passed=False, mesh_count=2,
        ) is False
        # Low-score (passing but mediocre) → not captured.
        assert lib.capture(
            prompt="build a couch", intent="hard_surface_model",
            tool_calls=_materialed_build(), critic_score=0.85,
            critic_passed=True, mesh_count=2,
        ) is False
        # Exemplary (passed + high score) → captured.
        assert lib.capture(
            prompt="build a wooden chair", intent="hard_surface_model",
            tool_calls=_materialed_build(), critic_score=1.0,
            critic_passed=True, mesh_count=2,
        ) is True
        assert len(lib.all()) == 1


def test_retrieve_relevant_by_token_overlap(tmp_path):
    with _patch.dict(_os.environ, {"ANIMORA_CAPTURE_DEMOS": "1"}):
        lib = _lib(tmp_path)
        lib.capture(prompt="build a wooden chair", intent="hard_surface_model",
                    tool_calls=_materialed_build(), critic_score=1.0,
                    critic_passed=True, mesh_count=2)
        lib.capture(prompt="build a metal shelf", intent="hard_surface_model",
                    tool_calls=_materialed_build(), critic_score=1.0,
                    critic_passed=True, mesh_count=2)
        lib.capture(prompt="build a warm beach", intent="dense_scene",
                    tool_calls=_materialed_build(), critic_score=1.0,
                    critic_passed=True, mesh_count=2)
        # New chair request should retrieve the chair demo first.
        hits = lib.retrieve_relevant("build a rustic wooden chair", k=2)
        assert hits, "expected at least one relevant demo"
        assert "chair" in hits[0].prompt


def test_retrieve_no_match_returns_empty(tmp_path):
    with _patch.dict(_os.environ, {"ANIMORA_CAPTURE_DEMOS": "1"}):
        lib = _lib(tmp_path)
        lib.capture(prompt="build a wooden chair", intent="hard_surface_model",
                    tool_calls=_materialed_build(), critic_score=1.0,
                    critic_passed=True, mesh_count=2)
        # Totally unrelated request → no overlapping subject tokens.
        hits = lib.retrieve_relevant("build a spaceship cockpit", k=3)
        assert hits == []


def test_library_stats(tmp_path):
    with _patch.dict(_os.environ, {"ANIMORA_CAPTURE_DEMOS": "1"}):
        lib = _lib(tmp_path)
        lib.capture(prompt="build a chair", intent="hard_surface_model",
                    tool_calls=_materialed_build(), critic_score=1.0,
                    critic_passed=True, mesh_count=2)
        lib.capture(prompt="build a beach", intent="dense_scene",
                    tool_calls=_materialed_build(), critic_score=0.95,
                    critic_passed=True, mesh_count=2)
        stats = lib.stats()
        assert stats["count"] == 2
        assert stats["by_intent"]["hard_surface_model"] == 1
        assert stats["by_intent"]["dense_scene"] == 1
