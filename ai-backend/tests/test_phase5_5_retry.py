"""
Phase 5.5 smoke test — retry helpers.

Covers the pure-function surface of `orchestrator/retry.py`:
  • max_retries_from_env() — defaults, env override, malformed values
  • is_retriable() — gate logic on overall/fix_suggestions/fallback_reason
  • build_revision_user_message() — format, counter, final-attempt warning
  • summarize_verdict_for_event() — compact payload shape

The end-to-end retry (streaming loop → tool dispatch → addon → vision)
is exercised by test_phase15_e2e.py with retry enabled in a future
follow-up; for now, this file covers the deterministic helpers so
unit-level regressions are caught before they cost Sonnet vision calls.
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

from ai_backend.orchestrator.quality import ArtistsEyeVerdict, CheckResult
from ai_backend.orchestrator.retry import (
    DEFAULT_MAX_RETRIES,
    build_revision_user_message,
    is_retriable,
    max_retries_from_env,
    summarize_verdict_for_event,
)


# ── max_retries_from_env ────────────────────────────────────────────────

def test_default_when_env_unset() -> bool:
    os.environ.pop("ANIMORA_QUALITY_RETRIES", None)
    assert max_retries_from_env() == DEFAULT_MAX_RETRIES == 2
    return True


def test_env_override_to_zero_disables() -> bool:
    os.environ["ANIMORA_QUALITY_RETRIES"] = "0"
    try:
        assert max_retries_from_env() == 0
    finally:
        os.environ.pop("ANIMORA_QUALITY_RETRIES", None)
    return True


def test_env_override_to_higher_value() -> bool:
    os.environ["ANIMORA_QUALITY_RETRIES"] = "5"
    try:
        assert max_retries_from_env() == 5
    finally:
        os.environ.pop("ANIMORA_QUALITY_RETRIES", None)
    return True


def test_env_malformed_falls_back_to_default() -> bool:
    os.environ["ANIMORA_QUALITY_RETRIES"] = "not-an-int"
    try:
        assert max_retries_from_env() == DEFAULT_MAX_RETRIES
    finally:
        os.environ.pop("ANIMORA_QUALITY_RETRIES", None)
    return True


def test_env_negative_clamps_to_zero() -> bool:
    os.environ["ANIMORA_QUALITY_RETRIES"] = "-3"
    try:
        assert max_retries_from_env() == 0
    finally:
        os.environ.pop("ANIMORA_QUALITY_RETRIES", None)
    return True


# ── is_retriable ────────────────────────────────────────────────────────

def test_passing_verdict_not_retriable() -> bool:
    v = ArtistsEyeVerdict(overall="pass", fix_suggestions=["could be brighter"])
    assert is_retriable(v) is False
    return True


def test_failing_verdict_with_suggestions_retriable() -> bool:
    v = ArtistsEyeVerdict(
        overall="fail",
        checks=[CheckResult(name="shading", verdict="fail", reason="too flat")],
        fix_suggestions=["enable shade_smooth on the mesh"],
    )
    assert is_retriable(v) is True
    return True


def test_failing_verdict_with_failed_checks_only_still_retriable() -> bool:
    # No fix_suggestions but at least one failed check → retriable.
    # The revision message still surfaces the check's reason as actionable.
    v = ArtistsEyeVerdict(
        overall="fail",
        checks=[CheckResult(name="proportion", verdict="fail", reason="wheels too small")],
    )
    assert is_retriable(v) is True
    return True


def test_fallback_reason_blocks_retry() -> bool:
    # The check itself failed (vision timeout, unparseable JSON) → no
    # new signal to act on, retrying would just burn cost.
    v = ArtistsEyeVerdict(
        overall="fail",
        fallback_reason="vision call timed out",
        fix_suggestions=["something"],  # not enough to make retriable
    )
    assert is_retriable(v) is False
    return True


def test_failing_with_no_signal_not_retriable() -> bool:
    v = ArtistsEyeVerdict(overall="fail")
    assert is_retriable(v) is False
    return True


# ── build_revision_user_message ─────────────────────────────────────────

def test_revision_message_role_and_content() -> bool:
    v = ArtistsEyeVerdict(
        overall="fail",
        checks=[
            CheckResult(name="material", verdict="fail", reason="no PBR setup"),
            CheckResult(name="naming", verdict="fail", reason="default 'Cube' name"),
        ],
        fix_suggestions=[
            "Create a Principled BSDF material and append to obj.data.materials",
            "Rename the cube to something descriptive like 'BalsaWoodBlock'",
        ],
    )
    msg = build_revision_user_message(v, retry_attempt=0, max_retries=2)
    assert msg["role"] == "user"
    body = msg["content"]
    assert isinstance(body, str)
    assert "revision" in body.lower()
    assert "attempt 1/3" in body  # 0-indexed retry_attempt → 1-indexed display
    assert "material" in body
    assert "no PBR setup" in body
    assert "BalsaWoodBlock" in body
    return True


def test_revision_message_final_attempt_warns_user() -> bool:
    # When this is the LAST retry slot (retry_attempt + 1 >= max_retries),
    # the model must be told no further revisions will be requested.
    # Spec checks the wording explicitly so the master prompt's rule 19
    # stays in sync with what the model actually sees.
    v = ArtistsEyeVerdict(
        overall="fail",
        checks=[CheckResult(name="x", verdict="fail", reason="y")],
        fix_suggestions=["do z"],
    )
    msg_intermediate = build_revision_user_message(v, retry_attempt=0, max_retries=2)
    msg_final = build_revision_user_message(v, retry_attempt=1, max_retries=2)
    assert "no further revisions" in msg_final["content"]
    assert "no further revisions" not in msg_intermediate["content"]
    return True


def test_revision_message_handles_empty_suggestions_gracefully() -> bool:
    v = ArtistsEyeVerdict(overall="fail")  # no checks, no suggestions
    msg = build_revision_user_message(v, retry_attempt=0, max_retries=2)
    # Shouldn't crash; should still produce something parseable
    assert "no specific" in msg["content"].lower()
    return True


# ── summarize_verdict_for_event ─────────────────────────────────────────

def test_summary_payload_is_compact_and_complete() -> bool:
    v = ArtistsEyeVerdict(
        overall="fail",
        summary="The cube is the wrong size and has no material applied.",
        checks=[
            CheckResult(name="size", verdict="fail", reason="too small"),
            CheckResult(name="material", verdict="fail", reason="missing"),
            CheckResult(name="naming", verdict="pass", reason="good"),
        ],
        fix_suggestions=["scale up to 2x", "add PBR material"],
        confidence=0.834,
    )
    payload = summarize_verdict_for_event(v)
    assert payload["overall"] == "fail"
    assert payload["failed_count"] == 2  # only the failing ones
    assert payload["confidence"] == 0.83  # rounded
    assert len(payload["fix_suggestions"]) == 2
    assert "wrong size" in payload["summary"]
    return True


def test_summary_truncates_long_strings() -> bool:
    long_summary = "x" * 500
    long_suggestion = "y" * 500
    v = ArtistsEyeVerdict(
        overall="fail",
        summary=long_summary,
        fix_suggestions=[long_suggestion],
    )
    payload = summarize_verdict_for_event(v)
    assert len(payload["summary"]) <= 240
    assert len(payload["fix_suggestions"][0]) <= 200
    return True


# ── Test runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_default_when_env_unset,
        test_env_override_to_zero_disables,
        test_env_override_to_higher_value,
        test_env_malformed_falls_back_to_default,
        test_env_negative_clamps_to_zero,
        test_passing_verdict_not_retriable,
        test_failing_verdict_with_suggestions_retriable,
        test_failing_verdict_with_failed_checks_only_still_retriable,
        test_fallback_reason_blocks_retry,
        test_failing_with_no_signal_not_retriable,
        test_revision_message_role_and_content,
        test_revision_message_final_attempt_warns_user,
        test_revision_message_handles_empty_suggestions_gracefully,
        test_summary_payload_is_compact_and_complete,
        test_summary_truncates_long_strings,
    ]

    failures = 0
    for t in tests:
        try:
            ok = t()
            print(f"  {t.__name__}: {'PASS' if ok else 'FAIL'}")
            if not ok:
                failures += 1
        except AssertionError as e:
            print(f"  {t.__name__}: FAIL — {e}")
            failures += 1
        except Exception as e:
            print(f"  {t.__name__}: ERROR — {type(e).__name__}: {e}")
            failures += 1

    print()
    print(f"{len(tests) - failures}/{len(tests)} tests passed.")
    sys.exit(0 if failures == 0 else 1)
