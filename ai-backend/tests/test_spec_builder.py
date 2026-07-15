"""
V2 Phase 5 (build-plan numbering) — spec builder unit tests.

The taste layer's SPECIFY step ships ON by default in V2. These tests
lock the module's contracts without firing a real Sonnet call:
  - defensive JSON parsing (fenced / embedded / garbage)
  - schema coercion (partial objects, wrong types, materials cap)
  - render_spec_for_assistant formatting
  - build_spec fallback behavior (timeout / API error / unparseable)
  - the V2 default: _ENABLE_SPEC_BUILDER is ON unless env-overridden

For the end-to-end spec flow (real Sonnet call), run an eval benchmark
with ANIMORA_ENABLE_SPEC unset and watch for the `spec.built` event.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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

from ai_backend.orchestrator import streaming
from ai_backend.orchestrator.spec import (
    Spec,
    _parse_spec_response,
    _validate_and_coerce,
    build_spec,
)
from ai_backend.prompts.spec_builder import EMPTY_SPEC, render_spec_for_assistant

_GOOD_SPEC_JSON = """{
  "subject": "a wooden chair",
  "framing": {"camera": "low three-quarter front", "lens_mm": 50, "angle": "product reveal"},
  "lighting": {"time_of_day": "studio neutral", "key": "soft key camera-left",
               "fill": "bounce fill", "rim": "subtle backlight", "mood": "neutral product"},
  "palette": {"dominant": "warm oak", "accent": "brass", "neutral": "soft grey"},
  "composition": {"foreground": "chair", "midground": "", "background": "seamless sweep",
                  "hero": "the chair"},
  "materials": [{"on": "seat", "type": "stained oak", "notes": "roughness 0.4"}],
  "density": {"scattered": "", "control": ""},
  "scale_notes": "chair is ~0.9m tall"
}"""


# ── Parsing ──────────────────────────────────────────────────────────


def test_parse_clean_json():
    parsed = _parse_spec_response(_GOOD_SPEC_JSON)
    assert parsed is not None
    assert parsed["subject"] == "a wooden chair"


def test_parse_fenced_json():
    fenced = f"```json\n{_GOOD_SPEC_JSON}\n```"
    parsed = _parse_spec_response(fenced)
    assert parsed is not None
    assert parsed["subject"] == "a wooden chair"


def test_parse_json_embedded_in_prose():
    noisy = f"Here is the SPEC you asked for:\n{_GOOD_SPEC_JSON}\nHope that helps!"
    parsed = _parse_spec_response(noisy)
    assert parsed is not None
    assert parsed["subject"] == "a wooden chair"


def test_parse_garbage_returns_none():
    assert _parse_spec_response("I cannot produce a spec right now.") is None
    assert _parse_spec_response("") is None


# ── Coercion ─────────────────────────────────────────────────────────


def test_coerce_partial_object_merges_into_empty_spec():
    out = _validate_and_coerce({"subject": "a cube"})
    assert out["subject"] == "a cube"
    # Untouched sections keep the EMPTY_SPEC shape
    assert out["framing"] == EMPTY_SPEC["framing"]
    assert out["materials"] == []


def test_coerce_drops_unknown_keys_and_wrong_types():
    out = _validate_and_coerce({
        "subject": 42,                      # wrong type -> stays empty
        "hallucinated_key": "x",            # unknown -> dropped
        "framing": {"camera": "eye-level", "lens_mm": "fifty", "angle": 3},
    })
    assert out["subject"] == ""
    assert "hallucinated_key" not in out
    assert out["framing"]["camera"] == "eye-level"
    assert out["framing"]["lens_mm"] == 0     # non-numeric lens not coerced
    assert out["framing"]["angle"] == ""      # non-string dropped


def test_coerce_materials_cleaned_and_capped():
    materials = [{"on": f"part{i}", "type": "metal", "notes": ""} for i in range(30)]
    materials.append("not-a-dict")
    materials.append({"notes": "orphan notes, no on/type"})
    out = _validate_and_coerce({"materials": materials})
    assert len(out["materials"]) == 20        # hard cap
    assert all(m["on"] or m["type"] for m in out["materials"])


def test_coerce_truncates_overlong_strings():
    out = _validate_and_coerce({"subject": "x" * 999, "scale_notes": "y" * 999})
    assert len(out["subject"]) == 200
    assert len(out["scale_notes"]) == 400


# ── Spec object + rendering ──────────────────────────────────────────


def test_spec_is_populated():
    assert Spec().is_populated is False
    assert Spec(data=_validate_and_coerce({"subject": "a cube"})).is_populated is True


def test_render_empty_spec_is_empty_string():
    assert render_spec_for_assistant(dict(EMPTY_SPEC)) == ""
    assert Spec().as_user_message() == ""


def test_render_populated_spec_contains_contract_sections():
    data = _validate_and_coerce(_parse_spec_response(_GOOD_SPEC_JSON))
    text = render_spec_for_assistant(data)
    assert "[ANIMORA PRE-PRODUCTION SPEC" in text
    assert "SUBJECT: a wooden chair" in text
    assert "LIGHTING (studio neutral" in text
    assert "  - key: soft key camera-left" in text
    assert "MATERIALS:" in text
    assert "SCALE: chair is ~0.9m tall" in text


def test_render_skips_empty_sections():
    data = _validate_and_coerce({"subject": "a cube"})
    text = render_spec_for_assistant(data)
    assert "SUBJECT: a cube" in text
    assert "LIGHTING" not in text
    assert "MATERIALS" not in text


# ── build_spec fallback behavior (no real API calls) ─────────────────


class _FakeClient:
    """Stands in for AnthropicClient.messages_create."""

    def __init__(self, *, text: str | None = None, exc: Exception | None = None,
                 delay_sec: float = 0.0):
        self._text = text
        self._exc = exc
        self._delay = delay_sec

    async def messages_create(self, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=200),
        )


async def test_build_spec_happy_path():
    spec = await build_spec(
        user_message="Build a wooden chair",
        persona_display_name="Hard Surface Artist",
        persona_discipline_brief="Hard-surface modeling.",
        anthropic_client=_FakeClient(text=_GOOD_SPEC_JSON),
    )
    assert spec.is_populated
    assert spec.fallback_reason == ""
    assert spec.data["subject"] == "a wooden chair"
    assert spec.input_tokens == 100 and spec.output_tokens == 200
    assert "SUBJECT: a wooden chair" in spec.as_user_message()


async def test_build_spec_timeout_falls_back_empty():
    spec = await build_spec(
        user_message="Build a chair",
        persona_display_name="Hard Surface Artist",
        persona_discipline_brief="Hard-surface modeling.",
        anthropic_client=_FakeClient(text=_GOOD_SPEC_JSON, delay_sec=5.0),
        timeout_sec=0.05,
    )
    assert spec.is_populated is False
    assert spec.fallback_reason == "timeout"
    assert spec.as_user_message() == ""


async def test_build_spec_api_error_falls_back_empty():
    spec = await build_spec(
        user_message="Build a chair",
        persona_display_name="Hard Surface Artist",
        persona_discipline_brief="Hard-surface modeling.",
        anthropic_client=_FakeClient(exc=RuntimeError("boom")),
    )
    assert spec.is_populated is False
    assert spec.fallback_reason.startswith("api_error:")


async def test_build_spec_unparseable_falls_back_empty():
    spec = await build_spec(
        user_message="Build a chair",
        persona_display_name="Hard Surface Artist",
        persona_discipline_brief="Hard-surface modeling.",
        anthropic_client=_FakeClient(text="sorry, no JSON today"),
    )
    assert spec.is_populated is False
    assert spec.fallback_reason == "unparseable_json"


# ── The V2 default: spec layer ships ON ──────────────────────────────


@pytest.mark.skipif(
    os.environ.get("ANIMORA_ENABLE_SPEC", "") != "",
    reason="ANIMORA_ENABLE_SPEC env override active in this environment",
)
def test_spec_builder_enabled_by_default():
    # V2 Phase 5 contract: the taste layer is ON unless explicitly
    # disabled via ANIMORA_ENABLE_SPEC=0 (the latency escape hatch).
    assert streaming._ENABLE_SPEC_BUILDER is True


def test_flag_parser_contract():
    assert streaming._flag("ANIMORA_TEST_FLAG_UNSET_XYZ", default=True) is True
    assert streaming._flag("ANIMORA_TEST_FLAG_UNSET_XYZ", default=False) is False
