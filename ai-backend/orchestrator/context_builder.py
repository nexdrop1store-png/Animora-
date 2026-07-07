"""
Context builder — assembles the layered LLM call.

Replaces the inline `system=` + `messages=` construction that used to live
inside the old single-file orchestrator. Each call to `build()` produces
a complete Anthropic Messages-API input: the layered system prompt, the
conversation history with vision attachments on the latest user message,
and the tool list.

Layering (docs/AI_ARCHITECTURE.md §7.1):

  Layer 1: master prompt + absolute rules + quality standards   ← cached
  Layer 2: tool catalog (Anthropic native — passed as `tools=`)
  Layer 3: persona extension                                    ← cached
  Layer 4: session memory summary (Phase 7 — currently empty)
  Layer 5: live scene context — substituted into {scene_context}

Vision attachments (Phase 2 — this module):
  - Most recent HD capture is attached as an image content block on the
    latest user message, if available and recent.
  - The viewport stream is NOT attached per-call (too expensive); it's
    used for the Phase 5 artist's-eye check which calls Claude vision
    independently.

`cache_control` annotations are added so Anthropic's prompt cache (90%
discount on cached input tokens, 5-min TTL) catches the long, stable
prefix. The cache cutoff is placed at the END of the persona extension —
everything before it caches; everything after (memory summary + scene
context + history + vision) does not.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..prompts.composition_rules import COMPOSITION_RULES, COMPOSITION_RULES_VERSION
from ..prompts.master_prompt import MASTER_PROMPT, MASTER_PROMPT_VERSION
from ..scene_intelligence import build_scene_context_block
from .image_media import sniff_image_media_type
from .personas import Persona
from .tools import BLENDER_TOOLS

log = logging.getLogger("animora.context_builder")

# Maximum age of an HD capture (seconds) before we consider it stale and
# don't attach it. Beyond this, the LLM should request a fresh render via
# render_preview rather than reason from a stale image.
HD_CAPTURE_MAX_AGE_SEC = 60.0

# Max recent conversation turns to include verbatim. Older turns are
# Phase-7 compressed into a summary block (not yet implemented).
RECENT_TURN_LIMIT = 20


def build(
    *,
    user_message: str,
    conversation_history: list[dict[str, Any]],
    scene_graph: dict[str, Any],
    prev_scene_graph: dict[str, Any] | None,
    persona: Persona,
    hd_capture: tuple[bytes, str, float] | None = None,
    session_memory_summary: str = "",
) -> dict[str, Any]:
    """Build the kwargs dict for `client.messages.stream(**result)`.

    Returns: dict with keys `system`, `messages`, `tools`. Caller adds
    `model`, `max_tokens` etc. before calling Anthropic.

    Args:
        user_message: the new turn's text
        conversation_history: prior turns ({role, content, [ts]} dicts)
        scene_graph: latest scene snapshot from the addon
        prev_scene_graph: snapshot before the current one (for diff). None
            if this is the first turn.
        persona: loaded persona (Phase 1 stub returns GENERALIST with
            empty extension; Phase 4 personas have real extension strings)
        hd_capture: optional (png_bytes, trigger_label, age_seconds) — the
            most-recent HD capture from the Redis vision buffer
        session_memory_summary: Phase 7's compressed summary of older turns
            (empty string until Phase 7 ships)
    """
    ctx = build_scene_context_block(scene_graph, prev_scene_graph)

    # ── Layer 1+3: cached prefix (master + persona) ────────────────────
    cached_blocks: list[dict[str, Any]] = []
    base = MASTER_PROMPT.replace("{scene_context}", "{__SCENE_PLACEHOLDER__}")

    # Insert persona extension + shared composition rules BEFORE the
    # scene placeholder, so they land in the cached prefix. The
    # composition rules are constant across every call → they're
    # cache-friendly and only invalidate the prefix once (the turn
    # following their introduction). Persona+rules together stay
    # cache-hit-ratio ≈ 0.99 for subsequent turns.
    persona_block = persona.extension if persona.extension else ""
    inserted = f"{persona_block}\n\n{COMPOSITION_RULES}\n\nCURRENT SCENE"
    base = base.replace("CURRENT SCENE", inserted)

    # Split at the scene placeholder so the cache cutoff lands JUST BEFORE
    # the live scene context. Everything above caches; scene below doesn't.
    cached_part, _, live_tail = base.partition("{__SCENE_PLACEHOLDER__}")
    cached_blocks.append({
        "type": "text",
        "text": cached_part,
        "cache_control": {"type": "ephemeral"},
    })

    # ── Layer 4: session memory summary (Phase 7 — usually empty) ──────
    tail_text = ""
    if session_memory_summary:
        tail_text += f"\n\nSESSION MEMORY (compressed from earlier in this session):\n{session_memory_summary}\n"

    # ── Layer 5: live scene context (always fresh) ──────────────────────
    tail_text += ctx["text"] + live_tail

    system: list[dict[str, Any]] = [
        cached_blocks[0],
        {"type": "text", "text": tail_text},
    ]

    # ── Messages: conversation history + new user turn ─────────────────
    history = conversation_history[-RECENT_TURN_LIMIT:]
    history_msgs = [{"role": m["role"], "content": m["content"]} for m in history]

    # New user turn — attach HD capture if fresh enough
    user_content: list[dict[str, Any]] | str
    if hd_capture is not None and hd_capture[2] <= HD_CAPTURE_MAX_AGE_SEC:
        img_bytes, trigger, age = hd_capture
        # NEVER trust the channel's format label — the vision pipe erases it
        # and the addon's capture_hd_png actually ships JPEG. Sniff the bytes.
        media_type = sniff_image_media_type(img_bytes)
        log.debug("Attaching HD capture (trigger=%s, age=%.1fs, %s) to user message",
                  trigger, age, media_type)
        # A user-uploaded reference is a TARGET to reproduce, not a snapshot
        # of the current viewport — frame it so the model treats it as spec.
        if trigger == "user_upload":
            frame = (
                "[USER-PROVIDED REFERENCE IMAGE — reproduce this faithfully in the "
                "3D scene: match its subject, proportions, colours, materials, any "
                "text/labels, and overall composition as closely as Blender allows. "
                "This image is the target to recreate, NOT a screenshot of the "
                "current viewport.]"
            )
        else:
            frame = f"[viewport snapshot, trigger='{trigger}', {age:.1f}s old]"
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(img_bytes).decode("ascii"),
                },
            },
            {
                "type": "text",
                "text": f"{frame}\n\n{user_message}",
            },
        ]
    else:
        user_content = user_message

    messages = history_msgs + [{"role": "user", "content": user_content}]

    # Phase 8 — cache_control breakpoints for the agentic loop.
    #
    # Anthropic's prompt cache (90% input-token discount, 5-min TTL)
    # honours up to 4 explicit `cache_control: {"type":"ephemeral"}`
    # breakpoints. Placing them at high-value stable boundaries lets
    # iteration 2+ of the loop hit cache on the system + tools + the
    # original user turn while only paying full price for the new
    # assistant_blocks + tool_result message at the tail.
    #
    # Already cached above (cached_blocks[0] gets `cache_control`):
    #   - end of master + persona prefix
    # Add here:
    #   - end of system (after the live scene tail)
    #   - end of tools array
    #   - end of the ORIGINAL user message (so the second-iteration
    #     prefix-suffix is identical to iteration 1's)
    if system and isinstance(system[-1], dict):
        system[-1]["cache_control"] = {"type": "ephemeral"}

    tools_with_cache: list[dict[str, Any]] = list(BLENDER_TOOLS)
    if tools_with_cache:
        # Anthropic accepts cache_control on the LAST tool entry to mark
        # the boundary at end-of-tools.
        last_tool = dict(tools_with_cache[-1])
        last_tool["cache_control"] = {"type": "ephemeral"}
        tools_with_cache[-1] = last_tool

    # Tag the user turn so iteration 2+ inherits the same cached prefix.
    if messages and isinstance(messages[-1], dict):
        last_msg = messages[-1]
        content = last_msg.get("content")
        if isinstance(content, list) and content:
            # Multimodal content list — add cache_control to the last block
            last_block = dict(content[-1])
            last_block["cache_control"] = {"type": "ephemeral"}
            content = list(content[:-1]) + [last_block]
            last_msg["content"] = content
        elif isinstance(content, str):
            # String content — promote to a single text block so we can
            # attach cache_control to it. Anthropic accepts both shapes.
            last_msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]

    return {
        "system": system,
        "messages": messages,
        "tools": tools_with_cache,
        # Returned for telemetry but not consumed by Anthropic:
        "_meta": {
            "prompt_version": MASTER_PROMPT_VERSION,
            "composition_version": COMPOSITION_RULES_VERSION,
            "persona": persona.id,
            "scene_object_count": ctx["object_count"],
            "scene_mode": ctx["mode"],
            "hd_attached": user_content is not user_message,
        },
    }


# ── Phase 8: tool_result message builder for the agentic loop ──────────

def build_tool_result_message(
    outcomes: dict[str, dict[str, Any]],
    tool_use_ids_in_order: list[str],
) -> dict[str, Any]:
    """Build the user-role message containing tool_result blocks for the
    next iteration of the agentic loop.

    Args:
        outcomes: dict[tool_use_id → outcome] from `ToolResultCoordinator
            .await_results()`. Each outcome contains at minimum
            `{is_error, output, error}` and optionally
            `{hd_capture_b64, hd_media_type, scene_diff}`.
        tool_use_ids_in_order: the order in which the assistant emitted
            its tool_use blocks. Anthropic requires the same order on the
            response side.

    Returns the message dict ready to append to `accumulated_messages`.
    """
    user_content: list[dict[str, Any]] = []

    for tool_use_id in tool_use_ids_in_order:
        outcome = outcomes.get(tool_use_id, {})
        is_error = bool(outcome.get("is_error", False) or outcome.get("error"))
        text_parts: list[str] = []

        # Build the text portion of the tool_result. The model uses this
        # to plan its next action (refine, finalize, recover).
        if is_error:
            text_parts.append(f"Error: {outcome.get('error', 'unknown error')}")
        else:
            text_parts.append(str(outcome.get("output") or "OK"))

        diff = outcome.get("scene_diff") or {}
        if isinstance(diff, dict):
            diff_text = _format_scene_diff(diff)
            if diff_text:
                text_parts.append(diff_text)

        result_blocks: list[dict[str, Any]] = [{
            "type": "text",
            "text": "\n\n".join(p for p in text_parts if p),
        }]

        # Embed the HD viewport capture as an image block if the addon
        # provided one. This is the load-bearing piece — the model
        # SEES its own work for the first time across iterations.
        hd_b64 = outcome.get("hd_capture_b64")
        if hd_b64:
            # Sniff the actual bytes rather than trusting outcome[hd_media_type]
            # — a stale addon can label JPEG as png and Anthropic rejects the
            # mismatch with a 400 that aborts the whole turn.
            try:
                media_type = sniff_image_media_type(base64.b64decode(hd_b64[:32]))
            except Exception:
                media_type = outcome.get("hd_media_type") or "image/jpeg"
            result_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": hd_b64,
                },
            })

        user_content.append({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": result_blocks,
            "is_error": is_error,
        })

    return {"role": "user", "content": user_content}


def _format_scene_diff(diff: dict[str, Any]) -> str:
    """Render the addon's value-aware scene_diff (operators.py:_scene_graph_diff_brief)
    into terse human-readable lines for the tool_result text block.

    The diff payload contains:
      - added: list of either {name, type, location, modifiers, materials}
        dicts (rich form) OR bare strings (fallback when over the size cap)
      - removed: list of names
      - modified: list of {name, fields_changed: {field: [before, after]}}
      - object_count_before / _after: ints
      - _truncated: optional string explaining size cap hit

    Layout is optimised for the model reading it during a continuation
    turn — bullets, named objects, before→after values for transforms,
    explicit modifier add/remove. Caps individual sections so a giant
    scene doesn't blow the line budget.
    """
    if not diff:
        return ""

    lines: list[str] = []

    added = diff.get("added") or []
    if added:
        lines.append("Added:")
        for item in added[:12]:
            if isinstance(item, dict):
                name = item.get("name", "?")
                t = item.get("type", "")
                loc = item.get("location")
                tail_bits: list[str] = []
                if loc:
                    tail_bits.append(f"location={tuple(loc)}")
                mods = item.get("modifiers") or []
                if mods:
                    mod_summary = ", ".join(
                        f"{m.get('name','?')}({m.get('type','?')})"
                        for m in mods[:4]
                    )
                    tail_bits.append(f"modifiers=[{mod_summary}]")
                mats = item.get("materials") or []
                if mats:
                    tail_bits.append(f"materials={mats[:4]}")
                tail = " — " + "; ".join(tail_bits) if tail_bits else ""
                lines.append(f"  - {name} ({t}){tail}")
            else:
                lines.append(f"  - {item}")
        if len(added) > 12:
            lines.append(f"  ...({len(added) - 12} more)")

    removed = diff.get("removed") or []
    if removed:
        lines.append("Removed: " + ", ".join(removed[:12])
                     + (f", ...({len(removed) - 12} more)" if len(removed) > 12 else ""))

    modified = diff.get("modified") or []
    if modified:
        lines.append("Modified:")
        for m in modified[:12]:
            if isinstance(m, dict):
                name = m.get("name", "?")
                changes = m.get("fields_changed") or {}
                bullets: list[str] = []
                for field, vals in changes.items():
                    if field in ("location", "rotation_euler", "scale") and isinstance(vals, list) and len(vals) == 2:
                        bullets.append(f"{field} {tuple(vals[0])}→{tuple(vals[1])}")
                    elif field == "modifiers" and isinstance(vals, dict):
                        before_names = [x.get("name", "?") for x in vals.get("before", [])]
                        after_names = [x.get("name", "?") for x in vals.get("after", [])]
                        added_mods = [n for n in after_names if n not in before_names]
                        removed_mods = [n for n in before_names if n not in after_names]
                        if added_mods:
                            bullets.append(f"modifiers +[{', '.join(added_mods[:4])}]")
                        if removed_mods:
                            bullets.append(f"modifiers -[{', '.join(removed_mods[:4])}]")
                    elif field == "materials" and isinstance(vals, dict):
                        before_mats = vals.get("before") or []
                        after_mats = vals.get("after") or []
                        added_mats = [m for m in after_mats if m not in before_mats]
                        if added_mats:
                            bullets.append(f"materials +{added_mats[:4]}")
                    elif field == "parent":
                        bullets.append(f"parent {vals[0]}→{vals[1]}")
                if bullets:
                    lines.append(f"  - {name}: {'; '.join(bullets)}")
                else:
                    lines.append(f"  - {name}")
            else:
                lines.append(f"  - {m}")
        if len(modified) > 12:
            lines.append(f"  ...({len(modified) - 12} more)")

    n_before = diff.get("object_count_before")
    n_after = diff.get("object_count_after")
    if n_before is not None and n_after is not None and n_before != n_after:
        lines.append(f"Object count: {n_before} → {n_after}")

    if diff.get("_truncated"):
        lines.append(f"(diff truncated: {diff['_truncated']})")

    if not lines:
        return ""
    return "Scene diff:\n" + "\n".join(lines)
