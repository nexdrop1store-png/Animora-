"""
Runtime intent classifier — calls Haiku, parses JSON, returns a typed result.

Called from streaming.py BEFORE persona selection. Cheap (Haiku, <300
output tokens), fast (~500ms target), and the only place we tolerate
"bad LLM output" — if Haiku returns non-JSON or a missing field, we
fall back to a generalist routing rather than failing the whole turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from ..anthropic_client import AnthropicClient
from ..prompts.intent_classifier import INTENT_CLASSIFIER_PROMPT, INTENT_CLASSIFIER_VERSION

log = logging.getLogger("animora.intent")


# Whitelist of intent IDs. If Haiku returns something not on this list,
# we coerce to "unknown" rather than passing through garbage downstream.
_VALID_INTENTS = frozenset({
    "dense_scene", "terrain_landscape", "architecture",
    "hard_surface_model", "character_sculpt", "rig_setup",
    "character_animation", "cloth_sim", "fluid_water", "destruction_explosion",
    "lighting_setup", "material_authoring", "geometry_nodes_advanced",
    "render_setup", "compositing", "game_export", "2d_grease_pencil",
    "simple_edit", "question", "unknown",
})

_VALID_PERSONAS = frozenset({
    "environment_artist", "hard_surface_artist", "lighting_td",
    "character_artist", "generalist",
})


@dataclass
class IntentResult:
    intent: str
    confidence: float
    recommended_persona: str
    complexity_estimate: float
    rationale: str
    classifier_version: str = INTENT_CLASSIFIER_VERSION
    elapsed_ms: int = 0
    fallback_reason: str = ""  # populated if we couldn't classify normally


# Cheap default for failure paths: route to generalist, low confidence.
def _fallback(reason: str, elapsed_ms: int = 0) -> IntentResult:
    return IntentResult(
        intent="unknown",
        confidence=0.0,
        recommended_persona="generalist",
        complexity_estimate=0.3,
        rationale="Classifier failed; routed to generalist.",
        elapsed_ms=elapsed_ms,
        fallback_reason=reason,
    )


# Sprint 4E — Fast-path verbs. When the user message starts with one
# of these words, we skip the Haiku classifier (500-2000ms saved) and
# synthesise a confident execution-intent result. The persona system +
# master prompt handle correctness from there; Haiku was only catching
# edge cases. The intent → persona table mirrors the existing map a few
# lines below.
_FAST_PATH_VERBS = {
    "build":     ("hard_surface_model", "hard_surface_artist"),
    "make":      ("hard_surface_model", "hard_surface_artist"),
    "construct": ("hard_surface_model", "hard_surface_artist"),
    "model":     ("hard_surface_model", "hard_surface_artist"),
    "create":    ("hard_surface_model", "hard_surface_artist"),
    "add":       ("hard_surface_model", "hard_surface_artist"),
    "place":     ("hard_surface_model", "hard_surface_artist"),
    "drop":      ("hard_surface_model", "hard_surface_artist"),
    "spawn":     ("hard_surface_model", "hard_surface_artist"),
    # Lighting + scene-compose verbs lean on their specialists.
    "light":     ("lighting_setup", "lighting_td"),
    "illuminate": ("lighting_setup", "lighting_td"),
    "compose":   ("scene_compose", "environment_artist"),
    "scatter":   ("dense_scene", "environment_artist"),
}


def try_fast_path(user_message: str) -> IntentResult | None:
    """Synthesise an IntentResult for obvious build verbs without
    calling Haiku. Returns None if the message doesn't match — the
    caller falls back to `classify()`.

    Examples that hit the fast-path:
      "build a wooden chair"          → hard_surface_model
      "make a sunset HDRI"            → hard_surface_model
      "create three red spheres"      → hard_surface_model
      "light the scene for portrait"  → lighting_setup
    """
    if not user_message:
        return None
    head = user_message.lower().lstrip().split(maxsplit=1)
    if not head:
        return None
    verb = head[0].rstrip(":.,;")
    mapping = _FAST_PATH_VERBS.get(verb)
    if mapping is None:
        return None
    intent, persona = mapping
    return IntentResult(
        intent=intent,
        confidence=0.85,
        recommended_persona=persona,
        complexity_estimate=0.55,
        rationale=f"Fast-path on verb {verb!r}; skipped Haiku classifier.",
        elapsed_ms=0,
        fallback_reason="",  # this is success, not fallback
    )


def _strip_code_fence(text: str) -> str:
    """Some Haiku outputs include markdown fences despite instructions.
    Strip them defensively before json.loads."""
    text = text.strip()
    if text.startswith("```"):
        # Drop opening fence and optional language tag
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_classifier_response(raw: str) -> dict | None:
    raw = _strip_code_fence(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to find a JSON object embedded in prose (defensive)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


async def classify(
    *,
    user_message: str,
    anthropic_client: AnthropicClient,
    scene_summary: str = "",
    recent_context: str = "",
    timeout_sec: float = 8.0,
) -> IntentResult:
    """Run a single Haiku classification.

    Doesn't use the full AnthropicClient.stream path because we don't
    need streaming for a 200-token JSON output and don't want the
    overhead. Direct messages.create call wrapped in a per-call timeout.
    """
    import time
    started = time.monotonic()

    prompt = INTENT_CLASSIFIER_PROMPT.format(
        scene_summary=scene_summary or "(empty)",
        recent_context=recent_context or "(none)",
        user_message=user_message,
    )

    try:
        # Use the messages_create wrapper so the model ID is translated
        # to the Bedrock cross-region inference profile when on Bedrock.
        resp = await asyncio.wait_for(
            anthropic_client.messages_create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system="You are a strict JSON-only classifier. Output a single JSON object and nothing else.",
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("Intent classifier timed out after %dms", elapsed)
        return _fallback("timeout", elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("Intent classifier raised %s: %s", type(exc).__name__, exc)
        return _fallback(f"{type(exc).__name__}", elapsed)

    elapsed = int((time.monotonic() - started) * 1000)

    # Pull the text from the response
    text = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    parsed = _parse_classifier_response(text)
    if parsed is None:
        log.warning("Intent classifier returned unparseable output: %r", text[:200])
        return _fallback("unparseable_json", elapsed)

    # Validate + coerce
    intent = parsed.get("intent", "")
    if intent not in _VALID_INTENTS:
        log.warning("Intent classifier returned unknown intent %r — coercing to 'unknown'", intent)
        intent = "unknown"

    persona = parsed.get("recommended_persona", "")
    if persona not in _VALID_PERSONAS:
        log.warning("Intent classifier returned unknown persona %r — coercing to 'generalist'", persona)
        persona = "generalist"

    try:
        confidence = float(parsed.get("confidence", 0.0))
        complexity = float(parsed.get("complexity_estimate", 0.5))
    except (TypeError, ValueError):
        confidence, complexity = 0.0, 0.5

    confidence = max(0.0, min(1.0, confidence))
    complexity = max(0.0, min(1.0, complexity))

    rationale = str(parsed.get("rationale", ""))[:300]  # cap

    result = IntentResult(
        intent=intent,
        confidence=confidence,
        recommended_persona=persona,
        complexity_estimate=complexity,
        rationale=rationale,
        elapsed_ms=elapsed,
    )

    log.info("intent.classified intent=%s persona=%s confidence=%.2f complexity=%.2f elapsed=%dms",
             intent, persona, confidence, complexity, elapsed)

    return result
