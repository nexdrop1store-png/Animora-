"""
Quality-recovery regression guards for the completeness gates.

Three confirmed defects these lock down:

1. `is_hero_request` required the message to START WITH a verb from a 6-item
   list AND contain a noun from a ~60-item list — and "lamp" was NOT in that
   list. So "build a floor lamp" got no completeness enforcement at all,
   which is exactly why the floor_lamp benchmark shipped with zero lights.
   Natural phrasings ("Can you make a chair?") also fell through.

2. The finished-by-default gate required `lights == 0 AND cameras == 0`, so a
   build with a camera but no light passed silently.

3. Every gate carried `and not used_escape_hatch`, so any script-driven build
   — i.e. every complex/hero build — bypassed the material, lighting,
   scene-floor and critic gates entirely.

The gate predicates are currently inline in stream_response's body, so these
tests assert the *policy* against the module's own constants rather than
re-implementing it. They fail loudly if the keyword lists or the escape-hatch
skips regress.
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
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

_SRC = (_PKG_DIR / "orchestrator" / "streaming.py").read_text(encoding="utf-8")


def test_gates_no_longer_disable_on_escape_hatch():
    """The material, lighting and critic gates must NOT skip script builds.

    Only the first-step gate may legitimately still key off the escape hatch
    (it diagnoses the FIRST tool call, which a script legitimately replaces).
    """
    skips = _SRC.count("and not used_escape_hatch")
    assert skips <= 1, (
        f"{skips} gates still skip when execute_animora_code was used. "
        "Script-driven builds are the complex ones — they need gating most. "
        "Gates should read the live scene instead."
    )


def test_lighting_gate_uses_or_not_and():
    """Camera-but-no-light (and vice versa) must trip the gate."""
    assert 'or\n                _effective("camera"' in _SRC or (
        '_effective("light", atomic_light_count) == 0' in _SRC
        and "or" in _SRC.split('_effective("light", atomic_light_count) == 0')[1][:120]
    ), "finished-by-default gate must fire when EITHER lights or cameras are missing"


def test_hero_detection_is_not_startswith_only():
    """Natural phrasing must not fall through the hero gate."""
    assert "_user_lower.lstrip().startswith(v)" not in _SRC, (
        "hero detection still requires the message to START WITH a build verb; "
        '"Can you make a chair?" and "a cozy living room please" fall through.'
    )
    assert "_BUILD_INTENTS" in _SRC, (
        "hero detection should consult the intent classifier, not only keywords"
    )


def test_gates_consult_the_live_scene():
    """Outcome-based gating: the helpers that read the real scene must exist
    and be used by the gates (this is what makes script builds enforceable)."""
    for helper in ("_live_counts", "_scene_has_geometry", "_effective"):
        assert f"def {helper}" in _SRC, f"missing live-scene helper {helper}"
    assert _SRC.count("_scene_has_geometry(") >= 3, (
        "gates should use _scene_has_geometry() so script builds count"
    )


def test_loop_exhaustion_is_reported():
    """A build cut short by the iteration budget must say so, not pretend it
    finished (previously: no event, no message at all)."""
    assert '"reason": "max_iterations"' in _SRC, (
        "iteration exhaustion still emits no agent.loop_exit event"
    )
    assert "build limit for this turn" in _SRC, (
        "iteration exhaustion still doesn't tell the user the build is incomplete"
    )


def test_budgets_are_env_overridable_for_rollback():
    """Every raised limit must stay tunable via env so the live backend can be
    rolled back with a Space secret rather than a redeploy."""
    for var in (
        "ANIMORA_MAX_ITERATIONS",
        "ANIMORA_EXEC_MAX_TOKENS",
        "ANIMORA_MAX_WALL_CLOCK_SEC",
        "ANIMORA_MAX_ACCUM_INPUT_TOKENS",
    ):
        assert var in _SRC, f"{var} must remain env-overridable for rollback"
