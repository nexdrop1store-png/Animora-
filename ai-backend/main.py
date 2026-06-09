"""
Animora AI Backend — FastAPI application.

WebSocket: /ws/{session_id}?token=<access_token>
REST:      POST /validate-key  (key sanity check from Settings UI)
           GET  /health
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .anthropic_client import (
    AnthropicClient,
    StreamCancelled,
    _classify_error,
    fingerprint_key,
)
from .auth_middleware import AuthError, check_plan_access, check_rate_limit, decode_token
from .config import settings
from .key_source import NoKeyAvailable, pick_key
from .observability import configure as configure_logging, logger
from .orchestrator import bus, stream_response
from .orchestrator.personas import all_personas, load_persona_extension
from .orchestrator.final_review import run_final_review
from .orchestrator.quality import run_artists_eye_check
from .orchestrator.spec import Spec
from .recorder import SessionRecorder
from .orchestrator.tool_result_coordinator import ToolResultCoordinator
from .session_manager import (
    append_turn,
    get_redis,
    get_session,
    save_session,
    update_scene_context,
)
from .validate import router as validate_router
from .vision_buffer import (
    PAUSE_AT,
    RESUME_AT,
    clear_session_vision,
    get_latest_hd_capture,
    push_hd_capture,
    push_viewport_frame,
)

configure_logging()
log = logger("animora.main")

# Binary viewport frame header: 1B type + 2B w + 2B h + 8B ts = 13 bytes
_VPF_HEADER_FMT = ">BHHd"
_VPF_HEADER_SIZE = struct.calcsize(_VPF_HEADER_FMT)

app = FastAPI(title="Animora AI Backend", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://animora.tech", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(validate_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.3.0"}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
) -> None:
    # ── 1. Validate Origin (H4) ─────────────────────────────────────────
    # The Python websocket-client used by the Animora addon doesn't send
    # an Origin header — that's a desktop client, not a browser, so we
    # permit empty origin. Browser-based clients (the website redirect
    # flow, third-party tools) MUST match the configured allowlist;
    # otherwise a stolen JWT could drive a session from any browser tab.
    origin = (websocket.headers.get("origin") or "").strip().lower()
    if origin:  # browser-class client
        env_dev = os.environ.get("ANIMORA_ENV", "").lower() in ("dev", "development", "local")
        allowed = [o.strip().lower() for o in (settings.allowed_ws_origins or "").split(",") if o.strip()]
        if not env_dev and origin not in allowed:
            log.warning("ws.origin.rejected", extra={
                "session_id": session_id, "origin": origin,
            })
            await websocket.close(code=4403)
            return

    # ── 2. Authenticate BEFORE accepting the WebSocket ──────────────────
    # Doing this pre-accept removes a side-channel for session_id
    # enumeration (timing/error-message differences post-accept) and
    # prevents the per-connection setup cost from being paid for every
    # bogus token attacker can send. Failed handshakes close with a
    # generic 1008 (policy violation) and no body.
    try:
        claims = decode_token(token)
        check_plan_access(claims)
    except AuthError as exc:
        log.info("ws.auth.rejected", extra={
            "session_id": session_id, "code": exc.code,
        })
        # Reject the upgrade with the WebSocket protocol-level close code
        # 4401 (custom, "unauthorized"). The client sees the close, not
        # the reason — that's intentional; no info leak to attackers.
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # ── 2. Wait for hello message (carries BYOK key + client settings) ──
    # The addon sends a hello immediately after the WS upgrade succeeds.
    # We give it 5 seconds before falling back to pooled-key mode.
    hello_data: dict[str, Any] = {}
    try:
        # Note: receive_json raises if the next frame isn't text. We allow
        # the addon to skip hello entirely (legacy clients) — in that case
        # we hit the timeout, default to pooled, and replay the frame.
        import asyncio
        first_msg = await asyncio.wait_for(websocket.receive(), timeout=5.0)
        if "text" in first_msg and first_msg["text"]:
            try:
                parsed = json.loads(first_msg["text"])
                if parsed.get("type") == "hello":
                    hello_data = parsed
                else:
                    # Not a hello — stash it for the main loop to replay
                    hello_data = {"__replay__": parsed}
            except json.JSONDecodeError:
                pass
    except (asyncio.TimeoutError, WebSocketDisconnect):
        pass

    # Sprint 4E — Detect outdated addons IMMEDIATELY at hello, not 45s
    # later via coordinator timeouts. The addon's hello payload carries
    # `addon_protocol_version` (an integer that monotonically increments
    # whenever the dispatch contract changes). When the backend bumps to
    # protocol N but the installed addon is still on N-1, the user gets
    # silent tool_call drops — instead, fire a quality_notice now.
    _MIN_ADDON_PROTOCOL = 6
    addon_proto = int(hello_data.get("addon_protocol_version", 0) or 0)
    addon_version = str(hello_data.get("animora_version", "?"))
    if addon_proto < _MIN_ADDON_PROTOCOL:
        log.warning(
            "addon.outdated session=%s installed_protocol=%d expected>=%d addon_version=%s",
            session_id, addon_proto, _MIN_ADDON_PROTOCOL, addon_version,
        )
        # Best-effort notice — swallow send errors so the session still
        # proceeds (an outdated addon can still drive the panel).
        try:
            await websocket.send_json({
                "type": "quality_notice",
                "severity": "warning",
                "summary": (
                    "Your installed Animora addon is older than this backend. "
                    "Atomic tools (create_primitive, apply_material, …) will "
                    "appear as 'Unknown tool call' and the panel will sit on "
                    "'Animora is thinking' until the orchestrator gives up."
                ),
                "fix_suggestions": [
                    "From a terminal in the Animora repo: python scripts/sync_addon.py",
                    "Then in Animora: Edit > Preferences > Add-ons — toggle Animora off and on (or restart Animora).",
                ],
                "details": {
                    "source": "addon_protocol_mismatch",
                    "installed_protocol": addon_proto,
                    "expected_protocol": _MIN_ADDON_PROTOCOL,
                    "animora_version": addon_version,
                },
            })
        except Exception as exc:
            log.debug("addon.outdated notice send failed: %s", exc)
    else:
        log.info(
            "addon.protocol session=%s protocol=%d animora_version=%s",
            session_id, addon_proto, addon_version,
        )

    # ── 3. Pick the API key (BYOK from hello, else pooled fallback) ────
    try:
        decision = pick_key(hello_data.get("api_key", ""))
    except NoKeyAvailable as exc:
        log.error("session.no_key", extra={"session_id": session_id, "reason": str(exc)})
        await websocket.send_json({
            "type": "error",
            "code": "no_api_key",
            "message": (
                "No Anthropic API key configured. Open Animora settings and "
                "paste your Anthropic key, or contact support."
            ),
        })
        await websocket.close(code=4003)
        return

    # ── 4. Construct the per-session Anthropic client ──────────────────
    anthropic_client = AnthropicClient(
        decision.api_key,
        session_id=session_id,
        emit=bus.emit,
    )

    redis = await get_redis()
    session_data = await get_session(session_id)
    session_data["user_id"] = claims.user_id
    session_data["plan"] = claims.plan
    session_data["key_source"] = decision.source.value
    session_data["key_fingerprint"] = fingerprint_key(decision.api_key)

    await websocket.send_json({
        "type": "session_info",
        "session_id": session_id,
        "plan": claims.plan,
        "history_count": len(session_data.get("conversation_history", [])),
        "key_source": decision.source.value,
    })

    log.info("session.connected", extra={
        "session_id": session_id, "user_id": claims.user_id, "plan": claims.plan,
        "key_source": decision.source.value,
        "key_fingerprint": fingerprint_key(decision.api_key),
    })
    await bus.emit("session.connected", {
        "session_id": session_id, "user_id": claims.user_id,
        "plan": claims.plan, "key_source": decision.source.value,
    })

    # Phase 5: capture the active persona for each turn so the artist's-eye
    # check (triggered later by tool_result arrival) knows which checklist
    # to apply. The persona is decided inside streaming.py; we pick it up
    # via the intent.classified event the orchestrator emits.
    def _capture_persona(payload: dict[str, Any]) -> None:
        if payload.get("session_id") == session_id:
            session_data["last_persona_id"] = payload.get("persona", "generalist")
    bus.on("intent.classified", _capture_persona)

    # ── Sprint 4 — session recorder (Quality Plan §6.2 practical reframe) ─
    # Activated only when ANIMORA_RECORD_SESSIONS env is set. Captures
    # every turn as JSON + HD-capture PNGs under recordings/<session>/
    # for later mining (recordings_to_benchmarks.py, recordings_to_few_shot.py).
    # No-op when the env flag is unset, so production deploys carry no
    # overhead and no PII risk.
    recorder = SessionRecorder(session_id=session_id)
    if recorder.enabled:
        log.info("session.recorder.enabled session=%s dir=%s",
                 session_id, recorder.session_dir)

    def _record_iteration_done(payload: dict[str, Any]) -> None:
        if payload.get("session_id") != session_id or not recorder.enabled:
            return
        # Pull the latest HD capture if one landed for this iteration.
        # vision_buffer keeps a small ring of recent frames — this gives
        # us the post-script viewport snapshot the artist's-eye check
        # already used.
        async def _grab_and_close():
            try:
                latest = await get_latest_hd_capture(session_id)
                if latest:
                    recorder.write_hd_capture(latest[0])
            except Exception as exc:
                log.debug("recorder.hd_capture_grab_failed: %s", exc)
            finally:
                recorder.end_iteration()
        asyncio.create_task(_grab_and_close())

    def _record_iteration_started(payload: dict[str, Any]) -> None:
        if payload.get("session_id") != session_id or not recorder.enabled:
            return
        recorder.begin_iteration(
            scene_graph_before=session_data.get("last_scene_before"),
        )

    def _record_intent(payload: dict[str, Any]) -> None:
        if payload.get("session_id") != session_id or not recorder.enabled:
            return
        recorder.set_intent(
            intent=payload.get("intent", ""),
            persona=payload.get("persona", ""),
            model="",  # model name comes from model.selected, set below
            routing_reason="",
        )

    def _record_model(payload: dict[str, Any]) -> None:
        if payload.get("session_id") != session_id or not recorder.enabled:
            return
        # Patch the in-flight turn's model + routing_reason. set_intent
        # already ran (intent.classified fires before model.selected),
        # so we re-set with the model now known.
        cur = recorder._current_turn  # noqa: SLF001 — recorder is local to this handler
        if cur is not None:
            cur.model = payload.get("model", "")
            cur.routing_reason = payload.get("reason", "")[:240]

    def _record_rescue(payload: dict[str, Any]) -> None:
        if payload.get("session_id") != session_id or not recorder.enabled:
            return
        recorder.mark_script_rescue()

    bus.on("agent.iteration_started", _record_iteration_started)
    bus.on("agent.iteration_done", _record_iteration_done)
    bus.on("intent.classified", _record_intent)
    bus.on("model.selected", _record_model)
    bus.on("script.rescue.triggered", _record_rescue)

    stream_paused = False
    pending_tool_results: dict[str, dict[str, Any]] = {}

    # Phase 8 — agentic loop infrastructure. Both are per-session.
    # The coordinator correlates tool_use → tool_result for the loop in
    # streaming.py. The cancel_event is set by the WS `interrupt` handler
    # so streaming.py's await_results race bails the loop cleanly.
    tool_coordinator = ToolResultCoordinator(session_id=session_id)
    user_cancel_event = asyncio.Event()

    # Sprint 4H — Active turn task. The outer `while True` loop MUST
    # NOT `await _handle_user_message(...)` directly: that blocks the
    # only receive coroutine for the entire turn, so `tool_result`
    # frames from the addon sit unread in the WS buffer while
    # `stream_response → coordinator.await_results` is awaiting Futures
    # that can only be resolved by THAT SAME outer loop. Classic
    # await-deadlock — manifested in dev_server.log as
    # `coordinator.await.timeout (45s)` x3 followed by
    # `coordinator.resolve.already_done` all firing at the same instant
    # when MAX_AGENT_ITERATIONS finally let _handle_user_message
    # return. Fix: spawn it as a task so the receive loop keeps
    # draining the socket. `_active_turn_task` lets us interrupt /
    # cancel cleanly when a fresh user_message arrives mid-turn.
    _active_turn_task: asyncio.Task | None = None

    # H4 — Per-session message rate limit (text + binary frames). The
    # existing check_rate_limit() in user_message is plan-aware and
    # measured per-hour; this is a separate floor that catches floods of
    # ANY message type (scene_graph, hd_capture, interrupt, tool_result)
    # which would otherwise bypass the per-message-type gate. Sliding
    # 60-second window of monotonic timestamps; drop frame if exceeded.
    msg_window: list[float] = []

    # If hello arrived with a non-hello message, replay it through the loop
    replay_queue: list[dict[str, Any]] = []
    if hello_data.get("__replay__"):
        replay_queue.append(hello_data["__replay__"])

    try:
        while True:
            if replay_queue:
                msg = replay_queue.pop(0)
            else:
                raw = await websocket.receive()

                # H4 — Global per-session message rate limit (applies
                # to BOTH text + binary frames). Sliding 60s window.
                now = time.monotonic()
                msg_window = [t for t in msg_window if now - t < 60.0]
                if len(msg_window) >= settings.ws_messages_per_minute:
                    log.warning("ws.rate_limit.exceeded", extra={
                        "session_id": session_id,
                        "messages_in_window": len(msg_window),
                        "limit_per_minute": settings.ws_messages_per_minute,
                    })
                    # Drop without disconnecting — a slightly noisy addon
                    # shouldn't lose its session, just shed frames.
                    continue
                msg_window.append(now)

                if raw.get("bytes"):
                    stream_paused = await _handle_binary_frame(
                        raw["bytes"], session_id, websocket, stream_paused,
                    )
                    continue
                text = raw.get("text")
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue

            msg_type = msg.get("type")

            if msg_type == "resume":
                log.info("session.resumed", extra={"session_id": session_id})
                await websocket.send_json({
                    "type": "session_info",
                    "session_id": session_id, "plan": claims.plan,
                    "history_count": len(session_data.get("conversation_history", [])),
                    "key_source": decision.source.value,
                })

            elif msg_type == "interrupt":
                cancelled = anthropic_client.cancel()
                # Phase 8: also signal the agentic loop's cancel_event so
                # streaming.py's `await coordinator.await_results(...)` race
                # bails immediately. anthropic_client.cancel() handles the
                # in-flight stream; this handles the coordinator wait.
                user_cancel_event.set()
                await bus.emit("session.interrupt", {
                    "session_id": session_id,
                    "reason": msg.get("reason", "user_cancel"),
                    "cancelled": cancelled,
                })
                log.info("session.interrupt", extra={
                    "session_id": session_id, "cancelled": cancelled,
                })

            elif msg_type == "scene_graph":
                graph = msg.get("graph", {})
                await update_scene_context(session_id, graph)
                session_data["scene_context"] = graph

            elif msg_type == "hd_capture":
                trigger = msg.get("trigger", "unknown")
                data_b64 = msg.get("data", "")
                if data_b64:
                    try:
                        png_bytes = base64.b64decode(data_b64)
                        await push_hd_capture(session_id, png_bytes, trigger)
                        session_data["last_hd_capture_ts"] = time.time()
                        session_data["last_hd_trigger"] = trigger
                    except Exception as exc:
                        log.warning("hd_capture.store_failed", extra={
                            "session_id": session_id, "error": str(exc),
                        })

            elif msg_type == "tool_result":
                tool_use_id = msg.get("tool_use_id", "")
                error = msg.get("error", "")
                outcome = {
                    "tool_use_id": tool_use_id,
                    "is_error": bool(error),
                    "output": msg.get("output", ""),
                    "error": error,
                    "scene_diff": msg.get("scene_diff"),
                    # Phase 8 — addon embeds HD viewport capture directly
                    # in the tool_result message so the agentic loop's
                    # next iteration can attach it as image content.
                    "hd_capture_b64": msg.get("hd_capture_b64", ""),
                    "hd_media_type": msg.get("hd_media_type", "image/jpeg"),
                }
                pending_tool_results[tool_use_id] = outcome

                # Sprint 4H — log every tool_result arrival with timing
                # context so if the receive↔resolve deadlock ever
                # regresses, dev_server.log shows it explicitly. A
                # healthy session has receive timestamps spread across
                # the agent loop's wall-clock; an unhealthy one has
                # them batched at MAX_AGENT_ITERATIONS exit.
                log.info("tool_result.received", extra={
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "is_error": bool(error),
                    "has_hd": bool(outcome["hd_capture_b64"]),
                })

                # Phase 8 — route to the agentic-loop coordinator so the
                # awaiting stream_response() iteration can continue with
                # this tool_result + HD image as the model's next input.
                tool_coordinator.resolve(tool_use_id, outcome)

                # Track the most-recent HD capture and scene-after for
                # the end-of-turn quality check (moved out of this
                # per-tool_result spot into _handle_user_message so it
                # runs ONCE per user turn on the final iteration's
                # capture — no longer per-iteration).
                if outcome["hd_capture_b64"]:
                    session_data["last_hd_capture_ts"] = time.time()
                    session_data["last_hd_trigger"] = "post_script"

                await bus.emit(
                    "tool.completed" if not error else "tool.failed",
                    {"session_id": session_id, "tool_use_id": tool_use_id},
                )

            elif msg_type == "user_message":
                # Sprint 4H — If a previous turn is still in flight,
                # cancel it cleanly first so we don't run two
                # stream_responses against the same WebSocket.
                if _active_turn_task is not None and not _active_turn_task.done():
                    log.info("session.turn_superseded", extra={"session_id": session_id})
                    user_cancel_event.set()
                    _active_turn_task.cancel()
                    try:
                        await asyncio.wait_for(_active_turn_task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # Reset the per-turn cancel + coordinator so a STOP from
                # a previous turn doesn't leak into this one.
                user_cancel_event.clear()
                tool_coordinator.clear()
                # CRITICAL — spawn as a task, do NOT await. Awaiting
                # blocks the receive loop and deadlocks coordinator
                # Futures that are waiting for tool_result frames.
                _active_turn_task = asyncio.create_task(_handle_user_message(
                    msg, websocket, session_id, session_data, claims,
                    redis, anthropic_client,
                    tool_coordinator=tool_coordinator,
                    user_cancel_event=user_cancel_event,
                    recorder=recorder,
                ))
                # Attach an exception logger so unhandled errors inside
                # the task surface in dev_server.log instead of being
                # silently absorbed by the asyncio.Task.
                def _turn_done(t: asyncio.Task, sid: str = session_id) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        log.error("turn_task.failed", extra={
                            "session_id": sid,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:200],
                        })
                _active_turn_task.add_done_callback(_turn_done)

    except WebSocketDisconnect:
        log.info("session.disconnected", extra={"session_id": session_id})
        await bus.emit("session.disconnected", {"session_id": session_id})
    except RuntimeError as exc:
        # Starlette raises this when receive() is called on an already-closed
        # WS — happens whenever the addon hangs up before we notice. It is
        # semantically the same as WebSocketDisconnect; log it that way so
        # the terminal isn't flooded with traceback noise on normal closes.
        if "disconnect message has been received" in str(exc):
            log.info("session.disconnected", extra={
                "session_id": session_id, "via": "runtime_error_after_close",
            })
            await bus.emit("session.disconnected", {"session_id": session_id})
        else:
            log.exception("session.error", extra={
                "session_id": session_id, "error_type": type(exc).__name__,
            })
            try:
                await websocket.send_json({"type": "error", "message": "Internal server error"})
            except Exception:
                pass
    except Exception as exc:
        log.exception("session.error", extra={"session_id": session_id, "error_type": type(exc).__name__})
        try:
            await websocket.send_json({"type": "error", "message": "Internal server error"})
        except Exception:
            pass
    finally:
        # Sprint 4H — cancel any in-flight turn so it doesn't keep
        # streaming into a dead socket after the WS closed.
        if _active_turn_task is not None and not _active_turn_task.done():
            user_cancel_event.set()
            _active_turn_task.cancel()
            try:
                await asyncio.wait_for(_active_turn_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        await save_session(session_id, session_data)
        await clear_session_vision(session_id)


async def _handle_binary_frame(
    data: bytes,
    session_id: str,
    websocket: WebSocket,
    currently_paused: bool,
) -> bool:
    # H4 — size cap. A 4K JPEG at q=80 is < 2 MB; 8 MB (configurable) is
    # generous. Frames exceeding the cap are dropped silently — we don't
    # disconnect the session because the addon may be misconfigured (high
    # quality slider) rather than malicious.
    if len(data) > settings.ws_max_binary_frame_bytes:
        log.warning("ws.binary.oversize", extra={
            "session_id": session_id,
            "size": len(data),
            "limit": settings.ws_max_binary_frame_bytes,
        })
        return currently_paused
    if not data or len(data) < _VPF_HEADER_SIZE or data[0] != 0x01:
        return currently_paused
    try:
        _t, _w, _h, _ts = struct.unpack(_VPF_HEADER_FMT, data[:_VPF_HEADER_SIZE])
    except struct.error:
        return currently_paused
    payload = data[_VPF_HEADER_SIZE:]
    if not payload:
        return currently_paused

    depth = await push_viewport_frame(session_id, payload)

    if depth >= PAUSE_AT and not currently_paused:
        try:
            await websocket.send_json({"type": "pause_stream", "reason": "buffer_full", "buffer_depth": depth})
        except Exception:
            pass
        return True
    if depth <= RESUME_AT and currently_paused:
        try:
            await websocket.send_json({"type": "resume_stream", "buffer_depth": depth})
        except Exception:
            pass
        return False
    return currently_paused


def _tool_args_summary(name: str, tool_input: dict[str, Any]) -> str:
    """Sprint 4E — render a 1-line `tool.start` summary the panel
    can show as "⏵ {name}({summary})". Keeps the LLM-facing input
    untouched; pure cosmetic. Picks 1-3 most-identifying fields per
    tool kind (the name + key params); falls back to the first 2 keys
    if the tool isn't in the recognised set."""
    if not tool_input:
        return ""
    if name == "create_primitive":
        return f"{tool_input.get('kind', '?')} {tool_input.get('name', '')}".strip()
    if name == "create_light":
        return f"{str(tool_input.get('kind', '')).lower()} {tool_input.get('name', '')}".strip()
    if name == "create_camera":
        return str(tool_input.get("name", ""))
    if name == "set_transform":
        return str(tool_input.get("name", ""))
    if name == "add_modifier":
        return f"{tool_input.get('kind', '?')} on {tool_input.get('object', '')}".strip()
    if name == "apply_material":
        bc = tool_input.get("base_color")
        col = ""
        if isinstance(bc, (list, tuple)) and len(bc) >= 3:
            col = f"#{int(bc[0]*255):02x}{int(bc[1]*255):02x}{int(bc[2]*255):02x}"
        return f"{tool_input.get('object', '')} {col}".strip()
    if name == "set_parent":
        return f"{tool_input.get('child', '')} → {tool_input.get('parent', '')}"
    if name == "delete_object":
        return str(tool_input.get("name", ""))
    if name == "duplicate_object":
        return f"{tool_input.get('source', '')} → {tool_input.get('new_name', '')}"
    if name == "set_world":
        return "world"
    if name in ("execute_animora_code", "execute_blender_script"):
        return str(tool_input.get("intent_summary", ""))[:80]
    if name == "use_asset":
        return str(tool_input.get("asset_id", ""))
    if name == "get_scene_info":
        return ""
    if name == "viewport_screenshot":
        return ""
    if name == "get_object_info":
        return str(tool_input.get("name", ""))
    # Fallback: first 2 keys + values, truncated
    pairs = []
    for k, v in list(tool_input.items())[:2]:
        s = str(v)
        if len(s) > 32:
            s = s[:32] + "…"
        pairs.append(f"{k}={s}")
    return " ".join(pairs)


