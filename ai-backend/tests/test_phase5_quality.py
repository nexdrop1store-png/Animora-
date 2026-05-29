"""
Phase 5 smoke test — artist's-eye verdict parsing + recipe lookup.

Doesn't fire a real Claude vision call (that needs an HD capture we
don't have in a unit test). Instead, exercises the parser with known
good/bad payloads and confirms the recipe lookup table is well-formed.

For the end-to-end vision test (real Sonnet call against a real capture),
run dev_server.py and exercise the full panel flow.
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

from ai_backend.orchestrator.quality import _parse_verdict, ArtistsEyeVerdict
from ai_backend.personas.mesh_repair_recipes import REPAIRS, recipes_for, all_recipes
from ai_backend.orchestrator.personas import all_personas
from ai_backend.prompts.artists_eye import ARTISTS_EYE_PROMPT, ARTISTS_EYE_VERSION


def test_parse_clean_pass() -> bool:
    raw = """{
        "checks": [
            {"name": "silhouette", "verdict": "pass", "reason": "clear subject"},
            {"name": "scatter_density", "verdict": "pass", "reason": "good"}
        ],
        "overall": "pass",
        "fix_suggestions": [],
        "confidence": 0.92,
        "summary": "Looks ready."
    }"""
    v = _parse_verdict(raw)
    assert v is not None, "verdict was None"
    assert v.overall == "pass"
    assert len(v.checks) == 2
    assert v.confidence == 0.92
    assert len(v.failed_checks) == 0
    return True


def test_parse_clean_fail() -> bool:
    raw = """{
        "checks": [
            {"name": "horizon_treatment", "verdict": "fail", "reason": "empty horizon"},
            {"name": "scatter_density", "verdict": "pass", "reason": "ok"}
        ],
        "overall": "fail",
        "fix_suggestions": ["add fog and distant tree silhouettes"],
        "confidence": 0.78,
        "summary": "Horizon needs work."
    }"""
    v = _parse_verdict(raw)
    assert v is not None
    assert v.overall == "fail"
    assert len(v.failed_checks) == 1
    assert v.failed_checks[0].name == "horizon_treatment"
    assert len(v.fix_suggestions) == 1
    return True


def test_parse_strips_markdown_fence() -> bool:
    raw = """```json
{
    "checks": [],
    "overall": "pass",
    "fix_suggestions": [],
    "confidence": 0.5,
    "summary": "fenced"
}
```"""
    v = _parse_verdict(raw)
    assert v is not None, f"failed to parse fenced JSON: {raw!r}"
    assert v.overall == "pass"
    return True


def test_parse_rejects_garbage() -> bool:
    assert _parse_verdict("not json at all") is None
    assert _parse_verdict('{"overall": "maybe"}') is None  # invalid overall value
    return True


def test_parse_handles_embedded_json() -> bool:
    raw = 'Here is the verdict: {"checks":[],"overall":"pass","fix_suggestions":[],"confidence":0.5,"summary":""} thanks'
    v = _parse_verdict(raw)
    assert v is not None
    assert v.overall == "pass"
    return True


def test_parse_caps_long_fields() -> bool:
    long_reason = "x" * 500
    raw = (
        '{"checks":[{"name":"a","verdict":"pass","reason":"'
        + long_reason + '"}],"overall":"pass","fix_suggestions":[],"confidence":0.5,"summary":""}'
    )
    v = _parse_verdict(raw)
    assert v is not None
    assert len(v.checks[0].reason) <= 240
    return True


def test_parse_coerces_invalid_confidence() -> bool:
    raw = '{"checks":[],"overall":"pass","fix_suggestions":[],"confidence":"high","summary":""}'
    v = _parse_verdict(raw)
    assert v is not None
    assert 0.0 <= v.confidence <= 1.0  # coerced to 0.0
    return True


def test_recipe_lookup() -> bool:
    """Every recipe in REPAIRS has well-formed check_name + description + bmesh_pattern."""
    for check_name, recipes in REPAIRS.items():
        for r in recipes:
            assert r.check_name == check_name, f"recipe key/name mismatch: {check_name} vs {r.check_name}"
            assert r.description, f"empty description for {check_name}"
            assert r.bmesh_pattern, f"empty bmesh_pattern for {check_name}"
            assert len(r.description) < 200, f"description too long for {check_name}"

    # recipes_for() returns empty tuple on miss (not None / KeyError)
    assert recipes_for("nonexistent_check") == ()

    # all_recipes() returns at least one
    flat = all_recipes()
    assert len(flat) >= 5, f"too few recipes registered: {len(flat)}"
    return True


def test_personas_quality_checks_have_recipes() -> bool:
    """For each persona's quality_checks list, count how many have at least
    one repair recipe registered. Not a hard failure — just visibility into
    gaps Phase 5.5 should fill."""
    print()
    print("Persona check coverage (Phase 5.5 will use REPAIRS to drive auto-retry):")
    for p in all_personas():
        if not p.quality_checks:
            continue
        with_recipe = sum(1 for c in p.quality_checks if recipes_for(c))
        total = len(p.quality_checks)
        print(f"  {p.id:25}  {with_recipe}/{total} checks have recipes")
    return True


def test_prompt_template_well_formed() -> bool:
    """Required placeholders are present in the prompt template."""
    required = [
        "{user_intent}", "{persona_display_name}",
        "{persona_quality_checks}", "{scene_diff_summary}",
        "{execution_outcome}",
    ]
    for ph in required:
        assert ph in ARTISTS_EYE_PROMPT, f"placeholder {ph} missing from ARTISTS_EYE_PROMPT"
    # Output schema field names present in the template (for reader sanity)
    for field in ["checks", "overall", "fix_suggestions", "confidence", "summary"]:
        assert field in ARTISTS_EYE_PROMPT, f"output field {field} not mentioned in prompt"
    assert ARTISTS_EYE_VERSION
    return True


def main() -> int:
    tests = [
        ("parse_clean_pass", test_parse_clean_pass),
        ("parse_clean_fail", test_parse_clean_fail),
        ("parse_strips_markdown_fence", test_parse_strips_markdown_fence),
        ("parse_rejects_garbage", test_parse_rejects_garbage),
        ("parse_handles_embedded_json", test_parse_handles_embedded_json),
        ("parse_caps_long_fields", test_parse_caps_long_fields),
        ("parse_coerces_invalid_confidence", test_parse_coerces_invalid_confidence),
        ("recipe_lookup", test_recipe_lookup),
        ("personas_quality_checks_have_recipes", test_personas_quality_checks_have_recipes),
        ("prompt_template_well_formed", test_prompt_template_well_formed),
    ]
    passes = 0
    fails: list[str] = []
    for name, fn in tests:
        try:
            ok = fn()
            mark = "OK " if ok else "XX "
            print(f"{mark} {name}")
            if ok:
                passes += 1
            else:
                fails.append(name)
        except AssertionError as e:
            print(f"XX  {name} -> AssertionError: {e}")
            fails.append(name)
        except Exception as e:
            print(f"XX  {name} -> {type(e).__name__}: {e}")
            fails.append(name)

    print()
    print(f"Results: {passes}/{len(tests)} pass")
    if fails:
        print(f"Failed: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
