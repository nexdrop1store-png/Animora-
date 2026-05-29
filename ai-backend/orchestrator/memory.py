"""
Conversation memory compression (Phase 7 — docs/AI_ARCHITECTURE.md §8.2).

The conversation history grows unbounded across a long session. Without
compression, by turn ~50 we're feeding ~100k tokens of history into every
turn — slow, expensive, and after Anthropic's context window saturates,
broken outright.

Compression strategy
--------------------
We keep the most recent `_KEEP_VERBATIM_TURNS` turns in full fidelity
(the model needs the literal text of "what was I just told to do"),
and fold everything older into a rolling natural-language summary that
captures:
  - What's been built or modified in the scene
  - User-stated preferences (palette, style, scale, mood)
  - Things that were tried and rejected (so we don't repeat them)
  - Currently-in-progress goals

The summary is produced by Haiku (cheap, fast — ~500 ms typical), stored
on the session under `memory_summary`, and the corresponding old turns
are PRUNED from `conversation_history` once folded in. That way the
history stays bounded and the next turn's context-builder injects the
summary into the cached system prompt prefix (high cache hit rate since
the summary changes infrequently).

When does compression run
-------------------------
After a turn completes and the assistant message is persisted, we check:
  - Are there more than `_COMPRESS_TRIGGER_TURNS` raw turns waiting?
  - If yes → kick off compression in a background task (doesn't block the
    next user input).

The compression task:
  1. Reads `conversation_history` from the session
  2. Splits at `len(history) - _KEEP_VERBATIM_TURNS` — older items go in
  3. Sends old items + existing summary to Haiku
  4. Writes the new summary back, prunes the old items from history
  5. Re-saves the session

If Haiku fails (timeout, parse error), compression is a no-op for this
turn — history just stays uncompressed. Next turn we try again. The
session never fails because of memory work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..anthropic_client import AnthropicClient

log = logging.getLogger("animora.memory")


# How many of the most-recent turns we keep verbatim. Below this floor we
# never compress — the model needs literal recent context to understand
# "make it brighter" / "now do the other side" type references.
_KEEP_VERBATIM_TURNS = 12

# Once `conversation_history` exceeds this length, trigger compression.
# Chosen so we always keep at least 8 turns of new context between
# compressions (avoids compressing every single turn once history is long).
_COMPRESS_TRIGGER_TURNS = 20

# Per-call Haiku timeout. Compression runs in background so a slow call
# doesn't hurt the user, but we still cap it to avoid runaway costs.
_COMPRESSION_TIMEOUT_SEC = 15.0

# Max tokens for the summary itself. Sized so the summary is < 800 tokens
# even after many compressions — it's cached as part of the system prefix,
# so we want it small.
_SUMMARY_MAX_TOKENS = 800

_COMPRESS_MODEL = "claude-haiku-4-5-20251001"


_COMPRESSION_PROMPT = """You are compressing a 3D-tooling assistant's conversation history into a structured memory block. Be terse, factual, no prose.

PRIOR MEMORY (may be empty on first compression):
{prior_summary}

NEW TURNS TO FOLD IN (chronological order):
{turns_block}

Produce an updated memory block in EXACTLY this format. No code fences, no commentary.

ASSETS BUILT:
- (bulleted list of named objects/scenes the assistant created or modified, with key details like dimensions, materials, modifiers used. ≤ 12 items.)

USER PREFERENCES OBSERVED:
- (bulleted list of stylistic preferences, scale preferences, render engine, palette choices, etc. ≤ 6 items.)

REJECTED APPROACHES:
- (bulleted list of things the user explicitly disliked or asked to redo, ≤ 4 items.)

CURRENT GOAL:
- (one line — what is the user currently working toward, based on the most recent turns. "Unknown" if unclear.)

