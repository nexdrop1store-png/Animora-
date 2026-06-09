"""
Stage 8 — Cost-aware scoreboard verification.

Cost is the SECONDARY axis: optimised only after quality, never at its
expense. The gate flags a cost increase ONLY when quality did not improve
(pure waste); paying more for better quality is allowed. All offline —
mock result dicts, no API credits, no Blender.

Run:
    pytest ai-backend/tests/test_stage8_cost.py -v
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
    aggregate_cost_by_category,
    compare_to_baseline,
    estimate_cost_usd,
    total_cost_usd,
)


def _r(name, *, ok=True, critic_score=0.9, cost_usd=None,
       model="", input_tokens=0, output_tokens=0):
    d = {"name": name, "ok": ok, "notes": [], "critic_score": critic_score,
         "model": model, "input_tokens": input_tokens,
         "output_tokens": output_tokens}
    if cost_usd is not None:
        d["cost_usd"] = cost_usd
    return d


# ── 8.1 — cost estimate ────────────────────────────────────────────────
def test_estimate_cost_per_model():
    # Opus: 1M in @ $15 + 1M out @ $75 = $90.
    assert estimate_cost_usd("claude-opus-4-7", 1_000_000, 1_000_000) == 90.0
    # Sonnet: $3 + $15 = $18.
    assert estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    # Haiku: $1 + $5 = $6.
    assert estimate_cost_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000) == 6.0


def test_unknown_model_falls_back_to_sonnet_not_zero():
    # An unrecognised model must not cost $0 (that would hide spend).
    assert estimate_cost_usd("some-future-model", 1_000_000, 0) == 3.0


def test_estimate_cost_handles_garbage_tokens():
    assert estimate_cost_usd("claude-opus-4-7", None, "x") == 0.0


# ── 8.2 — aggregation ──────────────────────────────────────────────────
def test_aggregate_cost_prefers_stored_then_estimates():
    results = [
        _r("vehicle.a", cost_usd=0.50),                         # stored
        _r("vehicle.b", model="claude-opus-4-7",                # estimated
           input_tokens=1_000_000, output_tokens=0),            # = $15
    ]
    means = aggregate_cost_by_category(results)
    assert means["vehicle"] == round((0.50 + 15.0) / 2, 6)
    assert total_cost_usd(results) == round(0.50 + 15.0, 6)


# ── 8.3 — cost-regression gate (quality-neutral guard) ─────────────────
def test_cost_increase_with_flat_quality_is_regression():
    base = [_r("vehicle.lambo", critic_score=0.80, cost_usd=0.20)]
    new = [_r("vehicle.lambo", critic_score=0.80, cost_usd=0.40)]  # 2x, flat
    rep = compare_to_baseline(new, base)
    assert rep.has_regression
    assert ("vehicle.lambo", 0.20, 0.40) in rep.cost_regressions


def test_cost_increase_WITH_quality_gain_is_allowed():
    # Paid more but quality rose 0.70 -> 0.90 → NOT waste, must not flag.
    base = [_r("vehicle.lambo", critic_score=0.70, cost_usd=0.20)]
    new = [_r("vehicle.lambo", critic_score=0.90, cost_usd=0.40)]
    rep = compare_to_baseline(new, base)
    assert rep.cost_regressions == []
    assert not rep.has_regression


def test_small_cost_wobble_not_flagged():
    # +$0.002 absolute is under the $0.01 floor → noise, not a regression.
    base = [_r("primitive.cube", critic_score=0.9, cost_usd=0.020)]
    new = [_r("primitive.cube", critic_score=0.9, cost_usd=0.022)]
    rep = compare_to_baseline(new, base)
    assert rep.cost_regressions == []


def test_cost_gate_silent_without_comparable_quality():
    # No critic score on one side → can't confirm waste → don't flag.
    base = [_r("scene.beach", critic_score=-1.0, cost_usd=0.20)]
    new = [_r("scene.beach", critic_score=-1.0, cost_usd=0.50)]
    rep = compare_to_baseline(new, base)
    assert rep.cost_regressions == []


def test_category_cost_increase_flagged_quality_neutral():
    base = [
        _r("vehicle.a", critic_score=0.80, cost_usd=0.20),
        _r("vehicle.b", critic_score=0.80, cost_usd=0.20),
    ]
    new = [
        _r("vehicle.a", critic_score=0.80, cost_usd=0.30),
        _r("vehicle.b", critic_score=0.80, cost_usd=0.32),
    ]
    rep = compare_to_baseline(new, base)
    assert rep.has_regression
    cats = [c for c, _b, _n in rep.category_cost_increases]
    assert "vehicle" in cats


def test_old_baseline_without_cost_loads():
    # Baseline predates cost_usd AND has no tokens → cost estimates to 0,
    # so base_c <= 0 path no-ops. Must not crash or flag.
    base = [{"name": "vehicle.a", "ok": True, "notes": []}]
    new = [_r("vehicle.a", critic_score=0.8, cost_usd=0.50)]
    rep = compare_to_baseline(new, base)
    assert rep.cost_regressions == []
    assert not rep.has_regression


# ── 8.4 — report surfaces cost + efficiency ────────────────────────────
def test_report_includes_cost_and_efficiency():
    from ai_backend.eval.runner import BenchmarkResult, _format_report
    results = [
        BenchmarkResult(name="vehicle.lambo", prompt="build a lambo",
                        ok=True, critic_score=0.80, first_step_ok=True,
                        cost_usd=0.25),
        BenchmarkResult(name="primitive.cube", prompt="cube",
                        ok=True, critic_score=0.95, first_step_ok=True,
                        cost_usd=0.01),
    ]
    report = _format_report(results)
    assert "Run cost" in report
    assert "Quality per dollar" in report
    assert "mean cost" in report          # category table column
    assert "$0.25" in report or "$0.2500" in report
