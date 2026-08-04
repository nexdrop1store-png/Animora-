"""
Artist's-eye check runner — Phase 5 quality enforcement.

Triggered by main.py after the addon's tool_result arrives (Phase 2 sends
a post-script HD capture immediately after script execution). This
module pulls the HD capture from `vision_buffer`, calls Claude Sonnet
vision with the persona-aware prompt, parses the JSON verdict, and
emits a `quality.passed` / `quality.failed` event.

Phase 5 v1 scope (this file):
  • Run the check
  • Emit telemetry events
  • Surface failures as `quality_notice` WS messages so the user sees
    what the system flagged (no auto-fix yet)

Phase 5.5 (next round, NOT this file):
  • Wrap streaming.py's tool dispatch in a retry loop that consumes the
    `fix_suggestions` from a failed verdict, asks the LLM to re-execute
    with the fix, and only surfaces the passing result to the user.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..anthropic_client import AnthropicClient
from ..prompts.artists_eye import ARTISTS_EYE_PROMPT, ARTISTS_EYE_VERSION
from ..prompts.aspect_verifiers import ASPECT_VERIFIERS_VERSION
from ..scene_diff import diff_text
from .aspect_verifiers import AspectResult, run_all_aspect_verifiers
from .image_media import sniff_image_media_type
from ..vision_buffer import get_hd_capture_by_trigger, get_latest_hd_capture
from .events import bus
from .personas import Persona

log = logging.getLogger("animora.quality")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# V2 AI-quality build plan, Phase 1 (docs/ROADMAP-V2-notes.md §B2 item 2):
# 8 independent single-purpose vision calls instead of one holistic call
# self-reporting every check. Default OFF until validated against the
# Phase 0 render harness baseline and explicitly turned on — same rollout
# pattern as ANIMORA_ENABLE_SPEC / ANIMORA_ENFORCE_LOOP in streaming.py.
_USE_ASPECT_VERIFIERS = _flag("ANIMORA_ASPECT_VERIFIERS", default=False)

# Strip-only constants
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)```$", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Sonnet vision call — bounded latency target.
_VISION_TIMEOUT_SEC = 25.0
_VISION_MAX_TOKENS = 800

# How long to wait for an HD capture to arrive after a tool_result.
# Phase 2's addon trigger sends the capture immediately post-script, but
# JPEG encode + WS send adds a small delay. 2s is generous.
_HD_CAPTURE_WAIT_SEC = 2.5


@dataclass
class CheckResult:
    name: str
    verdict: str  # "pass" | "fail" | "n/a"
    reason: str = ""


@dataclass
class ArtistsEyeVerdict:
    overall: str  # "pass" | "fail"
    checks: list[CheckResult] = field(default_factory=list)
    fix_suggestions: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    elapsed_ms: int = 0
    fallback_reason: str = ""  # populated when the call fails / unparseable

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.verdict == "fail"]


def _no_op_verdict(reason: str) -> ArtistsEyeVerdict:
    """When the check itself can't run, treat as 'no opinion' — overall pass
    with a fallback_reason populated. Better than blocking the user on an
    infra hiccup."""
    return ArtistsEyeVerdict(
        overall="pass", summary="Quality check unavailable; proceeding.",
        fallback_reason=reason,
    )


def _parse_verdict(raw: str) -> ArtistsEyeVerdict | None:
    """Parse the JSON output from the vision call. Tolerant of markdown
    fences (Sonnet sometimes wraps despite instructions)."""
    raw = raw.strip()

    # Strip optional markdown fence
    m = _CODE_FENCE_RE.match(raw)
    if m:
        raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find an embedded object
        m = _JSON_OBJECT_RE.search(raw)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    overall = str(parsed.get("overall", "")).lower()
    if overall not in ("pass", "fail"):
        return None

    checks = []
    for c in parsed.get("checks", []) or []:
        if not isinstance(c, dict):
            continue
        verdict = str(c.get("verdict", "")).lower()
        if verdict not in ("pass", "fail", "n/a"):
            continue
        checks.append(CheckResult(
            name=str(c.get("name", "?"))[:80],
            verdict=verdict,
            reason=str(c.get("reason", ""))[:240],
        ))

    fix_suggestions = [
        str(s)[:280]
        for s in (parsed.get("fix_suggestions") or [])
        if isinstance(s, str)
    ]

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return ArtistsEyeVerdict(
        overall=overall,
        checks=checks,
        fix_suggestions=fix_suggestions[:5],
        summary=str(parsed.get("summary", ""))[:300],
        confidence=max(0.0, min(1.0, confidence)),
    )


async def _run_aspect_verifier_path(
    png_bytes: bytes, anthropic_client: AnthropicClient, *, user_intent: str, persona: Persona,
    checks_listing: str, scene_diff_summary: str, execution_outcome: str,
) -> ArtistsEyeVerdict:
    """Run the 8 independent aspect verifiers and fold the results into the
    same ArtistsEyeVerdict shape the single-call path produces, so
    retry.py/telemetry/tests don't need to know which path ran.

    An aspect that errored (timeout/unparseable/API error) is surfaced as
    a failed_checks-visible "error" verdict rather than silently dropped —
    the caller should know a grader didn't return an opinion, same spirit
    as the single-call path's _no_op_verdict for a total failure. If EVERY
    aspect errored, that IS a total failure — return a no-op pass so an
    infra outage doesn't block the user, matching the single-call path's
    behavior on a failed vision call."""
    try:
        results: list[AspectResult] = await run_all_aspect_verifiers(
            [png_bytes], anthropic_client, user_intent=user_intent,
            persona_display_name=persona.display_name, persona_quality_checks=checks_listing,
            scene_diff_summary=scene_diff_summary, execution_outcome=execution_outcome,
        )
    except Exception as exc:
        return _no_op_verdict(f"aspect verifier batch raised {type(exc).__name__}")

    if all(r.verdict == "error" for r in results):
        return _no_op_verdict("all 8 aspect verifiers errored")

    checks = [CheckResult(name=r.aspect, verdict=("n/a" if r.verdict == "error" else r.verdict), reason=r.reason or "grader error")
              for r in results]
    judged = [r for r in results if r.verdict != "error"]
    overall = "fail" if any(r.verdict == "fail" for r in judged) else "pass"
    fix_suggestions = [r.fix_suggestion for r in judged if r.verdict == "fail" and r.fix_suggestion]
    confidence = round(sum(r.confidence for r in judged) / len(judged), 3) if judged else 0.0
    failed_labels = [r.label for r in judged if r.verdict == "fail"]
    summary = (
        "All aspects pass." if not failed_labels
        else f"Failed: {', '.join(failed_labels)}."
    )

    return ArtistsEyeVerdict(
        overall=overall, checks=checks, fix_suggestions=fix_suggestions[:5],
        summary=summary[:300], confidence=confidence,
    )


async def _wait_for_hd_capture(session_id: str, trigger: str = "post_script") -> bytes | None:
    """Poll the vision buffer briefly for a specific trigger's HD capture.

    The addon's post-script trigger fires immediately when the bpy
    script returns, but JPEG encode + WS transit adds a small delay.
    Polling at 100ms intervals up to _HD_CAPTURE_WAIT_SEC is much
    cheaper than coordinating an explicit handshake."""
    deadline = time.monotonic() + _HD_CAPTURE_WAIT_SEC
    while time.monotonic() < deadline:
        cap = await get_hd_capture_by_trigger(session_id, trigger)
        if cap:
            return cap
        await asyncio.sleep(0.1)
    # Last-chance fallback: any recent capture
    fallback = await get_latest_hd_capture(session_id)
    if fallback:
        return fallback[0]
    return None


async def run_artists_eye_check(
    *,
    session_id: str,
    user_intent: str,
    persona: Persona,
    anthropic_client: AnthropicClient,
    scene_graph_before: dict | None,
    scene_graph_after: dict | None,
    execution_outcome: str = "OK",
    send_quality_notice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> ArtistsEyeVerdict:
    """Run the post-execution artist's-eye check for a single tool call.

    Returns a verdict regardless of failure mode — infrastructure errors
    produce a "no-op pass" verdict with `fallback_reason` populated.
    Telemetry events fire in all cases.
    """
    started = time.monotonic()

    # ── 1. Wait for the HD capture ────────────────────────────────────
    png_bytes = await _wait_for_hd_capture(session_id, trigger="post_script")
    if png_bytes is None:
        verdict = _no_op_verdict("no HD capture available")
        verdict.elapsed_ms = int((time.monotonic() - started) * 1000)
        await bus.emit("quality.skipped", {
            "session_id": session_id, "reason": verdict.fallback_reason,
        })
        return verdict

    # ── 2/3/4. Build prompt + call vision + parse ─────────────────────
    # Two paths, same output shape (ArtistsEyeVerdict) so steps 5+ below
    # are shared: the original single holistic call, or (Phase 1,
    # ANIMORA_ASPECT_VERIFIERS=1) 8 independent single-aspect calls.
    checks_listing = "\n".join(f"  - {c}" for c in persona.quality_checks) or "  (no domain-specific checks declared)"
    scene_diff_summary = diff_text(scene_graph_before, scene_graph_after or {}) if scene_graph_after else "no scene-graph snapshot available"

    if _USE_ASPECT_VERIFIERS:
        verdict = await _run_aspect_verifier_path(
            png_bytes, anthropic_client, user_intent=user_intent, persona=persona,
            checks_listing=checks_listing, scene_diff_summary=scene_diff_summary,
            execution_outcome=execution_outcome,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if verdict.fallback_reason:
            verdict.elapsed_ms = elapsed
            await bus.emit("quality.skipped", {
                "session_id": session_id, "reason": verdict.fallback_reason, "elapsed_ms": elapsed,
            })
            return verdict
        verdict.elapsed_ms = elapsed
    else:
        prompt_text = ARTISTS_EYE_PROMPT.format(
            user_intent=user_intent[:500],
            persona_display_name=persona.display_name,
            persona_quality_checks=checks_listing,
            scene_diff_summary=scene_diff_summary[:1200],
            execution_outcome=execution_outcome[:400],
        )

        # Routed through messages_create so the model ID is translated to
        # the Bedrock cross-region inference profile when ANIMORA_LLM_PROVIDER=bedrock.
        try:
            response = await asyncio.wait_for(
                anthropic_client.messages_create(
                    model="claude-sonnet-4-6",
                    max_tokens=_VISION_MAX_TOKENS,
                    system="You are a strict JSON-only senior art-director. Output exactly one JSON object and nothing else.",
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": sniff_image_media_type(png_bytes),
                                    "data": base64.b64encode(png_bytes).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": prompt_text},
                        ],
                    }],
                ),
                timeout=_VISION_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            log.warning("quality.timeout", extra={"session_id": session_id, "elapsed_ms": elapsed})
            verdict = _no_op_verdict("vision call timed out")
            verdict.elapsed_ms = elapsed
            await bus.emit("quality.skipped", {
                "session_id": session_id, "reason": verdict.fallback_reason, "elapsed_ms": elapsed,
            })
            return verdict
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            log.error("quality.api_error", extra={
                "session_id": session_id, "error_type": type(exc).__name__, "elapsed_ms": elapsed,
            })
            verdict = _no_op_verdict(f"vision call raised {type(exc).__name__}")
            verdict.elapsed_ms = elapsed
            return verdict

        text = ""
        for block in response.content:
            if getattr(block, "type", "") == "text":
                text += getattr(block, "text", "")

        verdict = _parse_verdict(text)
        elapsed = int((time.monotonic() - started) * 1000)

        if verdict is None:
            log.warning("quality.unparseable", extra={
                "session_id": session_id, "preview": text[:200], "elapsed_ms": elapsed,
            })
            v = _no_op_verdict("unparseable verdict JSON")
            v.elapsed_ms = elapsed
            return v

        verdict.elapsed_ms = elapsed

    # ── 5. Emit telemetry + optional WS notice ────────────────────────
    event_name = "quality.passed" if verdict.overall == "pass" else "quality.failed"
    await bus.emit(event_name, {
        "session_id": session_id,
        "persona": persona.id,
        "overall": verdict.overall,
        "elapsed_ms": elapsed,
        "checks_pass": sum(1 for c in verdict.checks if c.verdict == "pass"),
        "checks_fail": sum(1 for c in verdict.checks if c.verdict == "fail"),
        "checks_na":   sum(1 for c in verdict.checks if c.verdict == "n/a"),
        "confidence": verdict.confidence,
        "version": ASPECT_VERIFIERS_VERSION if _USE_ASPECT_VERIFIERS else ARTISTS_EYE_VERSION,
    })

    log.info(
        "quality.verdict",
        extra={
            "session_id": session_id, "persona": persona.id,
            "overall": verdict.overall, "confidence": verdict.confidence,
            "fails": len(verdict.failed_checks), "elapsed_ms": elapsed,
        },
    )

    # Surface failures to the user as a soft notice (Phase 5 v1 — no
    # auto-retry; just let them see what the system flagged).
    if verdict.overall == "fail" and send_quality_notice is not None:
        await send_quality_notice({
            "type": "quality_notice",
            "severity": "warning",
            "summary": verdict.summary or "Quality check flagged this output.",
            "failed_checks": [
                {"name": c.name, "reason": c.reason} for c in verdict.failed_checks
            ][:3],
            "fix_suggestions": verdict.fix_suggestions[:3],
            "confidence": verdict.confidence,
        })

    return verdict
