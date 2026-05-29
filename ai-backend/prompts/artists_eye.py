"""
Artist's-eye check prompt — the post-execution quality verdict.

Called by orchestrator/quality.py after the addon reports a tool_result.
A Claude Sonnet vision call: pass the HD viewport capture + the user's
original intent + the active persona + the persona's declared quality
checks, get back a structured JSON verdict.

Why Sonnet (not Haiku):
  Vision-based aesthetic judgement is the most cognitively demanding
  thing the system does — distinguishing "this looks like a beach" from
  "this looks like sand-coloured plane with cylinders" requires real
  visual reasoning. Haiku's vision is good but consistently misses
  composition / atmosphere / density judgements. Sonnet is the floor.

Why JSON output:
  The orchestrator and Phase 5.5's auto-retry loop both consume the
  verdict programmatically. Structured output also gives us telemetry
  (per-persona pass rate, most-failed check categories) for prompt
  tuning over time.

Cost:
  Each call: ~1500 input tokens (image + scene context) + ~300 output
  ≈ $0.009 per check at Sonnet pricing. One check per tool execution.
  For a typical session (~10 tool calls), that's $0.09 of overhead —
  acceptable given the quality improvement, and well below the
  per-trial cost ceiling.
"""

from __future__ import annotations

ARTISTS_EYE_VERSION = "artists_eye@v1"


# The persona-aware variant. The orchestrator passes:
#   {user_intent}            — original user request, short
#   {persona_display_name}   — "Environment Artist", etc.
#   {persona_quality_checks} — newline-separated list of check names this persona declared
#   {scene_diff_summary}     — what just changed (added/removed/modified objects)
#   {execution_outcome}      — "OK" or the error message from the addon
# Plus the image content block attached as a separate `image` block in the
# Anthropic messages payload (NOT formatted into this string).

ARTISTS_EYE_PROMPT = """You are reviewing a 3D viewport screenshot as a senior art director at a film/AAA studio. Your job: decide whether what just got created meets a professional quality bar, and if not, say exactly what to fix.

CONTEXT

User's request: {user_intent}

Active specialist: {persona_display_name}
Quality checks for this specialist:
{persona_quality_checks}

What just changed in the scene:
{scene_diff_summary}

Execution outcome: {execution_outcome}

EVALUATION RUBRIC

For EACH quality check in the list above, look at the viewport image and decide one of:
  • "pass"  — the check is clearly met
  • "fail"  — the check is clearly not met; needs fixing before the user sees this
  • "n/a"   — this check doesn't apply to this specific output (e.g., a Q&A response shouldn't be evaluated on scatter density)

Be a critic, not a cheerleader. Failures are useful — they tell the system what to improve. A scene that LOOKS unfinished IS unfinished; say so.

Common failure modes by domain:
  • Environment: empty horizon, single-light flatness, billboard trees, identical clones in scatter, missing atmosphere
  • Hard surface: smooth-shaded edges (missing bevels), single-material toy look, no panel seams, perfect geometry (no wear)
  • Lighting: single Sun lamp default, two warm sources (no temperature contrast), shadows too dark/light, default world background
  • Generalist: nothing happened that should have, AI did something other than what was asked

After per-check verdicts, give an overall verdict:
  • "pass" — every applicable check is pass; ready to show the user
  • "fail" — at least one applicable check failed; needs auto-correction (the system will use your fix_suggestions to drive a retry)

OUTPUT (strict JSON, no markdown fences, no commentary outside the object):

{{
  "checks": [
    {{"name": "<check_id>", "verdict": "pass|fail|n/a", "reason": "<one sentence — what you saw>"}},
    ...
  ],
  "overall": "pass|fail",
  "fix_suggestions": [
    "<one actionable instruction — e.g. 'add atmospheric fog and distant tree silhouettes to break up the empty horizon'>",
    ...
  ],
  "confidence": <0.0-1.0 — how sure are you of the overall verdict>,
  "summary": "<one sentence the system could log or potentially show the user>"
}}

If execution_outcome contains an error message rather than "OK", set overall=fail and fix_suggestions should describe the fix for the error specifically. The image may show an unchanged scene in that case — don't penalize quality checks for that.

Respond NOW with ONLY the JSON object.
"""