OPEN QUESTIONS:
- (bulleted list of things the assistant asked the user that haven't been answered yet, ≤ 3 items. "None" if none.)

Rules:
- If a field has no content, write "- (none)".
- Combine duplicates across PRIOR MEMORY + NEW TURNS — don't repeat.
- Use concrete details: object names, dimensions, hex/rgb colours, modifier types.
- Never invent details that weren't in the turns.
- Output ONLY the structured block. No preamble.
"""


def _format_turns(turns: list[dict[str, Any]]) -> str:
    """Render the to-be-summarised turns as a compact string."""
    lines = []
    for t in turns:
        role = str(t.get("role", "?")).upper()
        content = str(t.get("content", ""))
        # Cap each turn at 800 chars so a runaway message doesn't dominate
        if len(content) > 800:
            content = content[:790] + "...(truncated)"
        lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


async def maybe_compress(
    session_data: dict[str, Any],
    anthropic_client: AnthropicClient,
    *,
    force: bool = False,
) -> bool:
    """If history is long enough, compress old turns into memory_summary.

    Returns True if compression ran (whether it succeeded or not).
    Returns False if the threshold wasn't met — no work needed.

    Mutates `session_data` in place:
      - prunes the compressed turns from `conversation_history`
      - writes/updates `memory_summary`
      - updates `memory_compressed_at` timestamp
    """
    # Snapshot at entry — this is what we'll FOLD INTO the summary. We
    # do NOT use this snapshot's "tail" as the new history because new
    # turns may be appended during the Haiku await below; using the
    # snapshot would silently lose them. See "race condition" comment
    # near the commit step at the bottom.
    history_at_entry = list(session_data.get("conversation_history", []))
    if not force and len(history_at_entry) < _COMPRESS_TRIGGER_TURNS:
        return False

    # Split point — fold everything except the most recent
    # _KEEP_VERBATIM_TURNS into the summary. `to_fold` is captured BEFORE
    # the await so the LLM sees a stable input.
    split = len(history_at_entry) - _KEEP_VERBATIM_TURNS
    if split <= 0:
        return False
    to_fold = history_at_entry[:split]
    n_folded = len(to_fold)

    prior_summary = session_data.get("memory_summary", "") or "(empty)"
    prompt = _COMPRESSION_PROMPT.format(
        prior_summary=prior_summary,
        turns_block=_format_turns(to_fold),
    )

    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            anthropic_client.messages_create(
                model=_COMPRESS_MODEL,
                max_tokens=_SUMMARY_MAX_TOKENS,
                system="You are a precise conversation summariser. Output only the structured block. No code fences.",
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=_COMPRESSION_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("memory.compress.timeout after %.1fs", _COMPRESSION_TIMEOUT_SEC)
        return True
    except Exception as exc:
        log.warning("memory.compress.failed: %s: %s", type(exc).__name__, exc)
        return True

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Extract text content from the response
    text = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")
    text = text.strip()

    if not text:
        log.warning("memory.compress.empty_response")
        return True

    # ── Race-safe commit ─────────────────────────────────────────────
    # During the await above (typically 0.5-2s), main.py may have run
    # one or more additional turns and appended new items to
    # `session_data["conversation_history"]`. If we just write back
    # `history_at_entry[split:]`, those new turns are silently dropped.
    #
    # Instead: re-read the live history, find where our snapshot's
    # first kept turn lives in it, and keep EVERYTHING from that point
    # onward — which naturally preserves any newly-appended turns at
    # the tail.
    live_history = session_data.get("conversation_history", [])

    # Build a cheap fingerprint of the first turn we want to keep so we
    # can locate it in live_history even after appends. Turns are dicts
    # with role+content+ts — (role, content[:120], ts) is a robust key.
    def _fingerprint(turn: dict) -> tuple:
        return (
            str(turn.get("role", "")),
            str(turn.get("content", ""))[:120],
            float(turn.get("ts", 0.0)),
        )

    if split < len(history_at_entry):
        first_kept = history_at_entry[split]
        first_kept_fp = _fingerprint(first_kept)
        # Find the same turn in live_history (typically at the same index)
        new_start = None
        for i, t in enumerate(live_history):
            if _fingerprint(t) == first_kept_fp:
                new_start = i
                break
        if new_start is None:
            # Couldn't find our anchor — be safe: don't prune anything,
            # just store the summary and skip the truncation this round.
            log.warning(
                "memory.compress.anchor_missing — storing summary only, "
                "skipping history truncation (will retry next turn)"
            )
            session_data["memory_summary"] = text
            session_data["memory_compressed_at"] = time.time()
            return True
        new_history = live_history[new_start:]
    else:
        # Edge case: split landed past end of history_at_entry. Keep
        # whatever's in live_history wholesale.
        new_history = list(live_history)

    session_data["memory_summary"] = text
    session_data["memory_compressed_at"] = time.time()
    session_data["memory_turns_folded"] = (
        session_data.get("memory_turns_folded", 0) + n_folded
    )
    session_data["conversation_history"] = new_history

    log.info(
        "memory.compressed folded=%d kept=%d (live had %d during await) "
        "elapsed_ms=%d summary_len=%d",
        n_folded, len(new_history), len(live_history) - len(history_at_entry),
        elapsed_ms, len(text),
    )
    return True
