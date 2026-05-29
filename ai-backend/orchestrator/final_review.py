"""
Whole-scene final review — Quality Plan §5.4.

Runs ONCE per user turn, AFTER the agentic loop converges. Distinct from
the per-step artist's-eye check (`quality.py`) which fires on every
iteration with a structured checklist verdict. This module's job is the
composition-level synthesis: did the final scene match the pre-production
brief? Would a senior art director ship it?

Output is consumed two ways:
  • Telemetry event `quality.final_review` — always emitted
  • Optional `final_review_notice` WS message to the panel — only when the
    verdict is not "ship", so the user gets commentary only when there's
    something to say

The module follows orchestrator/quality.py's structure (HD capture wait,
Sonnet vision call with bounded timeout, defensive JSON parse, no-op
verdict on failure). Read that first if you're touching this.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..anthropic_client import AnthropicClient
from ..prompts.final_review import FINAL_REVIEW_VERSION, build_prompt
from ..scene_diff import diff_text
from ..vision_buffer import get_hd_capture_by_trigger, get_latest_hd_capture
from .events import bus
from .spec import Spec

log = logging.getLogger("animora.final_review")

# Sonnet 4.6 vision — same model used by artist's-eye. We do NOT use
# Opus here: the composition-level judgement is well within Sonnet's
# vision capability and Opus would add cost + latency for no gain.
_REVIEW_MODEL = "claude-sonnet-4-6"
_REVIEW_MAX_TOKENS = 600
_REVIEW_TIMEOUT_SEC = 25.0

# How long to wait for an HD capture to settle before bailing. After the
# agentic loop has finished, the final HD capture has usually already
# arrived (the last tool_result included one). Short poll deadline.
_HD_CAPTURE_WAIT_SEC = 2.0


@dataclass
class FinalReviewVerdict:
    """The output of the whole-scene art-director pass."""
    verdict: str = "ship"  # "ship" | "refine" | "rebuild"
    match_to_brief: str = ""
    what_works: str = ""
    what_to_fix: str = ""
    user_facing_note: str = ""
    confidence: float = 0.0
    elapsed_ms: int = 0
    fallback_reason: str = ""  # populated when the review couldn't run
    review_version: str = FINAL_REVIEW_VERSION

    @property
    def should_surface_to_user(self) -> bool:
        """True iff the verdict has something worth showing the user.
        'ship' verdicts stay silent unless there's still a non-empty note."""
        if self.verdict == "ship" and not self.user_facing_note.strip():
            return False
        return bool(self.user_facing_note.strip())


