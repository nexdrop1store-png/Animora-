"""Pure-logic tests for the onboarding gate (animora_panel.onboarding).

The module imports bpy only inside functions; gate_needed() takes explicit
kwargs so the decision table tests run without Blender."""

from __future__ import annotations

import pytest

from animora_panel import onboarding

# ── Slide clamp ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("value", "expected"),
    [(-5, 0), (-1, 0), (0, 0), (1, 1), (2, 2), (3, 2), (99, 2)],
)
def test_clamp_slide(value, expected):
    assert onboarding.clamp_slide(value) == expected


def test_slides_are_three_pages():
    assert len(onboarding.SLIDES) == 3
    for spec in onboarding.SLIDES:
        assert spec["title"]
        assert spec["body"]
        assert spec["icon"]


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