async def _safe_ws_send(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send a JSON message to the WS, returning True on success, False on
    any failure. Centralises the WS close-race swallow used throughout
    the error / cleanup paths so we never crash on an already-closed
    socket and never spam tracebacks for an idiomatic disconnect."""
    try:
        await websocket.send_json(payload)
        return True
    except (WebSocketDisconnect, RuntimeError) as exc:
        # Starlette: "Cannot call 'send' once a close message has been sent."
        log.debug("ws.send_failed_closed", extra={"error": str(exc)[:120]})
        return False
    except Exception as exc:
        log.debug("ws.send_failed", extra={"error_type": type(exc).__name__})
        return False


async def _handle_user_message(
    msg: dict[str, Any],
    websocket: WebSocket,
    session_id: str,
    session_data: dict[str, Any],
    claims,
    redis,
    anthropic_client: AnthropicClient,
    *,
    tool_coordinator: ToolResultCoordinator | None = None,
    user_cancel_event: asyncio.Event | None = None,
    recorder: SessionRecorder,
) -> None:
    user_text = msg.get("text", "").strip()
    if not user_text:
        return

    try:
        await check_rate_limit(redis, claims.user_id, claims.plan)
    except AuthError as exc:
        await websocket.send_json({"type": "error", "message": str(exc), "code": exc.code})
        return

    scene_graph = session_data.get("scene_context", {})
    history_snaps = session_data.get("scene_graph_history", [])
    prev_scene_graph = history_snaps[-2].get("graph") if len(history_snaps) >= 2 else None

    # Phase 5: stash the user intent + a snapshot of the scene BEFORE the
    # LLM acts, so the artist's-eye check has the right context when it
    # fires after the tool_result arrives.
    session_data["last_user_intent"] = user_text
    session_data["last_scene_before"] = dict(scene_graph) if scene_graph else None

    hd_capture: tuple[bytes, str, float] | None = None
    capture = await get_latest_hd_capture(session_id)
    if capture is not None:
        png_bytes, trigger = capture
        last_ts = session_data.get("last_hd_capture_ts", 0.0)
        age = time.time() - last_ts if last_ts else 999.0
        hd_capture = (png_bytes, trigger, age)

    async def send_token(token: str) -> None:
        await _safe_ws_send(websocket, {"type": "stream_token", "token": token})

    # Stage 3C — collect every atomic tool call this turn so an
    # exemplary build can be captured as a demonstration after the
    # post-turn critic verdict. Lightweight; always collected (the
    # capture itself is env-gated and only stores passing builds).
    turn_tool_calls: list[dict[str, Any]] = []

    async def send_tool_call(
        tool_name: str,
        tool_use_id: str,
        tool_input: dict,
        *,
        iteration: int | None = None,
        user_intent: str = "",
    ) -> None:
        # Stage 3C — record the call for demonstration capture (cheap;
        # not gated on recorder.enabled). Skip the heavy script body for
        # the escape hatch — demonstrations are for the atomic surface.
        if tool_name not in ("execute_animora_code", "execute_blender_script"):
            turn_tool_calls.append({"name": tool_name, "input": dict(tool_input or {})})
        else:
            turn_tool_calls.append({"name": tool_name, "input": {}})
        # Sprint 4 — feed the recorder before forwarding to the addon
        if recorder.enabled:
            recorder.add_tool_use(tool_name)
            # execute_animora_code (and its renamed predecessor) carries
            # a `script` field; capture for benchmark mining.
            if tool_name in ("execute_animora_code", "execute_blender_script"):
                recorder.add_script(str(tool_input.get("script", "")))
            # Sprint 4D — atomic tool inputs land in tool_inputs[] so
            # recordings_to_few_shot.py can mine (intent → tool sequence)
            # triples for persona few-shot blocks. Captures every call,
            # including the code-tool (sans script body, which is on
            # scripts_emitted[] already).
            recorder.add_tool_input(tool_name, tool_input or {})
        # Live progress: flip the panel into "Building <intent>" the
        # instant we dispatch any mutating tool. The addon also sets
        # EXECUTING state on receipt of the tool_call, but the phase
        # event surfaces an intent-summary label sooner so the panel's
        # status pill flips immediately.
        _PHASE_MUTATING_TOOLS = {
            "execute_animora_code", "execute_blender_script",
            "create_primitive", "create_light", "create_camera",
            "set_transform", "add_modifier", "apply_material",
            "set_parent", "delete_object", "duplicate_object",
            "set_world", "use_asset",
        }
        # Sprint 4E — Per-tool args_summary: a single, scannable line the
        # panel renders as the "⏵ {name}({summary})" chat line + state
        # pill detail. Matches the MCP/Claude-Desktop UX where the user
        # sees each tool call as it dispatches.
        args_summary = _tool_args_summary(tool_name, tool_input)
        if tool_name in _PHASE_MUTATING_TOOLS:
            # Pick a short label per tool: code-tools carry an
            # intent_summary; atomic tools name the operation directly.
            if tool_name in ("execute_animora_code", "execute_blender_script"):
                script_text = str(tool_input.get("script", ""))
                label = str(tool_input.get("intent_summary", "Running script"))[:120]
                lines_hint = script_text.count("\n") + 1 if script_text else 0
            else:
                label = f"{tool_name.replace('_', ' ').title()} — {args_summary}" if args_summary else tool_name.replace("_", " ").title()
                lines_hint = 1
            await _safe_ws_send(websocket, {
                "type": "phase",
                "phase": "building",
                "label": label,
                "script_lines": lines_hint,
            })
        # Sprint 4E — `tool.start` event fires BEFORE the tool_call so
        # the panel renders the upcoming tool's name+summary as a chat
        # line ("⏵ create_primitive(cube TableTop)"). Backward-compat:
        # the existing `tool_call` message still ships verbatim; addons
        # that don't handle `tool.start` ignore it without harm.
        await _safe_ws_send(websocket, {
            "type": "tool.start",
            "tool": tool_name,
            "tool_use_id": tool_use_id,
            "args_summary": args_summary,
            "iteration": iteration if iteration is not None else 0,
        })
        await _safe_ws_send(websocket, {
            "type": "tool_call",
            "tool": tool_name,
            "tool_use_id": tool_use_id,
            "input": tool_input,
            # Sprint 4E — per-iteration undo grouping signal. The addon
            # pushes ONE bpy.ops.ed.undo_push per (session, iteration)
            # tuple — subsequent tool_calls within the same iteration
            # roll into that one undo entry. user_intent is the label.
            "iteration": iteration if iteration is not None else 0,
            "user_intent": user_intent[:120],
        })

    conv_history = session_data.get("conversation_history", [])
    session_memory_summary = session_data.get("memory_summary", "")

    async def _send_soft_notice(payload: dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception as exc:
            log.debug("quality_notice send failed in stream path: %s", exc)

    # Phase 5.5 — track whether the streaming loop ran the artist's-eye
    # check inline. If it did, we skip the background _run_quality_check
    # below (the inline path already handled retries + notice surfacing)
    # and run the whole-scene FINAL REVIEW instead.
    inline_quality_check_ran = {"done": False}
    captured_spec: dict[str, Spec | None] = {"spec": None}

    def _on_inline_quality_check(verdict: Any) -> None:
        inline_quality_check_ran["done"] = True
        if recorder.enabled and verdict is not None:
            # Convert ArtistsEyeVerdict → recorder-friendly dict
            try:
                recorder.set_artists_eye({
                    "overall": getattr(verdict, "overall", ""),
                    "summary": getattr(verdict, "summary", "")[:240],
                    "confidence": float(getattr(verdict, "confidence", 0.0)),
                    "fix_suggestions": list(getattr(verdict, "fix_suggestions", []))[:5],
                    "failed_check_count": len(getattr(verdict, "failed_checks", [])),
                })
            except Exception as exc:
                log.debug("recorder.artists_eye_capture_failed: %s", exc)

    def _on_spec_built(spec: Spec) -> None:
        captured_spec["spec"] = spec
        if recorder.enabled:
            recorder.set_spec(spec.data)

    async def _send_quality_retry_event(payload: dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception as exc:
            log.debug("quality retry event send failed: %s", exc)

    async def _send_final_review_notice(payload: dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception as exc:
            log.debug("final_review_notice send failed: %s", exc)

    # Sprint 4 — open a recording for this turn. No-op when disabled.
    recorder.start_turn(user_text)

    try:
        full_response = await stream_response(
            user_message=user_text,
            conversation_history=conv_history,
            scene_context_str="",
            plan=claims.plan,
            scene_graph=scene_graph,
            send_token_cb=send_token,
            send_tool_call_cb=send_tool_call,
            anthropic_client=anthropic_client,
            prev_scene_graph=prev_scene_graph,
            hd_capture=hd_capture,
            session_id=session_id,
            session_memory_summary=session_memory_summary,
            send_quality_notice=_send_soft_notice,
            # Phase 8 — agentic loop infrastructure
            coordinator=tool_coordinator,
            cancel_event=user_cancel_event,
            # Phase 5.5 — inline auto-retry hooks
            send_quality_retry_event=_send_quality_retry_event,
            on_inline_quality_check=_on_inline_quality_check,
            # Quality Plan §5.1 — capture spec for final_review
            on_spec_built=_on_spec_built,
            # Stage 3A — the critic-correction loop reads the freshest
            # scene graph the addon has pushed (updated on every
            # depsgraph change during the build, not suppressed by the
            # vision exec-pause). Returns {} early in a turn before any
            # push lands; the critic step no-ops on an empty graph.
            get_live_scene_graph=lambda: session_data.get("scene_context", {}),
        )
    except StreamCancelled:
        # Finalize the recording FIRST — the WS may already be dead;
        # finalize_turn touches local disk only, never raises on WS
        # state. Then attempt to notify the panel, swallowing any
        # send failure (e.g. "Cannot call 'send' once a close message
        # has been sent" when the WS already closed).
        recorder.finalize_turn("cancelled")
        try:
            await websocket.send_json({"type": "stream_cancelled", "reason": "user_interrupt"})
        except Exception as send_exc:
            log.debug("ws.send_failed_after_cancel: %s", send_exc)
        return
    except WebSocketDisconnect:
        # Addon hung up mid-turn (keepalive timeout, user closed Animora,
        # network blip). The outer websocket_endpoint will see this on
        # its next receive() and exit cleanly; here we just record the
        # turn as "cancelled" and bubble back up. NOT classified as an
        # error — disconnect is a normal control flow.
        log.info("stream.disconnected_mid_turn", extra={"session_id": session_id})
        recorder.finalize_turn("cancelled", error_message="ws_disconnected")
        raise
    except RuntimeError as exc:
        # Starlette raises this when send/receive is called on an
        # already-closed WS. Semantically equivalent to WebSocketDisconnect
        # — happens when the addon disconnected during a send. Don't
        # surface as a "stream.failed" error.
        if "close message" in str(exc) or "disconnect message" in str(exc):
            log.info("stream.ws_closed_mid_turn", extra={"session_id": session_id})
            recorder.finalize_turn("cancelled", error_message="ws_closed")
            return
        # Other RuntimeError → fall through to generic handler
        code, msg_text = _classify_error(exc)
        log.error("stream.failed", extra={
            "session_id": session_id, "error_code": code, "error_type": type(exc).__name__,
        })
        recorder.finalize_turn("error", error_message=f"{code}: {msg_text}")
        await _safe_ws_send(websocket, {"type": "error", "code": code, "message": msg_text})
        return
    except Exception as exc:
        code, msg_text = _classify_error(exc)
        log.error("stream.failed", extra={
            "session_id": session_id, "error_code": code, "error_type": type(exc).__name__,
        })
        # Finalize the recording BEFORE attempting the WS send. If the
        # WS already closed (keepalive timeout, coordinator timeout,
        # addon disconnect mid-turn), send_json raises and would
        # otherwise prevent finalize_turn from running → empty
        # recordings dir even though we have a captured turn.
        recorder.finalize_turn("error", error_message=f"{code}: {msg_text}")
        await _safe_ws_send(websocket, {"type": "error", "code": code, "message": msg_text})
        return

    # End-of-turn quality routing:
    #
    # Two paths fork on whether the agentic loop ran artist's-eye inline:
    #
    #   Inline path (Phase 5.5 retry was enabled and at least one iteration
    #   triggered a check): per-step quality is already vetted. We now run
    #   the whole-scene FINAL REVIEW (Quality Plan §5.4) — a single
    #   composition-level art-director pass that compares the result
    #   against the SPECIFY brief and may surface a closing remark to the
    #   user (only when the verdict isn't "ship").
    #
    #   Legacy path (retries disabled OR nothing fired inline): keep the
    #   pre-Quality-Plan behavior — schedule the background artist's-eye
    #   check that surfaces a quality_notice on failures.
    #
    # Both paths gate on having captured a persona + user intent.
    last_persona_id = session_data.get("last_persona_id")
    last_user_intent = session_data.get("last_user_intent", "")
    last_scene_before = session_data.get("last_scene_before")
    last_scene_after = session_data.get("scene_context")

    # Stage 2 — deterministic scene-data critic. Runs on the post-build
    # scene graph with zero LLM cost and logs a structured verdict. This
    # is the live wiring of the critic from orchestrator/critic.py — it
    # tells us, per build, exactly which structural rubric checks passed
    # or failed (materials_present, scene_element_count, scale_sanity,
    # …). Diagnostic-only for now (logs + telemetry); it does not gate
    # or alter the build. The existing rescue gates in streaming.py
    # remain the corrective path.
    if isinstance(last_scene_after, dict) and last_scene_after.get("objects"):
        try:
            from .orchestrator.critic import run_scene_critic
            critic_report = run_scene_critic(
                last_scene_after,
                require_materials=True,
                require_light=False,
                expected_min_objects=1,
            )
            log.info("critic.scene_verdict", extra={
                "session_id": session_id,
                "passed": critic_report.passed,
                "score": critic_report.score,
                "summary": critic_report.summary,
                "errors": [f.check_id for f in critic_report.errors],
                "warnings": [f.check_id for f in critic_report.warnings],
            })
            if critic_report.failed:
                # One readable line listing the actionable findings.
                log.info("critic.findings session=%s\n%s",
                         session_id, critic_report.actionable_text())
            await bus.emit("critic.scene_evaluated", {
                "session_id": session_id,
                "passed": critic_report.passed,
                "score": critic_report.score,
                "error_checks": [f.check_id for f in critic_report.errors],
            })

            # Stage 3C — capture exemplary builds as demonstrations.
            # No-op unless ANIMORA_CAPTURE_DEMOS is set; only stores
            # builds that pass the critic above the quality threshold.
            try:
                from .orchestrator.demonstrations import DemonstrationLibrary
                mesh_count = sum(
                    1 for o in last_scene_after.get("objects", [])
                    if o.get("type") == "MESH"
                )
                DemonstrationLibrary().capture(
                    prompt=last_user_intent,
                    intent=session_data.get("last_intent", ""),
                    tool_calls=turn_tool_calls,
                    critic_score=critic_report.score,
                    critic_passed=critic_report.passed,
                    mesh_count=mesh_count,
                )
            except Exception as exc:
                log.debug("demonstration.capture_failed: %s", exc)
        except Exception as exc:
            log.debug("critic.scene_verdict_failed: %s", exc)

    if last_persona_id and last_user_intent:
        if inline_quality_check_ran["done"]:
            # Sprint 4 — wrap run_final_review so the verdict ALSO
            # lands on the recorder before/after surfacing to the user.
            async def _final_review_with_record():
                try:
                    verdict = await run_final_review(
                        session_id=session_id,
                        user_intent=last_user_intent,
                        spec=captured_spec["spec"],
                        anthropic_client=anthropic_client,
                        scene_graph_before=last_scene_before,
                        scene_graph_after=last_scene_after,
                        execution_outcome="agent_loop_complete",
                        send_final_review_notice=_send_final_review_notice,
                    )
                    if recorder.enabled and verdict is not None:
                        recorder.set_final_review({
                            "verdict": getattr(verdict, "verdict", ""),
                            "summary": getattr(verdict, "user_facing_note", "")[:300],
                            "confidence": float(getattr(verdict, "confidence", 0.0)),
                            "what_works": getattr(verdict, "what_works", "")[:200],
                            "what_to_fix": getattr(verdict, "what_to_fix", "")[:200],
                        })
                finally:
                    # finalize_turn writes the JSON; do it AFTER
                    # final_review so the recording carries the verdict.
                    recorder.finalize_turn("success")
            asyncio.create_task(_final_review_with_record())
        else:
            asyncio.create_task(_run_quality_check(
                websocket=websocket,
                session_id=session_id,
                user_intent=last_user_intent,
                persona_id=last_persona_id,
                anthropic_client=anthropic_client,
                scene_before=last_scene_before,
                scene_after=last_scene_after,
                execution_outcome="agent_loop_complete",
            ))
            # Legacy path: no final_review to wait on; finalize the
            # recording now. The background quality check still runs
            # but its verdict won't make it into THIS turn's recording
            # (it would land after the JSON is already on disk).
            recorder.finalize_turn("success")
    else:
        # Edge case: no persona/intent captured (rare — happens when
        # the classifier fails AND the SPEC fails AND no execution
        # intent fired). Still finalize so we don't leak the in-flight
        # turn record across the next user message.
        recorder.finalize_turn("success")

    await append_turn(session_id, "user", user_text, claims.plan)
    await append_turn(session_id, "assistant", full_response, claims.plan)
    session_data["conversation_history"] = (
        session_data.get("conversation_history", [])
        + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": full_response},
        ]
    )

    # Phase 7: kick off memory compression in the background if the
    # history has grown past the trigger. Doesn't block the user's next
    # turn — they can keep typing while Haiku summarises old context.
    # If compression succeeds, the next turn's context_builder picks up
    # the new memory_summary and prunes the verbatim turns from history.
    try:
        from .orchestrator.memory import maybe_compress
        async def _bg_compress() -> None:
            try:
                ran = await maybe_compress(session_data, anthropic_client)
                if ran:
                    await save_session(session_id, session_data)
            except Exception as exc:
                log.warning("memory.compress.bg_failed", extra={
                    "session_id": session_id, "error": str(exc),
                })
        asyncio.create_task(_bg_compress())
    except Exception:
        pass  # compression must never break a turn

    # Tell the addon the LLM/tool side of this turn is done so the panel
    # can transition out of THINKING/STREAMING/EXECUTING back to IDLE
    # without the user having to click STOP. Quality checks (Phase 5)
    # still run async in the background and may surface a quality_notice
    # later — that's decoupled from the turn-complete signal.
    try:
        await websocket.send_json({
            "type": "turn_complete",
            "had_tool_call": bool(full_response) and "tool_use_id" in str(session_data.get("conversation_history", [])[-1:]),
        })
    except Exception:
        pass  # WS may already be closing — not worth blocking on


async def _run_quality_check(
    *,
    websocket: WebSocket,
    session_id: str,
    user_intent: str,
    persona_id: str,
    anthropic_client: AnthropicClient,
    scene_before: dict | None,
    scene_after: dict | None,
    execution_outcome: str,
) -> None:
    """Phase 5 post-execution artist's-eye check.

    Scheduled via asyncio.create_task() from the tool_result handler so
    the WS receive loop keeps draining. Surfaces failures as a
    quality_notice WS message (no auto-fix in Phase 5 v1; the auto-retry
    loop lands in Phase 5.5).
    """
    persona = load_persona_extension(intent=None)
    # Resolve the actual persona object from the captured id
    for p in all_personas():
        if p.id == persona_id:
            persona = p
            break

    async def _send_notice(payload: dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception as exc:
            log.debug("quality_notice send failed: %s", exc)

    try:
        verdict = await run_artists_eye_check(
            session_id=session_id,
            user_intent=user_intent,
            persona=persona,
            anthropic_client=anthropic_client,
            scene_graph_before=scene_before,
            scene_graph_after=scene_after,
            execution_outcome=execution_outcome,
            send_quality_notice=_send_notice,
        )
        log.info("quality.complete", extra={
            "session_id": session_id, "persona": persona.id,
            "overall": verdict.overall, "elapsed_ms": verdict.elapsed_ms,
        })
    except Exception as exc:
        # Quality check must never bring down the session — log + move on
        log.error("quality.crash", extra={
            "session_id": session_id, "error_type": type(exc).__name__,
        })
