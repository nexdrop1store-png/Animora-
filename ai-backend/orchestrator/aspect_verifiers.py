"""
Aspect-verifier runner — Phase 1 of the V2 AI-quality build plan.

Fires the 8 independent, narrowly-scoped Sonnet vision calls defined in
`prompts/aspect_verifiers.py` concurrently against one or more images, and
returns one `AspectResult` per aspect. Deliberately has NO dependency on
`orchestrator/quality.py` (which depends on this module instead) to avoid
a circular import — `quality.py` converts the list of `AspectResult` into
its own `ArtistsEyeVerdict`/`CheckResult` shape so existing consumers
(retry.py, telemetry, tests) don't need to change.

Cost/latency note: 8 concurrent Sonnet vision calls cost ~8x one holistic
call but take roughly the SAME wall-clock time (asyncio.gather, not
sequential) — Phase 0's baseline showed a single holistic score can't say
which aspect is actually wrong, which is what this buys back. Gated behind
ANIMORA_ASPECT_VERIFIERS (default off) in quality.py until validated
against the Phase 0 render harness and explicitly turned on, matching how
ANIMORA_ENABLE_SPEC and ANIMORA_ENFORCE_LOOP were rolled out.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass

from ..anthropic_client import AnthropicClient
from ..prompts.aspect_verifiers import ASPECTS, ASPECT_VERIFIER_PROMPT
from .image_media import sniff_image_media_type

log = logging.getLogger("animora.quality.aspects")

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)```$", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_VISION_TIMEOUT_SEC = 25.0
_VISION_MAX_TOKENS = 300  # one small JSON object per aspect, not a whole verdict


@dataclass
class AspectResult:
    aspect: str          # aspect_id, e.g. "silhouette"
    label: str            # display label, e.g. "Silhouette"
    verdict: str           # "pass" | "fail" | "n/a" | "error"
    reason: str = ""
    fix_suggestion: str = ""
    confidence: float = 0.0
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _parse_aspect_json(raw: str) -> dict | None:
    raw = raw.strip()
    m = _CODE_FENCE_RE.match(raw)
    if m:
        raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _image_blocks(images: list[bytes]) -> list[dict]:
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": sniff_image_media_type(data),
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
        for data in images
    ]


async def _run_one_aspect(
    aspect_id: str, label: str, rubric: str, image_blocks: list[dict],
    anthropic_client: AnthropicClient, *, user_intent: str, persona_display_name: str,
    persona_quality_checks: str, scene_diff_summary: str, execution_outcome: str,
) -> AspectResult:
    started = time.monotonic()
    prompt_text = ASPECT_VERIFIER_PROMPT.format(
        aspect_label=label, aspect_rubric=rubric,
        user_intent=user_intent[:500], persona_display_name=persona_display_name,
        persona_quality_checks=persona_quality_checks, scene_diff_summary=scene_diff_summary[:1200],
        execution_outcome=execution_outcome[:400],
    )
    try:
        response = await asyncio.wait_for(
            anthropic_client.messages_create(
                model="claude-sonnet-4-6",
                max_tokens=_VISION_MAX_TOKENS,
                system=f"You are a strict, single-purpose JSON-only grader. You judge ONLY {label}. "
                       "Output exactly one JSON object and nothing else.",
                messages=[{"role": "user", "content": [*image_blocks, {"type": "text", "text": prompt_text}]}],
            ),
            timeout=_VISION_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return AspectResult(aspect=aspect_id, label=label, verdict="error",
                             reason="vision call timed out", elapsed_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:
        return AspectResult(aspect=aspect_id, label=label, verdict="error",
                             reason=f"vision call raised {type(exc).__name__}",
                             elapsed_ms=int((time.monotonic() - started) * 1000))

    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    parsed = _parse_aspect_json(text)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    if parsed is None:
        log.warning("aspect_verifier.unparseable", extra={"aspect": aspect_id, "preview": text[:200]})
        return AspectResult(aspect=aspect_id, label=label, verdict="error",
                             reason="unparseable verdict JSON", elapsed_ms=elapsed_ms,
                             input_tokens=input_tokens, output_tokens=output_tokens)

    verdict = str(parsed.get("verdict", "")).lower()
    if verdict not in ("pass", "fail", "n/a"):
        return AspectResult(aspect=aspect_id, label=label, verdict="error",
                             reason=f"unexpected verdict value: {verdict!r}", elapsed_ms=elapsed_ms,
                             input_tokens=input_tokens, output_tokens=output_tokens)

    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return AspectResult(
        aspect=aspect_id, label=label, verdict=verdict,
        reason=str(parsed.get("reason", ""))[:240],
        fix_suggestion=str(parsed.get("fix_suggestion", ""))[:280],
        confidence=confidence, elapsed_ms=elapsed_ms,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


async def run_all_aspect_verifiers(
    images: list[bytes], anthropic_client: AnthropicClient, *, user_intent: str,
    persona_display_name: str, persona_quality_checks: str, scene_diff_summary: str,
    execution_outcome: str = "OK",
) -> list[AspectResult]:
    """Fire all 8 aspect verifiers concurrently against the given image(s)
    (pass multiple images for multi-view judgment — each call receives all
    of them). Returns one AspectResult per aspect, in ASPECTS order,
    regardless of individual failures (a timed-out/errored call yields
    verdict="error", not an exception)."""
    image_blocks = _image_blocks(images)
    tasks = [
        _run_one_aspect(
            aspect_id, label, rubric, image_blocks, anthropic_client,
            user_intent=user_intent, persona_display_name=persona_display_name,
            persona_quality_checks=persona_quality_checks, scene_diff_summary=scene_diff_summary,
            execution_outcome=execution_outcome,
        )
        for aspect_id, label, rubric in ASPECTS
    ]
    return await asyncio.gather(*tasks)
