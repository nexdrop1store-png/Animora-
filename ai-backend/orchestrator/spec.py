"""
Runtime spec builder — Quality Plan §5.1 (SPECIFY).

Called from streaming.py AFTER the intent classifier and persona selection,
BEFORE the agentic execution loop kicks off. Sonnet 4.6 call, ~3-5s,
~$0.02 per turn. Returns a structured creative brief that the loop
inserts into `accumulated_messages` as a system-role contract.

Failure modes are absorbed (timeout / unparseable JSON / API error)
into a `fallback_reason` field on the returned Spec object. The loop
then proceeds without an injected SPEC — quality degrades to today's
behavior, no worse. This is critical: the SPEC step must never block
or break execution, only enhance it.

Design parallels orchestrator/intent.py — same patterns (defensive JSON
parsing, code-fence stripping, fallback object). Read that first if
you're touching this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..anthropic_client import AnthropicClient
from ..prompts.spec_builder import (
    EMPTY_SPEC,
    SPEC_BUILDER_VERSION,
    build_prompt,
    render_spec_for_assistant,
)

log = logging.getLogger("animora.spec")


# Sonnet 4.6 — Haiku is too thin for multi-field structured planning;
# Opus is overkill (we're not executing). Sonnet is the right floor.
_SPEC_MODEL = "claude-sonnet-4-6"
_SPEC_MAX_TOKENS = 1200
# 30s ceiling: Sonnet 4.6 on Bedrock can take 10-20s for the full
# structured-JSON output; direct Anthropic API is typically faster.
# Spec runs ONCE per user message (not per iteration), so a generous
# ceiling here doesn't compound. If it times out, we fall back to
# no-spec — degraded quality, not a broken turn.
_SPEC_TIMEOUT_SEC = 30.0


# Short one-line discipline summaries per persona ID. The spec planner
# uses these to bias its brief toward the persona's strengths (e.g.,
# environment artist gets a composition-focused brief; hard surface
# artist gets a dimensions/topology-focused brief). Falls back to the
# display name if a persona ID isn't in the map — Sonnet still produces
# a reasonable brief without specialist context.
_DISCIPLINE_BRIEFS: dict[str, str] = {
    "environment_artist": (
        "Outdoor / environment work: terrain, scatter, atmospheric depth, "
        "foreground-midground-background composition, motivated lighting."
    ),
    "hard_surface_artist": (
        "Hard-surface modeling: vehicles, weapons, machinery, furniture, "
        "appliances. Clean topology, precise dimensions, controlled bevels, "
        "panel-seam detail, realistic edge wear."
    ),
    "lighting_td": (
        "Look-development and lighting: three-point or motivated setups, "
        "color temperature, contrast, mood, no flat or blown-out frames."
    ),
    "character_artist": (
        "Organic / character work: anatomy and proportion first, clean edge "
        "flow, sculpting workflow, deformation-ready topology."
    ),
    "generalist": (
        "Generalist: cover any 3D request with sensible professional "
        "defaults — used when no specialist matches."
    ),
}


def _discipline_brief(persona_id: str, persona_display_name: str) -> str:
    """Resolve a one-line persona discipline brief for the spec planner."""
    return _DISCIPLINE_BRIEFS.get(persona_id, persona_display_name)


# v1.2 — the SPEC call adds ~18-25s (its own timeout is 30s) to EVERY
# execution turn, which is a real, named contributor to "Animora is
# taking long" complaints. Skipping it is only safe for genuinely
# trivial single-primitive asks — everything else keeps the taste-
# layer quality lift. Deliberately conservative (word-count ceiling
# AND a bare-primitive-noun match) rather than a broad word-count-only
# cutoff, so a short but substantive ask ("build a cyberpunk katana")
# still gets planned.
# 0.6, not something lower: intent.py::try_fast_path always returns a
# FIXED complexity_estimate of 0.55 for every fast-pathed build verb
# ("make a cube" and "make a hyperdetailed dragon" both get 0.55) — a
# ceiling below 0.55 would reject every fast-pathed message outright,
# defeating the point (caught by this module's own tests using the
# real fast-path value rather than an arbitrary low one).
_TRIVIAL_PROMPT_MAX_WORDS = 8
_TRIVIAL_PROMPT_COMPLEXITY_CEILING = 0.6
_TRIVIAL_PRIMITIVE_NOUNS = frozenset({
    "cube", "cuboid", "box", "sphere", "ball", "cylinder", "cone",
    "plane", "circle", "torus", "primitive",
})


def should_skip_spec_for_trivial_prompt(user_message: str, complexity_estimate: float) -> bool:
    """True when a prompt is simple enough that the SPEC builder's
    ~20s planning call isn't worth its latency cost.

    Both conditions must hold:
      - short (<= _TRIVIAL_PROMPT_MAX_WORDS words) — a genuinely
        descriptive ask ("build a cozy living room with warm
        lighting") is long enough to fail this on its own.
      - mentions a bare primitive noun ("make A CUBE") — catches the
        "make a cube"-class prompt the fast-path intent classifier
        also fires on, WITHOUT relying on complexity_estimate alone,
        because the fast-path (intent.py::try_fast_path) always
        returns a fixed 0.55 regardless of actual prompt complexity —
        "make a cube" and "make a hyperdetailed dragon" hit the same
        fast-path verb and get the same estimate, so complexity_estimate
        by itself can't distinguish them for fast-pathed intents.

    complexity_estimate still acts as a ceiling: a real (non-fast-path)
    Haiku classification above _TRIVIAL_PROMPT_COMPLEXITY_CEILING skips
    the noun heuristic entirely and keeps SPEC, regardless of length.
    """
    if complexity_estimate > _TRIVIAL_PROMPT_COMPLEXITY_CEILING:
        return False
    words = user_message.strip().split()
    if not words or len(words) > _TRIVIAL_PROMPT_MAX_WORDS:
        return False
    lowered = user_message.lower()
    return any(noun in lowered for noun in _TRIVIAL_PRIMITIVE_NOUNS)


@dataclass
class Spec:
    """A built creative brief, or an empty marker if the step was skipped/failed.

    Callers check `spec.is_populated` to decide whether to inject the
    SPEC block into the agent loop's accumulated_messages. The raw
    `data` dict is exposed for final_review (which compares the final
    scene against the spec) and for telemetry / session-recorder
    capture.
    """
    data: dict = field(default_factory=lambda: dict(EMPTY_SPEC))
    spec_version: str = SPEC_BUILDER_VERSION
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_reason: str = ""

    @property
    def is_populated(self) -> bool:
        """True when the SPEC has a subject — the minimum signal that
        the planner produced something the model should consult."""
        return bool((self.data.get("subject") or "").strip())

    def as_user_message(self) -> str:
        """Render to the format the agent loop injects into messages.
        Empty string if no SPEC was built (caller should skip the inject)."""
        return render_spec_for_assistant(self.data)


def _empty_spec(reason: str, elapsed_ms: int = 0) -> Spec:
    return Spec(
        data=dict(EMPTY_SPEC),
        elapsed_ms=elapsed_ms,
        fallback_reason=reason,
    )


def _strip_code_fence(text: str) -> str:
    """Tolerate Sonnet wrapping JSON in markdown despite the prompt
    explicitly forbidding it. Same defensive parse as intent.py."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_spec_response(raw: str) -> dict | None:
    raw = _strip_code_fence(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match is None:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _validate_and_coerce(parsed: dict) -> dict:
    """Merge `parsed` into EMPTY_SPEC, dropping unknown top-level keys
    and coercing each field to its expected shape.

    We MERGE rather than REPLACE because the model might return a
    partial object (e.g. omit `density` for a single-object scene).
    Empty defaults are fine — `render_spec_for_assistant` skips empty
    sections. Wrong types get coerced to empty defaults rather than
    raising.
    """
    out = dict(EMPTY_SPEC)

    # Top-level scalar
    if isinstance(parsed.get("subject"), str):
        out["subject"] = parsed["subject"][:200]
    if isinstance(parsed.get("scale_notes"), str):
        out["scale_notes"] = parsed["scale_notes"][:400]

    # Nested objects — type-check each and copy known sub-keys
    for top_key in ("framing", "lighting", "palette", "composition", "density"):
        sub = parsed.get(top_key)
        if isinstance(sub, dict):
            expected = EMPTY_SPEC[top_key]
            assert isinstance(expected, dict)
            target = dict(expected)
            for sk in expected.keys():
                v = sub.get(sk)
                if sk == "lens_mm":
                    # Schema says integer; a string like "fifty" must not
                    # pollute the int field via the generic str branch.
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        target[sk] = int(v)
                elif isinstance(v, str):
                    target[sk] = v[:300]
            out[top_key] = target

    # Materials is an array of {on, type, notes}
    materials = parsed.get("materials")
    if isinstance(materials, list):
        cleaned: list[dict] = []
        for m in materials[:20]:  # hard cap
            if not isinstance(m, dict):
                continue
            entry = {
                "on": str(m.get("on", ""))[:120],
                "type": str(m.get("type", ""))[:200],
                "notes": str(m.get("notes", ""))[:200],
            }
            if entry["on"] or entry["type"]:
                cleaned.append(entry)
        out["materials"] = cleaned

    return out


async def build_spec(
    *,
    user_message: str,
    persona_display_name: str,
    persona_discipline_brief: str,
    anthropic_client: AnthropicClient,
    scene_summary: str = "",
    timeout_sec: float = _SPEC_TIMEOUT_SEC,
) -> Spec:
    """Build a creative brief for one user turn.

    Robust by design: any failure path returns an empty Spec with a
    `fallback_reason`. The agentic loop in streaming.py will detect
    `is_populated == False` and skip the SPEC injection entirely —
    behavior degrades gracefully to pre-Quality-Plan defaults.

    Cost: one Sonnet call (~$0.02). Called once per user message; the
    spec is reused across all retry iterations.
    """
    started = time.monotonic()
    prompt = build_prompt(
        user_message=user_message,
        persona_display_name=persona_display_name,
        persona_discipline_brief=persona_discipline_brief,
        scene_summary=scene_summary,
    )

    try:
        resp = await asyncio.wait_for(
            anthropic_client.messages_create(
                model=_SPEC_MODEL,
                max_tokens=_SPEC_MAX_TOKENS,
                system="You are a strict JSON-only pre-production planner. Output exactly one JSON object and nothing else.",
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("spec.timeout elapsed_ms=%d", elapsed)
        return _empty_spec("timeout", elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("spec.api_error type=%s msg=%s", type(exc).__name__, str(exc)[:200])
        return _empty_spec(f"api_error:{type(exc).__name__}", elapsed)

    elapsed = int((time.monotonic() - started) * 1000)

    # Pull text from the response. Sonnet may return thinking + text
    # blocks; we only care about text.
    text = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    parsed = _parse_spec_response(text)
    if parsed is None:
        log.warning("spec.unparseable preview=%r elapsed_ms=%d", text[:200], elapsed)
        return _empty_spec("unparseable_json", elapsed)

    validated = _validate_and_coerce(parsed)

    usage = getattr(resp, "usage", None)
    spec = Spec(
        data=validated,
        spec_version=SPEC_BUILDER_VERSION,
        elapsed_ms=elapsed,
        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
    )

    log.info(
        "spec.built subject=%r elapsed_ms=%d in_tokens=%d out_tokens=%d",
        spec.data.get("subject", "")[:60], elapsed,
        spec.input_tokens, spec.output_tokens,
    )
    return spec


def spec_summary_for_event(spec: Spec) -> dict[str, Any]:
    """Compact payload for the `spec.built` WS event. Drops verbose
    sub-objects so the panel doesn't have to parse heavyweight data."""
    return {
        "subject": spec.data.get("subject", "")[:120],
        "time_of_day": (spec.data.get("lighting") or {}).get("time_of_day", "")[:60],
        "framing": (spec.data.get("framing") or {}).get("camera", "")[:60],
        "mood": (spec.data.get("lighting") or {}).get("mood", "")[:60],
        "spec_version": spec.spec_version,
        "elapsed_ms": spec.elapsed_ms,
        "fallback_reason": spec.fallback_reason,
    }
