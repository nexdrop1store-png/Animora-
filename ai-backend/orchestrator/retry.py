"""
Phase 5.5 — quality-retry helpers.

Wired into `streaming.py`'s agentic loop: after each tool_result returns
from the addon, the loop runs `run_artists_eye_check`; if the verdict
fails AND we have retries left, we append a `user`-role "revision
context" message to `accumulated_messages` so the next iteration nudges
the model to fix the specific issues the artist's-eye flagged.

This module is INTENTIONALLY small. The retry is not a separate
orchestrator — it leverages the same iteration mechanism the agentic
loop already has. All we add is:

  • The retry-budget knob (`max_retries_from_env`)
  • The revision-context message builder
  • The decision predicate (`is_retriable`)

Wiring + event emission live in streaming.py so the loop body stays
the single source of truth for "what happens between tool calls."
"""

from __future__ import annotations

import os

from .quality import ArtistsEyeVerdict


# Per-user-message cap. We use an env var so ops can dial this down
# (e.g. for a cost-sensitive plan) without redeploying. Default 2 is the
# blueprint-mandated value — covers the common "one minor fix needed"
# case and gives one slack iteration for stubborn bugs.
DEFAULT_MAX_RETRIES = 2


def max_retries_from_env() -> int:
    """Read ANIMORA_QUALITY_RETRIES from the environment, default 2.
    Set to 0 to disable retry entirely (useful for the eval harness when
    we want to measure baseline quality without retry's contribution)."""
    raw = os.environ.get("ANIMORA_QUALITY_RETRIES")
    if raw is None:
        return DEFAULT_MAX_RETRIES
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_RETRIES
    return max(0, n)


def is_retriable(verdict: ArtistsEyeVerdict) -> bool:
    """Decide whether a failed verdict can be productively retried.

    A verdict is retriable iff:
      • It failed overall (no point retrying a pass)
      • It carries at least one actionable `fix_suggestion`
      • It's NOT a no-op verdict (fallback_reason populated means the
        check itself failed — retrying without new signal is pointless)

    Low-confidence verdicts (confidence < 0.3) are still retried — the
    model often produces a clearer second attempt that the next check
    can score with higher confidence.
    """
    if verdict.overall != "fail":
        return False
    if verdict.fallback_reason:
        return False
    return bool(verdict.fix_suggestions) or bool(verdict.failed_checks)


def build_revision_user_message(
    verdict: ArtistsEyeVerdict,
    *,
    retry_attempt: int,
    max_retries: int,
) -> dict:
    """Build the `user`-role message that gets appended to the loop's
    accumulated_messages after a failing artist's-eye check. The model
    sees this on the next iteration and (per master prompt rule, added
    in this phase) responds with a revised execute_animora_code
    tool_use.

    The structure is deliberate: lead with the user's perspective ("I
    looked at what you produced"), enumerate the specific failures, then
    list actionable fixes. We do NOT show raw Sonnet vision output — the
    fix_suggestions are already distilled.
    """
    failed_lines = []
    for c in verdict.failed_checks[:5]:
        failed_lines.append(f"  • {c.name}: {c.reason}")
    failed_block = "\n".join(failed_lines) or "  (no specific checks failed; overall judgment was 'fail')"

    fix_lines = []
    for s in verdict.fix_suggestions[:5]:
        fix_lines.append(f"  • {s}")
    fix_block = "\n".join(fix_lines) or "  (no specific fix suggestions; use your judgment)"

    attempt_str = f"attempt {retry_attempt + 1}/{max_retries + 1}"

    body = (
        f"I just looked at your result and it needs revision ({attempt_str}). "
        f"Specific issues:\n"
        f"{failed_block}\n\n"
        f"Suggested fixes:\n"
        f"{fix_block}\n\n"
        f"Please call `execute_animora_code` again with a REVISED script "
        f"that addresses these issues. Don't redo the parts that were "
        f"already correct — modify only what needs to change. "
        f"After this, no further revisions will be requested."
        if retry_attempt + 1 >= max_retries
        else f"I just looked at your result and it needs revision ({attempt_str}). "
        f"Specific issues:\n"
        f"{failed_block}\n\n"
        f"Suggested fixes:\n"
        f"{fix_block}\n\n"
        f"Please call `execute_animora_code` again with a REVISED script "
        f"that addresses these issues. Don't redo the parts that were "
        f"already correct — modify only what needs to change."
    )

    return {"role": "user", "content": body}


def summarize_verdict_for_event(verdict: ArtistsEyeVerdict) -> dict:
    """Compact dict suitable for both bus events and the WS messages
    that surface to the panel. Keeps payload < 1 KB so the panel doesn't
    have to parse heavyweight data structures."""
    return {
        "overall": verdict.overall,
        "summary": verdict.summary[:240],
        "failed_count": len(verdict.failed_checks),
        "confidence": round(verdict.confidence, 2),
        "fix_suggestions": [s[:200] for s in verdict.fix_suggestions[:3]],
    }
