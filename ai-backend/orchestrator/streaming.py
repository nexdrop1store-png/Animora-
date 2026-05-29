"""
Streaming LLM call + tool-call dispatch.

Phase 1 split out from the old monolithic orchestrator.py.
Phase 2 added context_builder + vision attachment.
Phase 2.5 (this round) replaces the raw `anthropic.AsyncAnthropic()` call
          with `AnthropicClient` — production wrapper with retry, timeout,
          cancellation, token tracking, structured errors.

The caller supplies an `AnthropicClient` instance (constructed once per
session by main.py from the BYOK or pooled key). That gives main.py the
handle it needs to call `client.cancel()` when the WS receives an
`interrupt` message.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..anthropic_client import AnthropicClient, StreamCancelled, StreamResult
from ..assets.fetcher import AssetFetchError, fetch_asset
from ..assets.query import format_for_model as format_assets_for_model
from ..assets.query import relevant_assets
from ..quality_enforcer import validate_script
from ..scene_intelligence import build_scene_context
from .context_builder import build as build_context, build_tool_result_message
from .events import bus
from .intent import classify as classify_intent, try_fast_path as try_intent_fast_path
from .personas import load_persona_extension
from .quality import run_artists_eye_check
from .retry import (
    build_revision_user_message,
    is_retriable,
    max_retries_from_env,
    summarize_verdict_for_event,
)
from .router import select_model
from .spec import Spec, _discipline_brief, build_spec, spec_summary_for_event
from .tool_result_coordinator import ToolResultCoordinator

log = logging.getLogger("animora.streaming")

# ── Agentic loop bounds (Phase 8) ──────────────────────────────────────
# Configurable via constants at the top so a single PR can tune the
# trade-off between quality and cost without touching the loop body.
_MAX_AGENT_ITERATIONS = 3
# Bail before Anthropic rejects the request at its 200k input ceiling.
_MAX_ACCUMULATED_INPUT_TOKENS = 150_000
# Total wall-clock across all iterations (per-stream timeout is already 600s).
_MAX_AGENT_WALL_CLOCK_SEC = 900
# How long to wait for the addon's tool_result(s) for one iteration.
# Atomic ops complete in well under 100ms; the only thing that needs
# real time here is `execute_animora_code` running a big AST-split script
# on the addon's main thread (rare; capped at ~30s for hero builds).
# 45s is generous for the happy path and bounds the user-visible wait
# when the addon is unresponsive (outdated install, frozen Blender, etc.)
# — the cofounder's 2026-05-28 18:18 session burned 6 minutes of 180s ×
# 3 timeouts because their addon was on pre-MCP-pivot code. We surface
# a clear "addon may be outdated" notice when the timeout fires below.
_TOOL_RESULT_WAIT_SEC = 45.0

# Sprint 4D follow-up — Pre-stream feature gates.
# The cofounder's session showed Animora "going unresponsive" for 20-30s
# AFTER the user submits a prompt but BEFORE the script starts running.
# Two heavy steps run there: the SPEC-builder Sonnet call (Quality Plan
# §5.1, ~18-25s) and the artist's-eye / final-review chain. They lift
# quality but every second they add is a second the panel sits silent.
# Turn them off by default for v1 demonstration; opt in via env var
# once perceived latency is no longer the gating issue.
def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


import os  # noqa: E402  (placed after the helper to keep its scope tight)
_ENABLE_SPEC_BUILDER = _flag("ANIMORA_ENABLE_SPEC", default=False)
# Output-token budget for execution iter 0 with forced tool_choice. The
# 32k default lets hero builds breathe but means Bedrock Opus 4.6
# routinely takes 60-90s on the SDK side before the addon sees the
# tool_call. Capping at 8k forces tighter, faster scripts — quality may
# soften on Lamborghini-class hero assets but the cofounder feedback
# loop is "show me anything in < 20s" right now.
_EXECUTION_MAX_TOKENS = int(os.environ.get("ANIMORA_EXEC_MAX_TOKENS", "8192"))


async def stream_response(
    user_message: str,
    conversation_history: list[dict],
    scene_context_str: str,  # legacy/unused since Phase 2
    plan: str,
    scene_graph: dict,
    send_token_cb,
    send_tool_call_cb,
    *,
    anthropic_client: AnthropicClient,
    prev_scene_graph: dict | None = None,
    hd_capture: tuple[bytes, str, float] | None = None,
    session_id: str = "unknown",
    session_memory_summary: str = "",
    send_quality_notice=None,  # H3 — optional async fn(payload) for soft warnings
    coordinator: ToolResultCoordinator | None = None,  # Phase 8 — agentic loop
    cancel_event: asyncio.Event | None = None,         # Phase 8 — STOP/interrupt
    send_quality_retry_event=None,  # Phase 5.5 — async fn(payload) for retrying/retry_succeeded/retry_exhausted
    on_inline_quality_check=None,   # Phase 5.5 — sync fn(verdict) main.py uses to skip its background check
    on_spec_built=None,             # Quality Plan §5.1 — sync fn(Spec) so main.py can pass it to final_review
) -> str:
    """Stream an LLM response to the client.

    Returns the full assistant text response for persistence.
    Raises StreamCancelled if the user issued an interrupt.
    """
    del scene_context_str  # superseded by context_builder

    await bus.emit("message.received", {
        "session_id": session_id,
        "text_length": len(user_message),
        "history_length": len(conversation_history),
    })

    # ── Phase 4: intent classification → persona selection ─────────────
    # Run Haiku to classify the user's request before picking a persona.
    # ~500ms typical. If classification fails, _fallback() returns the
    # generalist persona so the turn still proceeds.
    scene_summary = build_scene_context(scene_graph) if scene_graph else ""
    recent_context = _format_recent_context(conversation_history, n=2)

    # Surface "Animora is thinking…" sub-state IMMEDIATELY so the panel
    # doesn't sit on the dot animation in silence while Haiku classifies
    # + (optional) Sonnet specs the build. Each phase event flips the
    # panel's status pill to a more specific label, so a 30s pre-stream
    # setup feels like progress instead of a freeze.
    if send_quality_notice is not None:
        try:
            await send_quality_notice({
                "type": "phase",
                "phase": "drafting",
                "label": "Reading your request",
                "iteration": -1,
            })
        except Exception as exc:
            log.debug("phase.classifying send failed: %s", exc)

    # Sprint 4E — fast-path for obvious build verbs. Skips Haiku
    # (saves 500-2000ms before the panel sees any further phase event).
    intent_result = try_intent_fast_path(user_message)
    if intent_result is None:
        intent_result = await classify_intent(
            user_message=user_message,
            anthropic_client=anthropic_client,
            scene_summary=scene_summary[:800],   # cap — classifier doesn't need full graph
            recent_context=recent_context,
        )
    else:
        log.info("intent.fast_path session=%s intent=%s persona=%s",
                 session_id, intent_result.intent, intent_result.recommended_persona)
    await bus.emit("intent.classified", {
        "session_id": session_id,
        "intent": intent_result.intent,
        "persona": intent_result.recommended_persona,
        "confidence": intent_result.confidence,
        "complexity": intent_result.complexity_estimate,
        "classifier_elapsed_ms": intent_result.elapsed_ms,
        "fallback_reason": intent_result.fallback_reason,
    })

    # H3 — Surface classifier fallback as a user-visible notice so the
    # user knows when routing went to best-effort. Without this, a Haiku
    # timeout silently drops the turn to the non-execution router path
    # → Sonnet or Haiku is picked instead of Opus → quality degrades and
    # the user has no visibility.
    if intent_result.fallback_reason and send_quality_notice is not None:
        log.warning(
            "intent.classifier.fallback session=%s reason=%s — surfacing as quality_notice",
            session_id, intent_result.fallback_reason,
        )
        try:
            await send_quality_notice({
                "type": "quality_notice",
                "severity": "info",
                "summary": "Classifier hiccup — using best-effort routing for this turn.",
                "fix_suggestions": [],
                "details": {
                    "source": "intent_classifier_fallback",
                    "reason": intent_result.fallback_reason,
                },
            })
        except Exception as exc:
            log.debug("intent.fallback.notice_send_failed: %s", exc)

    persona = load_persona_extension(intent=intent_result.intent)

    # ── Model routing: factor in classifier's complexity estimate ───────
    # The router reads two classifier signals via the scene_graph dict:
    #   __intent_complexity → estimate_task_complexity falls back to it
    #   __intent_class      → router.select_model uses it to pick Opus
    #                         for execution intents (build/modify/etc.).
    # Cheap shim — proper router signature change comes later.
    scene_graph_aug = {
        **scene_graph,
        "__intent_complexity": intent_result.complexity_estimate,
        "__intent_class": intent_result.intent,
    }
    model, reason = select_model(user_message, conversation_history, scene_graph_aug, plan)
    log.info("Routing to %s (plan=%s, %s) persona=%s",
             model, plan, reason, persona.id)

    await bus.emit("model.selected", {
        "session_id": session_id, "model": model, "plan": plan, "reason": reason,
        "persona": persona.id,
    })

    ctx_kwargs = build_context(
        user_message=user_message,
        conversation_history=conversation_history,
        scene_graph=scene_graph,
        prev_scene_graph=prev_scene_graph,
        persona=persona,
        hd_capture=hd_capture,
        session_memory_summary=session_memory_summary,
    )
    meta = ctx_kwargs.pop("_meta")

    await bus.emit("llm.stream_started", {
        "session_id": session_id,
        "model": model,
        "persona": persona.id,
        "prompt_version": meta["prompt_version"],
        "scene_object_count": meta["scene_object_count"],
        "hd_attached": meta["hd_attached"],
    })

    # Output budget. Used to be Opus 4.7's native 32k native ceiling but
    # at ~30 tok/s on Bedrock Opus 4.6 that's a 90-second worst-case
    # stream per iteration — which the cofounder experiences as Animora
    # "going unresponsive". v1 caps execution turns at 8k (~270 lines of
    # bpy script, plenty for a single coherent build step) so the user
    # sees output much faster; the agentic loop can spread work across
    # multiple iterations if one turn isn't enough. Non-execution turns
    # keep the larger budget — explanations and Q&A aren't latency-bound.
    # Override via ANIMORA_EXEC_MAX_TOKENS env var if you want more rope.
    is_execution_intent_for_budget = intent_result.intent not in (
        "question", "simple_edit", "unknown", "",
    )
    max_output_tokens = _EXECUTION_MAX_TOKENS if is_execution_intent_for_budget else 16384

    accumulated_messages = list(ctx_kwargs["messages"])
    system_blocks = ctx_kwargs["system"]
    tools_with_cache = ctx_kwargs["tools"]

    # ── Quality Plan §5.1: SPECIFY step ─────────────────────────────────
    # For execution intents, build a structured creative brief (subject,
    # framing, lighting, palette, composition, materials, scale) BEFORE
    # the agentic loop fires. The brief lands in accumulated_messages
    # as a user-role pre-amble; every iteration sees it. Cached at the
    # turn level — retries reuse the same spec without re-calling Sonnet.
    #
    # Failure paths return an empty Spec; we skip the inject and the
    # loop runs exactly as it did pre-Quality-Plan. The SPEC layer is
    # purely additive — never a regression risk.
    is_execution_intent = intent_result.intent not in (
        "question", "simple_edit", "unknown", "",
    )
    built_spec: Spec | None = None
    if is_execution_intent and _ENABLE_SPEC_BUILDER:
        # Surface the spec-builder phase so the panel knows what's
        # happening during the ~20s Sonnet call.
        if send_quality_notice is not None:
            try:
                await send_quality_notice({
                    "type": "phase",
                    "phase": "drafting",
                    "label": "Planning the build",
                    "iteration": -1,
                })
            except Exception as exc:
                log.debug("phase.speccing send failed: %s", exc)

        scene_summary_short = ""
        try:
            sc = build_scene_context(scene_graph or {})
            scene_summary_short = sc[:600] if isinstance(sc, str) else ""
        except Exception:
            pass  # spec is best-effort; scene summary is optional context

        built_spec = await build_spec(
            user_message=user_message,
            persona_display_name=persona.display_name,
            persona_discipline_brief=_discipline_brief(persona.id, persona.display_name),
            anthropic_client=anthropic_client,
            scene_summary=scene_summary_short,
        )
        spec_text = built_spec.as_user_message()
        if spec_text:
            # Append after the user's actual message. Two consecutive
            # user-role messages are permitted by both Anthropic direct
            # API and Bedrock; the model reads them as "user said X,
            # then provided this additional context." The SPEC stays
            # in accumulated_messages for the whole turn, so every
            # iteration (including retries) sees the same contract.
            accumulated_messages.append({
                "role": "user",
                "content": spec_text,
            })

            # Sprint 3C — asset suggestions: rank the catalog against the
            # SPEC and inject the top matches as a separate user-role
            # message. Master prompt rule 22 tells the model to prefer
            # use_asset over hand-built when an entry matches.
            try:
                suggestions = relevant_assets(built_spec.data)
            except Exception as exc:
                log.debug("asset.query.failed session=%s exc=%s", session_id, exc)
                suggestions = []
            if suggestions:
                asset_text = format_assets_for_model(suggestions)
                if asset_text:
                    accumulated_messages.append({
                        "role": "user",
                        "content": asset_text,
                    })
                await bus.emit("assets.suggested", {
                    "session_id": session_id,
                    "count": len(suggestions),
                    "asset_ids": [s.asset.id for s in suggestions],
                })
        await bus.emit("spec.built", {
            "session_id": session_id,
            **spec_summary_for_event(built_spec),
        })
        if on_spec_built is not None:
            try:
                on_spec_built(built_spec)
            except Exception:
                pass  # callback must never break the request

    # ── Phase 8: Agentic multi-step loop ────────────────────────────────
    # Each iteration:
    #   1. Stream the model's next response
    #   2. If it emitted tool_use blocks → forward to addon, await
    #      tool_result + HD capture
    #   3. Append the assistant turn + a user-role tool_result message
    #      to accumulated_messages so iteration N+1 starts from there
    #   4. Bail on end_turn / max_tokens / max_iterations / token cap /
    #      wall-clock / cancel
    accumulated_input_tokens = 0
    turn_started = time.monotonic()
    final_text_parts: list[str] = []
    last_result: StreamResult | None = None

    # If the caller didn't pass a coordinator (legacy / non-agentic
    # callers), fall back to single-shot behaviour so existing tests still
    # pass. The eval harness does this today.
    if coordinator is None:
        log.debug("No ToolResultCoordinator provided — using single-shot path")

    # is_execution_intent is computed earlier in the SPECIFY block above
    # — same value reused here for the extended-thinking decision.

    # Phase 9 — extended thinking. Used for execution turns so the model
    # PLANS the asset before writing the bpy script (master prompt Rule #16).
    # Only fires on Opus.
    #
    # API shape note: Opus 4.7 / Sonnet 4.6 use the *adaptive* thinking API
    # — `thinking={"type": "adaptive"}` plus `output_config={"effort": ...}`
    # to dial reasoning depth. The older Opus-4.0 shape
    # `{"type": "enabled", "budget_tokens": N}` is rejected by 4.7 with
    # `"thinking.type.enabled is not supported for this model"`.
    thinking_config: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    if is_execution_intent and "opus" in model.lower():
        thinking_config = {"type": "adaptive"}
        output_config = {"effort": "high"}

    # ── Phase 5.5: per-turn retry state ──────────────────────────────────
    # The auto-retry loop reuses the existing agentic-loop iteration: when
    # the addon's tool_result lands, we run artist's-eye synchronously; on
    # failure we APPEND a revision-context user message and let the loop
    # iterate again. The retry budget is per-USER-MESSAGE, not per-iteration
    # — a model that legitimately needs three iterations to finish doesn't
    # eat into the retry pool. Only iterations that follow a quality FAIL
    # decrement the budget.
    quality_max_retries = max_retries_from_env()
    quality_retries_used = 0
    quality_last_verdict = None  # exposed via on_inline_quality_check callback
    quality_check_was_inline = False  # True if we ran at least one inline check this turn

    # Sprint 3 follow-up: track whether execute_blender_script was EVER
    # called across the whole turn. When the model over-uses use_asset
    # (asset-first regression — see master prompt rule 22 v12), it can
    # emit use_asset and stop without ever building the scene. The
    # script-rescue guard at the natural-end branch detects this and
    # forces one corrective iteration. One-shot — we don't rescue more
    # than once per turn (avoids infinite loops if the model keeps
    # ignoring the nudge).
    script_was_dispatched = False
    script_rescue_attempted = False

    # Sprint 4I — Hero-verb continuation hint. The cofounder reported a
    # repeatable regression: "build a wooden chair" → model emits 2
    # atomic calls (create_primitive cube + add_modifier bevel) and
    # then a closing "Build complete" text. The eval baseline doesn't
    # catch this because the only furniture benchmark explicitly
    # asks for low-poly. Sprint 1 (master prompt v17) teaches the
    # iteration-aware discipline; THIS safety net catches the case
    # where the model ignores the new doctrine and tries to ship a
    # hero asset on iteration 0 alone. After iteration 0 completes,
    # if the user message starts with a hero verb AND the iteration
    # emitted fewer mutating tool calls than `_HERO_MIN_CALLS`, we
    # append a "continue building" hint before iteration 1's stream.
    # Single-shot (only fires once per turn) so the model isn't
    # nagged on legitimately-simple builds.
    _HERO_VERBS = (
        "build", "make", "create", "model", "construct", "design",
    )
    _HERO_NOUNS = (
        "chair", "table", "sofa", "couch", "bed", "desk", "shelf",
        "bookshelf", "cabinet", "wardrobe", "stool", "bench",
        "car", "vehicle", "motorcycle", "truck", "bus", "plane",
        "boat", "ship", "spacecraft",
        "character", "person", "human", "creature", "dragon", "robot",
        "monster", "alien", "animal",
        "weapon", "gun", "rifle", "pistol", "sword", "blade",
        "room", "kitchen", "office", "living", "bedroom", "bathroom",
        "scene", "environment", "landscape", "beach", "forest", "desert",
        "mountain", "city", "street", "building", "house",
        "cathedral", "castle", "tower", "bridge",
        "still", "composition", "diorama", "tableau",
    )
    _HERO_MIN_CALLS = 5
    _user_lower = user_message.lower()
    is_hero_request = (
        is_execution_intent
        and any(_user_lower.lstrip().startswith(v) for v in _HERO_VERBS)
        and any(noun in _user_lower for noun in _HERO_NOUNS)
    )
    hero_hint_injected = False

    # Sprint 3 follow-up (rescue v2): when the rescue branch fires, we
    # set this for the FOLLOWING iteration only. Forces the model to
    # call execute_blender_script — without it, Bedrock's Opus 4.6
    # substitute sometimes responds with text+thinking only on the
    # rescue turn, which left turn recordings with `scripts_emitted=[]`
    # despite the model burning 7k+ output tokens. Consumed (read +
    # reset to None) at the top of every iteration.
    next_tool_choice: dict[str, Any] | None = None

    # Sprint 4C follow-up — Force tool_choice on iteration 0 for execution
    # intents. The cofounder's dev-user recordings show 0 tool_use across
    # 67 iterations / 34 prompts on Bedrock Opus 4.6 (the Opus 4.7
    # substitute): the model would burn output tokens on text + thinking
    # and never emit execute_blender_script. The script-rescue path
    # forced tool_choice on iter 1, but the loop had already paid the
    # cost of one wasted streaming call and the addon spent the gap
    # appearing frozen. Forcing tool_choice on iter 0 means execution
    # intents emit a script on the first try; non-execution intents
    # (questions, simple_edit) are unchanged. API constraint: forced
    # tool_choice is incompatible with `thinking` — handled by the
    # iter_thinking / iter_output_config gate inside the loop body.
    if is_execution_intent:
        # Sprint 4D — MCP pivot: model picks from the atomic suite +
        # execute_animora_code fallback. {"type":"any"} forces ANY tool
        # call but leaves the choice of WHICH tool to the model, so
        # simple builds use create_primitive + apply_material while
        # hero builds reach for execute_animora_code as the escape hatch.
        next_tool_choice = {"type": "any"}

    for iteration in range(_MAX_AGENT_ITERATIONS):
        # Bridge: collect tool_use_ids emitted in this iteration BEFORE
        # forwarding to the addon, so the coordinator can register them
        # and have futures waiting when the addon's tool_result arrives.
        # validate_script rejections produce a synthetic tool_result on
        # the coordinator so the loop doesn't deadlock on them.
        rejected_tool_use_ids: dict[str, str] = {}  # id → reason
        # Sprint 2B: track backend-only signal tools (request_final_review)
        # that the model can emit. These DON'T forward to the addon — the
        # orchestrator synthesises an immediate tool_result and uses the
        # call as a checkpoint signal for the artist's-eye batching logic.
        review_requested_ids: set[str] = set()
        # Sprint 3B: `use_asset` calls — orchestrator fetches the file
        # from PolyHaven, then dispatches a load_asset directive to the
        # addon with the resolved local path. id → (asset_kind, ok)
        # populated as fetches complete; used to synthesise tool_results.
        asset_fetch_outcomes: dict[str, dict[str, Any]] = {}

        async def _on_tool_call(name: str, tool_use_id: str,
                                 tool_input: dict[str, Any]) -> None:
            # Backend-only signal: model is declaring it's ready for the
            # whole-scene check. Don't dispatch to the addon. Mark the
            # tool_use_id so the post-await block knows to synthesise its
            # result + run artist's-eye on this iteration.
            if name == "request_final_review":
                log.info("checkpoint.requested session=%s iter=%d", session_id, iteration)
                await bus.emit("checkpoint.requested", {
                    "session_id": session_id, "iteration": iteration,
                    "tool_use_id": tool_use_id,
                })
                review_requested_ids.add(tool_use_id)
                return  # don't forward to addon
            # Asset-first: fetch from PolyHaven CDN, then dispatch a
            # load_asset directive (NOT the raw use_asset call) to the
            # addon with the resolved local path attached. The addon
            # doesn't need internet access — only file-system access to
            # the cache. Synthesises an error tool_result if the fetch
            # fails so the model can fall back to hand-built.
            if name == "use_asset":
                asset_id = str(tool_input.get("asset_id", "")).strip()
                target = str(tool_input.get("target", "")).strip()
                if not asset_id:
                    asset_fetch_outcomes[tool_use_id] = {
                        "ok": False,
                        "error": "use_asset called with empty asset_id",
                    }
                    return
                try:
                    fetched = await fetch_asset(asset_id)
                except AssetFetchError as exc:
                    log.warning("asset.fetch.failed session=%s id=%s exc=%s",
                                session_id, asset_id, exc)
                    await bus.emit("asset.fetch.failed", {
                        "session_id": session_id, "asset_id": asset_id,
                        "tool_use_id": tool_use_id, "error": str(exc)[:200],
                    })
                    asset_fetch_outcomes[tool_use_id] = {
                        "ok": False,
                        "error": f"Asset fetch failed: {exc}. Falling back to hand-built.",
                    }
                    return
                await bus.emit("asset.dispatched", {
                    "session_id": session_id, "asset_id": asset_id,
                    "kind": fetched.asset.kind.value, "cached": fetched.cached,
                    "local_path": str(fetched.local_path),
                })
                # Dispatch a load_asset tool_call to the addon. The
                # addon's operator knows how to apply HDRI / texture /
                # mesh appropriately given (kind, local_path, target).
                await send_tool_call_cb("load_asset", tool_use_id, {
                    "asset_id": asset_id,
                    "kind": fetched.asset.kind.value,
                    "local_path": str(fetched.local_path),
                    "name": fetched.asset.name,
                    "target": target,
                    "polyhaven_id": fetched.asset.polyhaven_id,
                }, iteration=iteration, user_intent=user_message)
                asset_fetch_outcomes[tool_use_id] = {
                    "ok": True,
                    "local_path": str(fetched.local_path),
                    "kind": fetched.asset.kind.value,
                }
                return  # success path also returns; coordinator awaits the addon's tool_result
            # Pre-execution safety gate — only the code-execution escape
            # hatch goes through the bpy AST validator. Atomic ops are
            # typed by JSON schema; their inputs are bounds-clamped on
            # the addon side and never need the import/builtins denylist.
            if name in ("execute_animora_code", "execute_blender_script"):
                script = tool_input.get("script", "")
                verdict = validate_script(script)
                if not verdict.ok:
                    log.warning("Script rejected: %s", verdict.reason)
                    await send_token_cb(f"\n\n[Script blocked: {verdict.reason}]")
                    await bus.emit("tool.rejected", {
                        "session_id": session_id, "tool_use_id": tool_use_id,
                        "reason": verdict.reason,
                    })
                    # Remember to synthesise a tool_result so the loop's
                    # coordinator.await_results doesn't deadlock waiting
                    # for an addon response that will never come.
                    rejected_tool_use_ids[tool_use_id] = verdict.reason
                    return
            await send_tool_call_cb(name, tool_use_id, tool_input,
                                     iteration=iteration, user_intent=user_message)
            await bus.emit("tool.dispatched", {
                "session_id": session_id, "tool": name, "tool_use_id": tool_use_id,
            })

        # Consume any forced tool_choice the previous iteration queued
        # (script-rescue v2). Reset before the call so it only applies
        # to ONE iteration — subsequent iterations are unconstrained.
        # When forcing a specific tool, we also DISABLE thinking for
        # this iteration: Anthropic's API rejects forced tool_choice
        # (anything other than {"type":"auto"} or {"type":"none"}) when
        # `thinking` is enabled. The rescue iteration doesn't need more
        # deliberation anyway — the model has already analysed the
        # scene; we want a forced action.
        iter_tool_choice = next_tool_choice
        next_tool_choice = None
        iter_thinking = None if iter_tool_choice is not None else thinking_config
        iter_output_config = None if iter_tool_choice is not None else output_config

        await bus.emit("agent.iteration_started", {
            "session_id": session_id, "iteration": iteration,
            "accumulated_input_tokens": accumulated_input_tokens,
            "thinking_mode": (iter_thinking or {}).get("type", "off"),
            "thinking_effort": (iter_output_config or {}).get("effort", "n/a"),
            "forced_tool_choice": (iter_tool_choice or {}).get("name", "none"),
        })

        # Sprint 4C follow-up — Live progress hint to the panel. With
        # forced tool_choice, the model goes straight into emitting the
        # tool_use JSON input — no `text` content_block_deltas are
        # produced, so the existing `stream_token` path stays silent for
        # the entire SDK call. That makes the addon look "frozen" for
        # 20-60s on hero builds even though the backend is actively
        # streaming. A single `phase` event flips the panel state to
        # "Drafting build plan…" the moment the iteration begins.
        if send_quality_notice is not None:
            label = "Drafting build plan" if is_execution_intent else "Composing reply"
            if iteration > 0:
                label = f"Revising (pass {iteration + 1})"
            try:
                await send_quality_notice({
                    "type": "phase",
                    "phase": "drafting",
                    "label": label,
                    "iteration": iteration,
                })
            except Exception as exc:
                log.debug("phase.drafting send failed: %s", exc)

        # Sprint 4E — `input_json_delta` -> `phase: composing`.
        # The SDK emits input_json_delta events as the tool_use's JSON
        # input is typed character-by-character. On forced-tool-choice
        # turns no `text` tokens stream, so this is the ONLY signal the
        # model is actively producing output. Fire `phase: composing`
        # the FIRST time any block emits a delta this iteration, so the
        # panel flips from "Drafting build plan" to "Composing the next
        # step" and the user knows the call is being assembled.
        _composing_emitted = {"sent": False}

        async def _on_input_json_delta(_block_index: int, _chunk: str) -> None:
            if _composing_emitted["sent"]:
                return
            _composing_emitted["sent"] = True
            if send_quality_notice is None:
                return
            try:
                await send_quality_notice({
                    "type": "phase",
                    "phase": "composing",
                    "label": "Composing the next step",
                    "iteration": iteration,
                })
            except Exception as exc:
                log.debug("phase.composing send failed: %s", exc)

        try:
            result: StreamResult = await anthropic_client.stream(
                model=model,
                max_tokens=max_output_tokens,
                system=system_blocks,
                messages=accumulated_messages,
                tools=tools_with_cache,
                on_token=send_token_cb,
                on_tool_call=_on_tool_call,
                on_tool_input_delta=_on_input_json_delta,
                thinking=iter_thinking,
                output_config=iter_output_config,
                tool_choice=iter_tool_choice,
            )
        except StreamCancelled:
            await bus.emit("llm.stream_cancelled", {
                "session_id": session_id, "model": model, "iteration": iteration,
            })
            log.info("Stream cancelled for session %s at iteration %d",
                     session_id, iteration)
            return "".join(final_text_parts)

        last_result = result
        accumulated_input_tokens += result.usage.input_tokens
        if result.output_text:
            final_text_parts.append(result.output_text)

        # Append the assistant's turn (text + tool_use blocks in order)
        # so the next iteration has the full conversation context.
        if result.assistant_content_blocks:
            accumulated_messages.append({
                "role": "assistant",
                "content": result.assistant_content_blocks,
            })

        await bus.emit("llm.stream_completed", {
            "session_id": session_id,
            "iteration": iteration,
            "model": model,
            "output_length": len(result.output_text),
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_hit_ratio": round(result.usage.cache_hit_ratio, 3),
            "elapsed_ms": result.elapsed_ms,
            "attempts": result.attempts,
            "persona": persona.id,
            "stop_reason": result.stop_reason,
            "tool_call_count": len(result.tool_calls),
        })

        # Sprint 4D — Track whether ANY scene-mutating tool_call was
        # emitted this iteration. Used by the natural-end rescue guard
        # below. Atomic create/modify/delete ops AND the
        # execute_animora_code escape hatch all count as mutations.
        # Pure-read tools (get_scene_info, viewport_screenshot,
        # get_object_info, render_*, request_final_review) do NOT count
        # — a turn that only inspects is a turn the user sees nothing.
        _MUTATION_TOOLS = {
            "execute_animora_code", "execute_blender_script",  # escape hatch + back-compat
            "create_primitive", "create_light", "create_camera",
            "set_transform", "add_modifier", "apply_material",
            "set_parent", "delete_object", "duplicate_object",
            "set_world", "use_asset", "load_asset",
        }
        if any(tc.get("name") in _MUTATION_TOOLS for tc in result.tool_calls):
            script_was_dispatched = True

        # Detect empty / whitespace-only scripts in any tool_use that fired.
        for tc in result.tool_calls:
            if tc.get("name") in ("execute_animora_code", "execute_blender_script"):
                script = str(tc.get("input", {}).get("script", "")).strip()
                if not script:
                    log.warning(
                        "execute_animora_code tool_use had EMPTY script. session=%s "
                        "tool_use_id=%s. Nothing will run.",
                        session_id, tc.get("id", "?"),
                    )
                    await bus.emit("llm.empty_script", {
                        "session_id": session_id,
                        "tool_use_id": tc.get("id", ""),
                    })

        # ── End conditions ───────────────────────────────────────────────
        if result.stop_reason == "max_tokens":
            log.warning(
                "Output truncated: model hit max_tokens=%d at iteration=%d for "
                "session=%s. The bpy script was likely cut off mid-write; "
                "bailing out of the agentic loop (looping won't help).",
                max_output_tokens, iteration, session_id,
            )
            await send_token_cb(
                f"\n\n[Output was cut off — the model reached its "
                f"{max_output_tokens}-token limit while writing the script. "
                f"Try splitting this into smaller steps, or ask me to do "
                f"the most important part first.]"
            )
            await bus.emit("llm.output_truncated", {
                "session_id": session_id, "model": model,
                "max_tokens": max_output_tokens,
                "output_tokens": result.usage.output_tokens,
                "iteration": iteration,
            })
            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "max_tokens",
                "iteration": iteration,
            })
            break

        if not result.tool_calls:
            # Natural end — model didn't ask for more tool work.
            #
            # Sprint 3 follow-up — script-rescue guard:
            # If this is an EXECUTION intent and the model never called
            # execute_blender_script across the whole turn (only used
            # use_asset, only emitted text, etc.), the scene was never
            # actually BUILT. Master prompt rule 22 v12 tells the model
            # to always end with at least one execute_blender_script
            # call, but the model still over-uses use_asset on
            # composition benchmarks. Inject a corrective user-role
            # nudge and iterate ONCE more — bounded by
            # script_rescue_attempted so we never loop indefinitely.
            if (
                is_execution_intent
                and not script_was_dispatched
                and not script_rescue_attempted
                and iteration < _MAX_AGENT_ITERATIONS - 1
            ):
                script_rescue_attempted = True
                accumulated_messages.append({
                    "role": "user",
                    "content": (
                        "[ANIMORA MUTATION-RESCUE — your turn is not complete]\n\n"
                        "You haven't made any scene mutations yet. Inspect "
                        "tools (get_scene_info, viewport_screenshot) don't "
                        "build anything — the user still sees an empty "
                        "scene. Call one of the atomic create/modify tools "
                        "now: create_primitive, create_light, create_camera, "
                        "set_transform, add_modifier, apply_material, "
                        "set_world. If the build genuinely needs procedural "
                        "geometry that no atomic tool can express (Geometry "
                        "Nodes, bmesh edits, sculpting), call "
                        "execute_animora_code with a complete bpy script. "
                        "Without one of these, the turn ends with nothing "
                        "the user can see."
                    ),
                })
                # Force the next iteration to actually call a mutating tool.
                # {"type":"any"} forces some tool but leaves WHICH to the
                # model so it can pick create_primitive vs execute_animora_code
                # appropriately for the request.
                next_tool_choice = {"type": "any"}
                await bus.emit("script.rescue.triggered", {
                    "session_id": session_id, "iteration": iteration,
                    "forced_tool": "any",
                })
                log.warning(
                    "mutation_rescue.triggered session=%s intent=%s persona=%s — "
                    "execution intent ended with zero mutating tool calls (forcing tool_choice=any)",
                    session_id, intent_result.intent, persona.id,
                )
                continue  # force one more iteration with the nudge

            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "natural_end",
                "iteration": iteration,
            })

            # Diagnostic: if it was an execution intent and the model
            # answered without ANY tool call (even after rescue), surface
            # that. This is the old "described a plan but didn't run"
            # path — kept for visibility when the rescue doesn't help.
            if is_execution_intent and iteration == 0 and not script_was_dispatched:
                log.warning(
                    "Execution intent produced NO tool_call. session=%s intent=%s "
                    "persona=%s stop_reason=%s output_length=%d.",
                    session_id, intent_result.intent, persona.id,
                    result.stop_reason, len(result.output_text),
                )
                await bus.emit("llm.no_tool_on_execution", {
                    "session_id": session_id,
                    "intent": intent_result.intent,
                    "persona": persona.id,
                    "stop_reason": result.stop_reason,
                    "output_length": len(result.output_text),
                })
                await send_token_cb(
                    "\n\n[I described a plan but didn't actually run the script. "
                    "Please rephrase your request, or send 'do it' to retry.]"
                )
            break

        # Hard stops before another iteration
        if iteration >= _MAX_AGENT_ITERATIONS - 1:
            log.info("Agent loop: hit MAX_AGENT_ITERATIONS=%d, stopping",
                     _MAX_AGENT_ITERATIONS)
            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "max_iterations",
                "iteration": iteration,
            })
            break

        if accumulated_input_tokens >= _MAX_ACCUMULATED_INPUT_TOKENS:
            log.warning("Agent loop: input token cap reached (%d ≥ %d), stopping",
                        accumulated_input_tokens, _MAX_ACCUMULATED_INPUT_TOKENS)
            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "input_token_cap",
                "iteration": iteration,
                "accumulated_input_tokens": accumulated_input_tokens,
            })
            break

        if time.monotonic() - turn_started >= _MAX_AGENT_WALL_CLOCK_SEC:
            log.warning("Agent loop: wall-clock cap reached, stopping")
            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "wall_clock_cap",
                "iteration": iteration,
            })
            break

        if cancel_event is not None and cancel_event.is_set():
            log.info("Agent loop: user cancelled, stopping")
            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "user_cancel",
                "iteration": iteration,
            })
            break

        # ── Tool-result feedback for next iteration ─────────────────────
        if coordinator is None:
            # No coordinator → can't await tool_results from an addon →
            # single-shot path (eval harness, legacy callers).
            #
            # Sprint 3 follow-up — script-rescue ALSO fires here:
            # without a coordinator we can't replay the agentic loop in
            # the normal sense, but we CAN do ONE direct re-stream when
            # the execution intent emitted no execute_blender_script.
            # Crucial for the eval runner where the model over-uses
            # use_asset (composition benchmarks) — without this branch
            # the rescue would only help WS-connected sessions.
            if (
                is_execution_intent
                and not script_was_dispatched
                and not script_rescue_attempted
                and iteration < _MAX_AGENT_ITERATIONS - 1
            ):
                script_rescue_attempted = True
                # Critical: every tool_use the model emitted on the
                # PRECEDING assistant turn must have a tool_result in
                # the next user-role message before we can re-stream.
                # In the no-coordinator path nothing was awaited, so we
                # synthesize OK tool_results for every tool_use id and
                # bundle them with the rescue nudge into ONE user-role
                # message (matching the same shape build_tool_result_message
                # produces for the coordinator path). Without this, the
                # Anthropic API rejects the next call with "tool_use ids
                # were found without tool_result blocks immediately after."
                rescue_content: list[dict[str, Any]] = []
                for tc in result.tool_calls:
                    rescue_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": (
                            "Acknowledged (no addon dispatch in this single-shot path). "
                            "Continue per the rescue note below."
                        ),
                    })
                rescue_content.append({
                    "type": "text",
                    "text": (
                        "[ANIMORA MUTATION-RESCUE — your turn is not complete]\n\n"
                        "You haven't made any scene mutations yet. Inspect "
                        "tools (get_scene_info, viewport_screenshot) don't "
                        "build anything — the user still sees an empty "
                        "scene. Call one of the atomic create/modify tools "
                        "now: create_primitive, create_light, create_camera, "
                        "set_transform, add_modifier, apply_material, "
                        "set_world. If the build genuinely needs procedural "
                        "geometry that no atomic tool can express, call "
                        "execute_animora_code with a complete bpy script. "
                        "Without one of these, the turn ends with nothing "
                        "the user can see."
                    ),
                })
                accumulated_messages.append({
                    "role": "user",
                    "content": rescue_content,
                })
                # See coordinator-path rescue branch above — same reason.
                next_tool_choice = {"type": "any"}
                await bus.emit("script.rescue.triggered", {
                    "session_id": session_id, "iteration": iteration,
                    "path": "no_coordinator",
                    "tool_uses_acknowledged": len(result.tool_calls),
                    "forced_tool": "any",
                })
                log.warning(
                    "mutation_rescue.triggered (no_coordinator) session=%s "
                    "intent=%s persona=%s tool_uses_acknowledged=%d — "
                    "execution intent ended with zero mutating tool calls (forcing tool_choice=any)",
                    session_id, intent_result.intent, persona.id,
                    len(result.tool_calls),
                )
                continue  # force one more iteration with the nudge

            await bus.emit("agent.loop_exit", {
                "session_id": session_id, "reason": "no_coordinator",
                "iteration": iteration,
            })
            break

        # Register futures for the tool_use ids the addon will respond to,
        # PLUS synthesise tool_results for any rejected by validate_script.
        all_ids = [tc["id"] for tc in result.tool_calls]
        coordinator.register(all_ids)
        for rid, reason in rejected_tool_use_ids.items():
            coordinator.resolve(rid, {
                "tool_use_id": rid,
                "is_error": True,
                "output": "",
                "error": f"Script blocked: {reason}",
            })
        # Sprint 2B: backend-only request_final_review calls don't reach the
        # addon — synthesise an OK tool_result so the model sees the call
        # completed cleanly. The actual checkpoint behavior fires below.
        for rid in review_requested_ids:
            coordinator.resolve(rid, {
                "tool_use_id": rid,
                "is_error": False,
                "output": "Quality system will inspect the result.",
            })
        # Sprint 3B: use_asset calls that FAILED at the fetch stage get
        # a synthesised error tool_result so the model knows the fallback
        # path is required. Successful fetches were already dispatched
        # to the addon as load_asset calls → coordinator awaits THEIR
        # tool_result naturally; we don't pre-resolve those.
        for rid, outcome in asset_fetch_outcomes.items():
            if outcome.get("ok"):
                continue  # dispatched to addon — wait for its tool_result
            coordinator.resolve(rid, {
                "tool_use_id": rid,
                "is_error": True,
                "output": "",
                "error": outcome.get("error", "Asset fetch failed."),
            })

        # Await the rest (the non-rejected ones will be resolved when the
        # addon's tool_result WS frame arrives in main.py).
        outcomes = await coordinator.await_results(
            all_ids,
            timeout_sec=_TOOL_RESULT_WAIT_SEC,
            cancel_event=cancel_event,
        )

        # Sprint 4E — detect "the addon never responded to ANY of our
        # tool_calls in this iteration". This is almost always the
        # addon-outdated path (the user updated the backend but didn't
        # `python scripts/sync_addon.py` + reload the addon, so the
        # installed addon doesn't know about the new atomic tools and
        # logs "Unknown tool call" without sending a tool_result).
        # Surface a clear panel notice the moment it happens so the
        # user isn't staring at "Animora is thinking" for 45+ seconds
        # while the timeout burns down per-iteration.
        non_rejected_ids = [i for i in all_ids if i not in rejected_tool_use_ids]
        if non_rejected_ids:
            timed_out_ids = [
                tid for tid in non_rejected_ids
                if outcomes.get(tid, {}).get("is_error")
                and "No tool_result from addon" in str(outcomes.get(tid, {}).get("error", ""))
            ]
            if timed_out_ids and send_quality_notice is not None:
                log.warning(
                    "addon.unresponsive session=%s iter=%d timed_out=%d/%d — "
                    "the installed addon likely doesn't recognise these tool names. "
                    "Run `python scripts/sync_addon.py` and reload the addon.",
                    session_id, iteration, len(timed_out_ids), len(non_rejected_ids),
                )
                try:
                    await send_quality_notice({
                        "type": "quality_notice",
                        "severity": "warning",
                        "summary": (
                            "Animora's addon didn't respond. Your installed Animora "
                            "is likely on older addon code that doesn't recognise "
                            "the new atomic tools."
                        ),
                        "fix_suggestions": [
                            "From a terminal in the Animora repo, run: python scripts/sync_addon.py",
                            "Then in Animora: Edit > Preferences > Add-ons — toggle Animora off and on.",
                            "Or just restart Animora.",
                        ],
                        "details": {
                            "source": "tool_result_timeout",
                            "timed_out_tool_calls": len(timed_out_ids),
                            "total_tool_calls": len(non_rejected_ids),
                            "wait_seconds": _TOOL_RESULT_WAIT_SEC,
                        },
                    })
                except Exception as exc:
                    log.debug("addon-unresponsive notice send failed: %s", exc)

        # Build the user-role message that carries tool_result + HD image
        # back to the model for iteration N+1.
        accumulated_messages.append(build_tool_result_message(outcomes, all_ids))

        # Sprint 4I — Hero-verb continuation hint. Fires once per turn,
        # after iteration 0 of a hero request whose blockout came in
        # under `_HERO_MIN_CALLS` tools. The hint is appended as a
        # user-role message so the next stream sees it as fresh
        # context from "the user" (effectively, the orchestrator
        # speaking on the user's behalf). It does NOT force a tool
        # call (no next_tool_choice mutation) — the model can still
        # legitimately decide the asset is complete and end the turn.
        # Threshold of 5 calls separates a wooden chair (needs ~11)
        # from a "make a sphere" (needs 1-2).
        if (
            is_hero_request
            and not hero_hint_injected
            and iteration == 0
            and len(all_ids) < _HERO_MIN_CALLS
            and iteration < _MAX_AGENT_ITERATIONS - 1
        ):
            hero_hint_injected = True
            accumulated_messages.append({
                "role": "user",
                "content": (
                    f"[ANIMORA HERO-ITERATION HINT] Your iteration 0 emitted "
                    f"{len(all_ids)} tool call(s). That's below the typical "
                    f"blockout for a hero asset like this one. Master prompt "
                    f"v17 Rule #4 expects iteration 0 to lay out every named "
                    f"part of the asset (a wooden chair has ~11 parts; a sofa "
                    f"has ~10; a beach scene has 15+); iteration 1 then adds "
                    f"materials and parents the hierarchy.\n\n"
                    f"Continue building on this iteration: add the parts you "
                    f"left out (legs, arms, backrest, cushions, palms, "
                    f"lighting, etc. — whatever applies to the request), then "
                    f"apply materials, then parent the hierarchy. Don't emit a "
                    f"closing 'Build complete' message yet — the asset isn't "
                    f"finished."
                ),
            })
            await bus.emit("hero.iteration_hint", {
                "session_id": session_id,
                "iteration": iteration,
                "tool_calls_so_far": len(all_ids),
                "threshold": _HERO_MIN_CALLS,
            })
            log.info(
                "hero_iteration_hint.injected session=%s iter=%d "
                "calls=%d threshold=%d — nudging model to continue building",
                session_id, iteration, len(all_ids), _HERO_MIN_CALLS,
            )

        await bus.emit("agent.iteration_done", {
            "session_id": session_id, "iteration": iteration,
            "tool_calls": len(all_ids),
            "rejected": len(rejected_tool_use_ids),
            "elapsed_ms": result.elapsed_ms,
        })

        # ── Phase 5.5 + Sprint 2B: inline quality check + auto-retry ────
        # Only runs when retries are enabled (ANIMORA_QUALITY_RETRIES>0)
        # and at least one tool_use successfully executed (no point
        # vision-checking when everything was rejected).
        #
        # Sprint 2B batch verification: the artist's-eye Sonnet vision
        # call is the dominant per-turn cost ($0.009 per iteration). To
        # cut that, only fire it at "checkpoints":
        #   - iteration 0 (first attempt must always be verified)
        #   - the model explicitly emitted request_final_review (signal
        #     it thinks the result is ready)
        #   - the last iteration (safety net before MAX_ITERATIONS bails)
        # Intermediate iterations where the model is mid-build skip the
        # check. Saves ~30% of vision spend on multi-iteration turns.
        if quality_max_retries == 0:
            continue  # retry disabled — defer quality check to main.py background path
        if not any(not o.get("is_error") for o in outcomes.values()):
            continue  # nothing executed cleanly — quality check has nothing to look at

        is_first_iter = (iteration == 0)
        review_requested = bool(review_requested_ids)
        is_last_iter = (iteration >= _MAX_AGENT_ITERATIONS - 1)
        is_checkpoint = is_first_iter or review_requested or is_last_iter
        if not is_checkpoint:
            log.debug(
                "checkpoint.skip session=%s iter=%d (no checkpoint signal)",
                session_id, iteration,
            )
            continue

        try:
            verdict = await run_artists_eye_check(
                session_id=session_id,
                user_intent=user_message,
                persona=persona,
                anthropic_client=anthropic_client,
                scene_graph_before=scene_graph if isinstance(scene_graph, dict) else None,
                scene_graph_after=None,  # TODO Phase 7+: thread post-execution scene graph
                execution_outcome="OK",
                send_quality_notice=None,  # don't surface mid-retry; we may still succeed
            )
        except Exception as exc:
            log.warning(
                "quality.inline.crash session=%s iter=%d exc=%s",
                session_id, iteration, exc,
            )
            continue  # quality check must never break the loop
        quality_last_verdict = verdict
        quality_check_was_inline = True

        if verdict.overall == "pass":
            if quality_retries_used > 0:
                await bus.emit("quality.retry_succeeded", {
                    "session_id": session_id,
                    "retries_used": quality_retries_used,
                    "verdict": summarize_verdict_for_event(verdict),
                })
                if send_quality_retry_event is not None:
                    try:
                        await send_quality_retry_event({
                            "type": "quality.retry_succeeded",
                            "retries_used": quality_retries_used,
                        })
                    except Exception:
                        pass
            # Pass on first try OR retry worked — let the loop reach its
            # natural exit (the next iteration's stream call will see the
            # model emit a text-only response → break path).
            continue

        # Verdict is fail. Decide whether to retry.
        if quality_retries_used >= quality_max_retries or not is_retriable(verdict):
            await bus.emit("quality.retry_exhausted", {
                "session_id": session_id,
                "retries_used": quality_retries_used,
                "retriable": is_retriable(verdict),
                "verdict": summarize_verdict_for_event(verdict),
            })
            # Surface the soft warning the user would have seen pre-Phase-5.5.
            if send_quality_notice is not None:
                try:
                    await send_quality_notice({
                        "type": "quality_notice",
                        "severity": "warning",
                        "summary": verdict.summary or "Quality check flagged this output.",
                        "failed_checks": [
                            {"name": c.name, "reason": c.reason}
                            for c in verdict.failed_checks
                        ][:3],
                        "fix_suggestions": verdict.fix_suggestions[:3],
                        "confidence": verdict.confidence,
                        "retries_used": quality_retries_used,
                    })
                except Exception as exc:
                    log.debug("quality_notice (retry_exhausted) send failed: %s", exc)
            continue  # don't bail the loop — model may still want to wrap up

        # We have retry budget — emit retrying event, append revision
        # context, let the loop iterate.
        quality_retries_used += 1
        await bus.emit("quality.retrying", {
            "session_id": session_id,
            "attempt": quality_retries_used,
            "max_retries": quality_max_retries,
            "verdict": summarize_verdict_for_event(verdict),
        })
        if send_quality_retry_event is not None:
            try:
                await send_quality_retry_event({
                    "type": "quality.retrying",
                    "attempt": quality_retries_used,
                    "max_retries": quality_max_retries,
                    "summary": verdict.summary[:200],
                })
            except Exception as exc:
                log.debug("quality.retrying WS send failed: %s", exc)

        accumulated_messages.append(build_revision_user_message(
            verdict,
            retry_attempt=quality_retries_used - 1,
            max_retries=quality_max_retries,
        ))
        # Loop iterates naturally — model sees tool_result + revision request,
        # responds with a revised execute_blender_script tool_use.

    # Loop complete — notify main.py if we ran an inline quality check
    # (so it skips its background post-turn check) and return the text.
    if quality_check_was_inline and on_inline_quality_check is not None:
        try:
            on_inline_quality_check(quality_last_verdict)
        except Exception:
            pass  # callback must never break the return path

    # Loop complete — return the accumulated assistant text. The final
    # quality check runs at the call site (main.py) on the FINAL HD
    # capture, once per turn, not per iteration.
    return "".join(final_text_parts)


def _format_recent_context(history: list[dict], n: int = 2) -> str:
    """Render the last N conversation turns as plain text for the intent
    classifier. We keep it tiny — Haiku doesn't need the full history,
    just enough to disambiguate references ('make it brighter' → which it?)."""
    if not history:
        return ""
    out = []
    for turn in history[-n * 2:]:  # *2 because each turn = user+assistant
        role = turn.get("role", "?").upper()
        content = str(turn.get("content", ""))[:200]
        if content:
            out.append(f"{role}: {content}")
    return "\n".join(out)