def _no_op_verdict(reason: str, elapsed_ms: int = 0) -> FinalReviewVerdict:
    """When the review itself can't run, return a silent 'ship' so we
    don't block the user with a fake warning."""
    return FinalReviewVerdict(
        verdict="ship",
        match_to_brief="",
        what_works="",
        what_to_fix="",
        user_facing_note="",
        confidence=0.0,
        elapsed_ms=elapsed_ms,
        fallback_reason=reason,
    )


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)```$", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(raw: str) -> FinalReviewVerdict | None:
    raw = raw.strip()
    m = _CODE_FENCE_RE.match(raw)
    if m:
        raw = m.group(1).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m2 = _JSON_OBJECT_RE.search(raw)
        if not m2:
            return None
        try:
            parsed = json.loads(m2.group(0))
        except json.JSONDecodeError:
            return None

    verdict = str(parsed.get("verdict", "")).lower()
    if verdict not in ("ship", "refine", "rebuild"):
        return None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return FinalReviewVerdict(
        verdict=verdict,
        match_to_brief=str(parsed.get("match_to_brief", ""))[:300],
        what_works=str(parsed.get("what_works", ""))[:300],
        what_to_fix=str(parsed.get("what_to_fix", ""))[:300],
        user_facing_note=str(parsed.get("user_facing_note", ""))[:500],
        confidence=max(0.0, min(1.0, confidence)),
    )


async def _wait_for_hd_capture(session_id: str) -> bytes | None:
    """Pull the latest HD capture available for this session — usually
    the post-script frame from the last loop iteration. Short poll
    deadline; the capture has almost always already landed by the time
    this function fires."""
    deadline = time.monotonic() + _HD_CAPTURE_WAIT_SEC
    while time.monotonic() < deadline:
        # Prefer the most recent post-script frame
        cap = await get_hd_capture_by_trigger(session_id, "post_script")
        if cap:
            return cap
        await asyncio.sleep(0.1)
    fallback = await get_latest_hd_capture(session_id)
    if fallback:
        return fallback[0]
    return None


async def run_final_review(
    *,
    session_id: str,
    user_intent: str,
    spec: Spec | None,
    anthropic_client: AnthropicClient,
    scene_graph_before: dict | None,
    scene_graph_after: dict | None,
    execution_outcome: str = "OK",
    send_final_review_notice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> FinalReviewVerdict:
    """Run the whole-scene art-director pass on the final HD capture.

    Spec is the brief built by `orchestrator/spec.build_spec` at turn
    start. May be `None` (when retry was disabled, when spec building
    failed, or when this is a conversational turn) — in that case the
    review still runs but is told there's no brief to compare against.

    Returns a verdict regardless of failure mode. Infrastructure errors
    produce a silent 'ship' verdict with `fallback_reason` populated.
    Telemetry events fire in all cases.
    """
    started = time.monotonic()

    png_bytes = await _wait_for_hd_capture(session_id)
    if png_bytes is None:
        verdict = _no_op_verdict("no HD capture available")
        verdict.elapsed_ms = int((time.monotonic() - started) * 1000)
        await bus.emit("quality.final_review.skipped", {
            "session_id": session_id,
            "reason": verdict.fallback_reason,
        })
        return verdict

    spec_render = spec.as_user_message() if spec is not None else ""
    scene_diff_summary = (
        diff_text(scene_graph_before, scene_graph_after or {})
        if scene_graph_after
        else "no scene-graph snapshot available"
    )

    prompt_text = build_prompt(
        user_intent=user_intent,
        spec_render=spec_render,
        scene_diff_summary=scene_diff_summary,
        execution_outcome=execution_outcome,
    )

    try:
        response = await asyncio.wait_for(
            anthropic_client.messages_create(
                model=_REVIEW_MODEL,
                max_tokens=_REVIEW_MAX_TOKENS,
                system="You are a strict JSON-only senior art-director. Output exactly one JSON object and nothing else.",
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(png_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": prompt_text},
                    ],
                }],
            ),
            timeout=_REVIEW_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("final_review.timeout session=%s elapsed_ms=%d", session_id, elapsed)
        v = _no_op_verdict("vision call timed out", elapsed)
        await bus.emit("quality.final_review.skipped", {
            "session_id": session_id, "reason": v.fallback_reason, "elapsed_ms": elapsed,
        })
        return v
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        log.error("final_review.api_error session=%s exc_type=%s",
                  session_id, type(exc).__name__)
        return _no_op_verdict(f"vision call raised {type(exc).__name__}", elapsed)

    text = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    verdict = _parse_verdict(text)
    elapsed = int((time.monotonic() - started) * 1000)

    if verdict is None:
        log.warning("final_review.unparseable session=%s preview=%r",
                    session_id, text[:200])
        return _no_op_verdict("unparseable verdict JSON", elapsed)

    verdict.elapsed_ms = elapsed

    await bus.emit("quality.final_review", {
        "session_id": session_id,
        "verdict": verdict.verdict,
        "confidence": verdict.confidence,
        "spec_present": spec is not None and spec.is_populated,
        "elapsed_ms": elapsed,
        "version": FINAL_REVIEW_VERSION,
    })
    log.info(
        "final_review.complete session=%s verdict=%s confidence=%.2f elapsed_ms=%d",
        session_id, verdict.verdict, verdict.confidence, elapsed,
    )

    # Surface to the panel ONLY when there's something worth saying.
    # 'ship' verdicts stay silent — the user just sees the result.
    # 'refine' / 'rebuild' surface as a final_review_notice WS message,
    # which the panel renders as Animora's closing remark.
    if verdict.should_surface_to_user and send_final_review_notice is not None:
        try:
            await send_final_review_notice({
                "type": "final_review_notice",
                "verdict": verdict.verdict,
                "summary": verdict.user_facing_note,
                "what_works": verdict.what_works,
                "what_to_fix": verdict.what_to_fix,
                "confidence": verdict.confidence,
            })
        except Exception as exc:
            log.debug("final_review_notice send failed: %s", exc)

    return verdict
