"""
Final-review prompt — Quality Plan §5.4 (the whole-scene art-director pass).

After the agentic loop converges (success OR retry-exhausted), Animora
runs ONE composition-focused review on the final HD viewport capture
before the user sees the result. This is intentionally different from
the per-step artist's-eye check:

  • Artist's-eye (per-step) — fires after each tool execution; per-persona
    checklist; structured JSON verdict the auto-retry loop consumes; the
    granular "did this iteration's work pass."

  • FINAL REVIEW (this prompt) — fires ONCE per user turn at the end;
    looks at the scene as a unified composition; compares against the
    SPECIFY brief; produces a one-paragraph art-director note; meant
    to be surfaced to the user as a finishing comment ("Here's what I
    built. Note: the light feels a touch flat; want me to add a rim?")

The art-director note can also be silently logged (when the verdict is
"ship it") so the user only sees commentary when there's something
worth saying. The orchestrator owns that decision; this module just
produces the verdict.

## Cost

One Sonnet vision call per user turn (~$0.05). Replaces — not adds to —
the existing background `_run_quality_check` in main.py when the inline
retry path already ran the per-step check on every iteration.
"""

from __future__ import annotations

FINAL_REVIEW_VERSION = "final_review@v1"


# Bound the prompt size — the brief render can be long. Cap at 1.5k chars
# so the prompt stays compact and Sonnet vision has room for the image.
_BRIEF_RENDER_CAP = 1500


FINAL_REVIEW_PROMPT = """You are the senior art director reviewing the final result of one user turn at Animora — an AI 3D tool. The execution is complete; the agentic loop has stopped. Your job: look at the final viewport screenshot, compare it against the pre-production brief, and give a short professional verdict.

USER'S REQUEST
{user_intent}

PRE-PRODUCTION BRIEF (the contract the model was asked to execute)
{spec_render}

SCENE DIFF SUMMARY (what the loop changed)
{scene_diff_summary}

OUTCOME OF EXECUTION
{execution_outcome}

YOUR REVIEW

Look at the viewport image as a senior art director reviewing a shot. Evaluate the scene as a UNIFIED COMPOSITION — not item-by-item like the per-step quality checks already did. Specifically, comment on:

  • Match-to-brief: how well does the final result honor the SUBJECT, FRAMING, LIGHTING, PALETTE, COMPOSITION layers, and SCALE notes from the brief? Where did it deviate, and is the deviation an improvement or a regression?
  • Composition: does the eye land where it should? Is there a clear hero, supporting elements, and intentional negative space? Are the foreground / midground / background distinguishable?
  • Cohesion: does the scene read as ONE thing, or as a pile of unrelated objects? Lighting consistency, scale relationships, material coherence.
  • Professional bar: would a working studio ship this as a finished result, or send it back for another pass?

If the brief is missing (empty / "" / "no spec built"), just evaluate the scene against general professional bar — don't penalize for missing-brief alone.

VERDICT FORMAT (strict JSON, no markdown fences):

{{
  "verdict": "ship | refine | rebuild",
  "match_to_brief": "<one sentence — how well it tracked the brief, or 'no brief available'>",
  "what_works": "<one sentence — the strongest thing about the result>",
  "what_to_fix": "<one sentence — the single most impactful change, or 'nothing — ship it'>",
  "user_facing_note": "<short paragraph the panel can show as Animora's closing remark, OR empty string if verdict=ship>",
  "confidence": <0.0-1.0>
}}

VERDICT MEANINGS

  • "ship"      — meets professional bar; no comment needed (user_facing_note may be empty)
  • "refine"    — meets the bar but one specific improvement would lift it; surface the note as a soft suggestion
  • "rebuild"   — falls short of the bar; surface the note prominently. The user will likely follow up with a revision.

Be honest, not generous. The user is paying for senior-artist judgement — a "looks fine" verdict on a result that drops the proportion or has a single-grey material does not help them improve.

Respond NOW with ONLY the JSON object.
"""


def build_prompt(
    *,
    user_intent: str,
    spec_render: str,
    scene_diff_summary: str,
    execution_outcome: str = "OK",
) -> str:
    """Format the final-review prompt for one call. Pure formatting."""
    spec_block = spec_render[:_BRIEF_RENDER_CAP].strip() if spec_render else "(no brief built — evaluate against general professional bar)"
    return FINAL_REVIEW_PROMPT.format(
        user_intent=user_intent[:500],
        spec_render=spec_block,
        scene_diff_summary=(scene_diff_summary or "no scene-graph snapshot available")[:1200],
        execution_outcome=execution_outcome[:300],
    )
