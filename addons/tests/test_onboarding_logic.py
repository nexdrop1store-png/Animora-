"""Pure-logic tests for the onboarding gate (animora_panel.onboarding).

The module imports bpy only inside functions; gate_needed() takes explicit
kwargs so the decision table tests run without Blender."""

from __future__ import annotations

import pytest

from animora_panel import onboarding

# ── Slide clamp ──────────────────────────────────────────────────────────
# v1.1: trimmed from 3 slides (range [0,2]) to 1 (range [0,0]) — every
# value, in or out of range, clamps to the single sign-in slide.

@pytest.mark.parametrize(
    ("value", "expected"),
    [(-99, 0), (-5, 0), (-1, 0), (0, 0), (1, 0), (2, 0), (99, 0)],
)
def test_clamp_slide(value, expected):
    assert onboarding.clamp_slide(value) == expected


def test_clamp_slide_of_stale_slide_2_literal_is_zero():
    # Regression guard: two call sites (operators.py::OT_AnimoraSignOut,
    # auth/controller.py::_definitive_sign_out) used to hardcode
    # open_gate(slide=2) back when slide 2 was the sign-in slide. Both
    # were updated to open_gate() (default slide 0) in the v1.1 trim,
    # but if a stale `slide=2` literal ever creeps back in, it must
    # still resolve harmlessly to the only slide that exists.
    assert onboarding.clamp_slide(2) == 0


def test_slides_is_one_page():
    assert len(onboarding.SLIDES) == 1
    for spec in onboarding.SLIDES:
        assert spec["title"]
        assert spec["body"]
        assert spec["icon"]


def test_no_stale_open_gate_slide_2_literal_in_source():
    # Stronger regression guard than the clamp test above: assert the
    # actual call sites were edited, not just that clamping happens to
    # save a stale literal from crashing.
    import pathlib
    addons_dir = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("animora_panel/operators.py", "animora_panel/auth/controller.py"):
        src = (addons_dir / rel).read_text(encoding="utf-8")
        assert "open_gate(slide=2)" not in src, f"stale slide=2 literal found in {rel}"


# ── gate_needed decision table ───────────────────────────────────────────

def _needed(**overrides):
    defaults = {
        "background": False,
        "bundle_mode": False,
        "skip_env": "",
        "restorable": False,
    }
    defaults.update(overrides)
    return onboarding.gate_needed(**defaults)


def test_gate_opens_for_fresh_signed_out_launch():
    assert _needed() is True


def test_gate_skipped_in_background():
    assert _needed(background=True) is False


def test_gate_skipped_in_bundle_mode():
    assert _needed(bundle_mode=True) is False


def test_gate_skipped_with_restorable_session():
    # Returning signed-in users go straight into the app (silent restore).
    assert _needed(restorable=True) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", " 1 "])
def test_gate_skipped_via_env_escape_hatch(value):
    assert _needed(skip_env=value) is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_env_escape_hatch_off_values(value):
    assert _needed(skip_env=value) is True


def test_gate_inactive_by_default():
    assert onboarding.gate_active() is False
