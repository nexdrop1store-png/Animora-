"""
Stage 7 — Beat-the-MCP eval scoreboard verification.

All offline: builds mock BenchmarkResult dicts (the asdict() shape) and
exercises the critic-score regression gate, the quality targets, and the
first-step metric. No API credits, no Blender.

Run:
    pytest ai-backend/tests/test_stage7_eval.py -v
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

from ai_backend.eval.scoring import (  # noqa: E402
    aggregate_critic_by_category,
    compare_to_baseline,
    evaluate_targets,
)
from ai_backend.orchestrator.critic import first_step_ok  # noqa: E402


def _r(name, ok=True, critic_score=1.0, first_step=True):
    """One mock BenchmarkResult dict (the shape asdict() produces)."""
    return {
        "name": name, "ok": ok, "notes": [],
        "critic_score": critic_score, "first_step_ok": first_step,
    }


def _tc(tool_name, **inp):
    return {"name": tool_name, "input": inp}


# ── 7.1 — critic-score regression gating ───────────────────────────────
def test_score_drop_flagged():
    base = [_r("furniture.chair", ok=True, critic_score=0.90)]
    new = [_r("furniture.chair", ok=True, critic_score=0.60)]  # -0.30
    rep = compare_to_baseline(new, base)
    assert rep.has_regression
    assert ("furniture.chair", 0.90, 0.60) in rep.score_drops
    # The pass/fail gate alone wouldn't have caught this (ok stayed True).
    assert rep.newly_failing == []


def test_score_within_noise_not_flagged():
    base = [_r("furniture.chair", critic_score=0.90)]
    new = [_r("furniture.chair", critic_score=0.85)]  # -0.05, under 0.15
    rep = compare_to_baseline(new, base)
    assert rep.score_drops == []
    assert not rep.has_regression


def test_category_mean_score_drop_flagged():
    # furniture mean 0.90 → 0.70 (-0.20 ≥ 0.10 band), each within the
    # per-benchmark noise so only the CATEGORY mean trips.
    base = [
        _r("furniture.a", critic_score=0.92),
        _r("furniture.b", critic_score=0.88),
    ]
    new = [
        _r("furniture.a", critic_score=0.78),
        _r("furniture.b", critic_score=0.62),
    ]
    rep = compare_to_baseline(new, base)
    assert rep.has_regression
    cats = [c for c, _b, _n in rep.category_score_drops]
    assert "furniture" in cats


def test_passfail_gate_still_works():
    base = [_r("primitive.cube", ok=True, critic_score=1.0)]
    new = [_r("primitive.cube", ok=False, critic_score=1.0)]
    rep = compare_to_baseline(new, base)
    assert rep.has_regression
    assert "primitive.cube" in rep.newly_failing


def test_old_baseline_without_scores_loads():
    # Old baseline format: no critic_score key. Must not crash; score
    # gating no-ops until re-frozen.
    base = [{"name": "furniture.chair", "ok": True, "notes": []}]
    new = [_r("furniture.chair", ok=True, critic_score=0.40)]
    rep = compare_to_baseline(new, base)
    assert rep.score_drops == []          # can't compare → skip
    assert not rep.has_regression
    assert rep.newly_failing == []


# ── 7.2 — quality targets ──────────────────────────────────────────────
def test_quality_targets_met_and_below():
    # primitive target = (1.00 pass, 0.90 critic); furniture = (0.80, 0.85).
    results = [
        _r("primitive.cube", ok=True, critic_score=0.95),   # MET
        _r("furniture.chair", ok=True, critic_score=0.70),  # BELOW (critic)
    ]
    targets = evaluate_targets(results)
    assert targets["primitive"].met is True
    assert targets["furniture"].met is False
    assert "critic" in targets["furniture"].reason


def test_targets_below_on_pass_rate():
    # Two furniture benches, one fails → 50% pass < 80% target.
    results = [
        _r("furniture.a", ok=True, critic_score=0.95),
        _r("furniture.b", ok=False, critic_score=0.95),
    ]
    targets = evaluate_targets(results)
    assert targets["furniture"].met is False
    assert "pass" in targets["furniture"].reason


def test_aggregate_critic_by_category_skips_unscored():
    results = [
        _r("scene.beach", critic_score=0.8),
        _r("scene.forest", critic_score=-1.0),  # escape hatch / not scored
    ]
    means = aggregate_critic_by_category(results)
    # Only the scored one counts.
    assert means["scene"] == 0.8


# ── 7.3 — first-step metric ────────────────────────────────────────────
def test_first_step_ok_sane_vs_extreme():
    sane = [_tc("create_primitive", kind="cube", name="Base",
                location=[0, 0, 0], scale=[1, 1, 0.1])]
    assert first_step_ok(sane) is True

    exploded = [_tc("create_primitive", kind="cube", name="Base",
                    location=[0, 0, 0], scale=[900, 1, 1])]
    assert first_step_ok(exploded) is False


def test_first_step_bad_when_starts_with_material_or_parent():
    # Applying a material or parenting as the FIRST move = no foundation.
    assert first_step_ok([_tc("apply_material", object="X",
                              base_color=[1, 0, 0, 1])]) is False
    assert first_step_ok([_tc("set_parent", child="A", parent="B")]) is False


def test_first_step_read_only_then_create_is_ok():
    # Inspecting first (get_scene_info) is fine; the first real action
    # is the create that follows.
    calls = [
        _tc("get_scene_info"),
        _tc("create_primitive", kind="plane", name="Ground",
            location=[0, 0, 0], scale=[10, 10, 1]),
    ]
    assert first_step_ok(calls) is True


def test_first_step_escape_hatch_is_none():
    # Opaque script — can't judge the foundation from tool inputs.
    assert first_step_ok([_tc("execute_animora_code", script="import bpy")]) is None


def test_first_step_no_steps_is_none():
    assert first_step_ok([]) is None


# ── 7.4 — report includes critic + targets ─────────────────────────────
def test_report_includes_critic_and_targets():
    # Import here to avoid pulling the heavy runner at module load.
    from ai_backend.eval.runner import BenchmarkResult, _format_report
    results = [
        BenchmarkResult(name="furniture.chair", prompt="build a chair",
                        ok=True, critic_score=0.88, first_step_ok=True),
        BenchmarkResult(name="primitive.cube", prompt="cube",
                        ok=True, critic_score=0.95, first_step_ok=True),
    ]
    report = _format_report(results)
    assert "Mean critic score" in report
    assert "First-step accuracy" in report
    assert "mean critic" in report      # category table column
    assert "target" in report           # target column header
    assert "first step" in report       # per-benchmark column
