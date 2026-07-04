"""
WebSocket client for the Animora AI backend.

Maintains a persistent connection, handles reconnect, dispatches:
- Text frames: JSON messages (stream_token, tool_call, session_info, error)
- Binary frames: viewport frame acknowledgements

Thread-safe: sends from any thread, dispatches received events via
bpy.app.timers on the main thread.
"""

from __future__ import annotations

import json
import logging
import queue
import struct
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("animora.ws")

_RECONNECT_DELAY_BASE = 1.0   # seconds
_RECONNECT_DELAY_MAX = 30.0
_PING_INTERVAL = 20.0

# H6 — Send-queue backpressure. The queue holds outbound text + binary
# frames waiting for the WS write thread. On a slow connection or while
# the backend has paused us, viewport frames pile up; unbounded queues
# turn long sessions into a slow OOM. Cap at 256 entries; on overflow
# binary frames (viewport — most-recent-wins) drop the OLDEST binary in
# the queue to make room. Text frames are too important to drop silently;
# if the queue is fully saturated by binaries, we still trim a binary
# rather than ever blocking the caller (which would freeze Blender).
_SEND_QUEUE_MAX = 256


class AuthRejectedError(Exception):
    pass


class AnimoraWSClient:
    def __init__(self) -> None:
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        # Bounded queue; we never block on .put(), see send_* methods below.
        self._send_queue: queue.Queue[bytes | str] = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._send_queue_lock = threading.Lock()  # serialises trim-then-put on overflow
        self._dropped_binary_frames = 0  # cumulative for diagnostics
        self._dropped_text_frames = 0
        self._stop_event = threading.Event()
        self._connected = False
        self._session_id: str = ""
        self._reconnect_delay = _RECONNECT_DELAY_BASE
        self._manual_disconnect = False

        # Backpressure state — read by vision.py before pushing frames.
        # Set by the server via `pause_stream` / `resume_stream` control
        # messages (docs/AI_ARCHITECTURE.md §3.1).
        self._stream_paused = False

        # Callbacks (set by panel/operators)
        self.on_stream_token: Optional[Callable[[str], None]] = None
        self.on_tool_call: Optional[Callable[[dict], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_connecting: Optional[Callable[[], None]] = None
        self.on_auth_rejected: Optional[Callable[[str], None]] = None
        self.on_transport_disconnected: Optional[Callable[[str], None]] = None

        # Called before every (re)connect attempt to fetch the CURRENT
        # access token. Without this, the background token refresh updates
        # auth.session but reconnects keep using the token captured at
        # connect() time — which expires after ~1h and turns every
        # transport blip into a spurious 401 sign-out.
        self.token_provider: Optional[Callable[[], str]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self, url: str, session_id: str, access_token: str) -> None:
        # A new auth/session handoff must replace any previous reconnect loop.
        # Otherwise an older thread can keep retrying with a stale token and
        # force the panel back into "Reconnecting..." after sign-in succeeds.
        if self._thread and self._thread.is_alive():
            self.disconnect()
            self._thread.join(timeout=2.0)

        self._url = url
        self._session_id = session_id
        self._access_token = access_token
        self._manual_disconnect = False
        self._stop_event.clear()
        self._schedule_callback(self.on_connecting)
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="animora-ws"
        )
        self._thread.start()

    def disconnect(self) -> None:
        self._manual_disconnect = True
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def send_message(self, text: str, context_flags: dict | None = None) -> None:
        # Sprint 1 Deep: reset the per-turn chat caps so the next turn
        # gets a fresh ✓-line budget. Safe to call at any time
        # (idempotent when no turn is active).
        try:
            from . import operators as _ops
            _ops.reset_per_turn_chat_caps()
        except Exception:
            pass
        payload = json.dumps({
            "type": "user_message",
            "text": text,
            "context": context_flags or {},
            "session_id": self._session_id,
        })
        self._enqueue(payload)

    def send_binary(self, data: bytes) -> None:
        self._enqueue(data)

    def send_json(self, obj: dict) -> None:
        self._enqueue(json.dumps(obj))

    def _enqueue(self, item: bytes | str) -> None:
        """Non-blocking put with overflow handling (H6).

        Strategy:
          • Try a non-blocking put. If it fits, done.
          • If the queue is full, atomically drain ONE oldest BINARY frame
            (viewport stream is most-recent-wins) and try again.
          • If we still can't fit a TEXT frame, drop the incoming text
            frame with a warning. Never block the caller — a blocking
            put() here would freeze the Blender main thread on slow
            networks.
        """
        try:
            self._send_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        # Overflow path — serialized so two concurrent senders don't
        # both try to trim and overshoot the cap.
        with self._send_queue_lock:
            # Try to free a slot by removing the oldest binary frame.
            trimmed = False
            try:
                # Walk the queue, copy items except the first binary.
                # queue.Queue doesn't expose iteration so we drain + repush.
                buffered: list[bytes | str] = []
                while True:
                    try:
                        buffered.append(self._send_queue.get_nowait())
                    except queue.Empty:
                        break
                # Drop the FIRST binary item we encounter (oldest binary).
                kept: list[bytes | str] = []
                for entry in buffered:
                    if not trimmed and isinstance(entry, (bytes, bytearray)):
                        trimmed = True
                        self._dropped_binary_frames += 1
                        continue
                    kept.append(entry)
                for entry in kept:
                    self._send_queue.put_nowait(entry)
            except Exception as exc:
                log.debug("send_queue trim failed: %s", exc)

            if trimmed:
                try:
                    self._send_queue.put_nowait(item)
                    if self._dropped_binary_frames % 50 == 1:
                        log.warning(
                            "send_queue overflow — dropped %d binary frames cumulatively",
                            self._dropped_binary_frames,
                        )
                    return
                except queue.Full:
                    pass

            # Still full and incoming is text — drop it. The session can
            # recover from a missed scene_graph/tool_result more gracefully
            # than from a hung main thread.
            if isinstance(item, str):
                self._dropped_text_frames += 1
                log.warning(
                    "send_queue full + only text frames present — dropping "
                    "outgoing TEXT frame (cumulative drops=%d). First 80 chars: %s",
                    self._dropped_text_frames, item[:80],
                )
            else:
                # Incoming binary, no binary to evict — queue is all text.
                # Drop the incoming binary; viewport stream is fine to skip.
                self._dropped_binary_frames += 1

    def _build_hello_payload(self) -> dict:
        """Assemble the hello message sent right after WS upgrade.

        Pulls the API key from the OS keyring (via credentials.py) and
        the current settings from preferences. The key never appears in
        any log line — only its sha256 fingerprint."""
        api_key = ""
        try:
            from . import credentials
            api_key = credentials.get_api_key() or ""
        except Exception as exc:
            log.warning("Failed to load API key from credentials: %s", exc)

        settings: dict = {}
        try:
            from .preferences import get_prefs
            prefs = get_prefs()
            settings = {
                "default_model": getattr(prefs, "default_model", "auto"),
                "temperature": getattr(prefs, "temperature", 1.0),
                "max_output_tokens": getattr(prefs, "max_output_tokens", 4096),
                "streaming_enabled": getattr(prefs, "streaming_enabled", True),
                "share_viewport": getattr(prefs, "share_viewport", True),
                "share_scene_graph": getattr(prefs, "share_scene_graph", True),
            }
        except Exception as exc:
            log.debug("Could not load settings for hello: %s", exc)

        return {
            "type": "hello",
            "api_key": api_key,
            "animora_version": "0.3.0",
            # Sprint 4E — protocol version. Bumped each time the addon's
            # tool dispatch contract changes so the backend can detect
            # "user updated backend but didn't sync addon" and warn
            # immediately on connect rather than letting the user sit
            # through 45s coordinator timeouts on every iteration.
            #   1 = pre-MCP-pivot (only execute_blender_script + a few)
            #   2 = MCP atomic surface (create_primitive, set_transform, etc.)
            #   3 = + tool.start handler + per-iteration undo + presence verify
            #   4 = + vision exec-pause + execute_animora_code (rename + no MATERIAL_PREVIEW switch)
            #   5 = + critical-path tool_result send (defer scene_diff / chat / HD capture)
            #   6 = + batched drain queue (no per-tool timer storm) + per-turn ✓-line cap
            "addon_protocol_version": 6,
            "settings": settings,
        }

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stream_paused(self) -> bool:
        """True when the backend has asked us to stop sending viewport
        frames (its buffer is full). vision.py honors this flag."""
        return self._stream_paused

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._connect_and_serve()
                self._reconnect_delay = _RECONNECT_DELAY_BASE
                if self._manual_disconnect or self._stop_event.is_set():
                    break
                raise RuntimeError("Connection closed")
            except AuthRejectedError as exc:
                self._connected = False
                self._schedule_callback(self.on_auth_rejected, str(exc))
                break
            except Exception as exc:
                if self._manual_disconnect or self._stop_event.is_set():
                    break
                log.warning("WS connection lost: %s — reconnecting in %.1fs", exc, self._reconnect_delay)
                self._connected = False
                self._schedule_callback(self.on_disconnected)
                self._schedule_callback(self.on_transport_disconnected, str(exc))
                if not self._stop_event.wait(self._reconnect_delay):
                    self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_DELAY_MAX)

    def _connect_and_serve(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            log.error("websocket-client not installed — cannot connect")
            self._stop_event.wait(10)
            return

        token = self._access_token
        if self.token_provider is not None:
            try:
                token = self.token_provider() or token
            except Exception as exc:
                log.debug("token_provider failed, using cached token: %s", exc)
        url = f"{self._url}/{self._session_id}?token={token}"
        log.info("Connecting to %s", self._url)

        ws = websocket.WebSocket()
        try:
            ws.connect(url, timeout=10)
        except websocket.WebSocketBadStatusException as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            if status in (401, 403):
                raise AuthRejectedError("Session expired — please sign in again") from exc
            raise
        self._ws = ws
        self._connected = True
        self._stream_paused = False
        self._schedule_callback(self.on_connected)

        # First message: hello — carries the BYOK Anthropic API key (if
        # the user pasted one) plus client-side settings (default model,
        # streaming on/off, version). Backend uses this to set up the
        # per-session AnthropicClient.
        hello_payload = self._build_hello_payload()
        try:
            ws.send(json.dumps(hello_payload))
        except Exception as exc:
            log.warning("Failed to send hello: %s", exc)

        # Then resume the session (replays history if any)
        ws.send(json.dumps({"type": "resume", "session_id": self._session_id}))

        last_ping = time.monotonic()

        while not self._stop_event.is_set():
            # Drain send queue
            while True:
                try:
                    item = self._send_queue.get_nowait()
                    if isinstance(item, bytes):
                        ws.send_binary(item)
                    else:
                        ws.send(item)
                except queue.Empty:
                    break

            # Ping keepalive
            if time.monotonic() - last_ping > _PING_INTERVAL:
                ws.ping()
                last_ping = time.monotonic()

            # Non-blocking receive
            ws.sock.settimeout(0.05)
            try:
                opcode, data = ws.recv_data()
                self._dispatch(opcode, data)
            except websocket.WebSocketTimeoutException:
                pass

        self._connected = False
        ws.close()

    def _dispatch(self, opcode: int, data: bytes | str) -> None:
        import websocket

        if opcode == websocket.ABNF.OPCODE_BINARY:
            # Binary frames are acknowledgements from backend — currently ignored
            return

        if opcode == websocket.ABNF.OPCODE_TEXT:
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                return
            msg_type = msg.get("type")
            if msg_type == "stream_token":
                token = msg.get("token", "")
                # State: THINKING → STREAMING on the first token
                self._schedule_main_thread(self._on_first_token_or_continue)
                self._schedule_callback(self.on_stream_token, token)
            elif msg_type == "tool_call":
                # State: → EXECUTING with intent_summary as the detail
                tool_name = msg.get("tool", "")
                inp = msg.get("input", {}) or {}
                intent_summary = str(inp.get("intent_summary", ""))[:120]
                self._schedule_main_thread(
                    lambda: self._set_state("EXECUTING", intent_summary, tool_name)
                )
                self._schedule_callback(self.on_tool_call, msg)
            elif msg_type == "tool.start":
                # Sprint 4E — fires from main.send_tool_call BEFORE the
                # tool_call message. Carries `args_summary` (e.g.
                # "cube TableTop") so the panel renders
                #   ⏵ create_primitive(cube TableTop)
                # as a chat line the moment the tool is about to
                # dispatch — matches Claude-Desktop's per-tool log UX.
                tool_name = str(msg.get("tool", ""))
                args_summary = str(msg.get("args_summary", ""))[:120]
                display = f"⏵ {tool_name}({args_summary})" if args_summary else f"⏵ {tool_name}"
                detail = args_summary or tool_name.replace("_", " ").title()
                # Sprint 1 Deep: route the ⏵ chat append through the
                # batched drain queue in operators._enqueue_cleanup so
                # 22 tool.start events in iteration 1 don't fire 22
                # individual timer callbacks. The status-pill flip
                # stays on its own main-thread hop (it's fast +
                # latency-sensitive, no chat history involved).
                def _enqueue_tool_start(d=display):
                    from . import operators as _ops
                    _ops._enqueue_cleanup(
                        chat_line=d, balance_exec_pause=False, force_redraw=True,
                    )
                self._schedule_main_thread(_enqueue_tool_start)
                self._schedule_main_thread(
                    lambda d=detail, t=tool_name: self._set_state("EXECUTING", d, t)
                )
            elif msg_type == "phase":
                # Live progress hint emitted by the orchestrator at points
                # where the existing stream_token / tool_call cadence
                # leaves a perceptible silence — e.g. during the LLM
                # call for a forced-tool turn (no text tokens stream
                # because the model goes straight to tool_use input).
                # `drafting` is the most useful — it flips the panel
                # into a visible "Drafting build plan…" the instant the
                # SDK call starts, rather than staying in THINKING with
                # nothing happening for 20-60s.
                phase = msg.get("phase", "")
                label = str(msg.get("label", ""))[:120]
                if phase == "drafting":
                    self._schedule_main_thread(
                        lambda: self._set_state("THINKING", label)
                    )
                elif phase == "composing":
                    # Sprint 4E — `input_json_delta` started streaming;
                    # the model is now typing the tool_use input. Same
                    # state as drafting but with a more specific label.
                    self._schedule_main_thread(
                        lambda: self._set_state("THINKING", label or "Composing the next step")
                    )
                elif phase == "building":
                    self._schedule_main_thread(
                        lambda: self._set_state("EXECUTING", label, "execute_animora_code")
                    )
            elif msg_type == "error":
                emsg = msg.get("message", "Unknown error")
                self._schedule_main_thread(lambda: self._set_state("ERROR", emsg))
                self._schedule_callback(self.on_error, emsg)
            elif msg_type == "session_info":
                log.debug("Session info: %s", msg)
            elif msg_type == "pause_stream":
                self._stream_paused = True
                log.debug("Stream paused by backend (depth=%s)", msg.get("buffer_depth", "?"))
            elif msg_type == "resume_stream":
                self._stream_paused = False
                log.debug("Stream resumed by backend (depth=%s)", msg.get("buffer_depth", "?"))
            elif msg_type == "stream_cancelled":
                log.debug("Stream cancelled: %s", msg.get("reason", "?"))
                self._schedule_main_thread(lambda: self._set_state("IDLE", "(stopped)"))
                self._schedule_callback(self.on_error, "(stopped)")
            elif msg_type == "quality_notice":
                # Phase 5: stash on state singleton, panel renders inline
                self._schedule_main_thread(lambda: self._on_quality_notice(msg))
                log.info("Quality notice [%s]: %s",
                         msg.get("severity", "?"), msg.get("summary", ""))
            elif msg_type == "turn_complete":
                # Server says LLM + tool dispatch are done. Auto-return
                # the panel to IDLE so the user doesn't have to STOP.
                # Sprint 1 Deep: flush the suppressed-tool-result
                # summary line if the per-turn ✓ cap kicked in.
                def _turn_complete_flush():
                    from . import operators as _ops
                    try:
                        _ops.flush_turn_end_chat_summary()
                    except Exception as exc:
                        log.debug("turn_complete.flush_failed: %s", exc)
                    # Diagnostic: log the material state of every mesh so
                    # we can see definitively why a build is grey
                    # (not-applied vs grey-color vs viewport-not-showing).
                    try:
                        _ops.log_material_diagnostic()
                    except Exception as exc:
                        log.debug("turn_complete.material_diagnostic_failed: %s", exc)
                self._schedule_main_thread(_turn_complete_flush)
                # NOTE: the MATERIAL_PREVIEW switch is NOT done here.
                # Switching shading at turn_complete forces EEVEE to
                # compile EVERY material in the scene at once on the
                # main thread — that was the "compiling EEVEE shaders"
                # hang. Instead, operators._ensure_render_responsive()
                # switches to MATERIAL_PREVIEW EARLY (while the scene
                # is still empty, so the switch is instant) and enables
                # background shader compilation, so per-material
                # compiles happen incrementally and off the main
                # thread as materials are applied.
                self._schedule_main_thread(
                    lambda: self._set_state("COMPLETE", "")
                )

    def _schedule_callback(self, cb: Callable | None, *args: Any) -> None:
        if cb is None:
            return
        import bpy

        def _call():
            try:
                cb(*args)
            except Exception as exc:
                log.error("Callback error: %s", exc)
            return None

        bpy.app.timers.register(_call, first_interval=0.0)

    def _schedule_main_thread(self, fn: Callable[[], None]) -> None:
        """Hop a zero-arg callable onto Blender's main thread via the
        app.timers queue. Used for state mutations from the receive
        thread — state.set_state() must run on main because it triggers
        area redraws."""
        import bpy

        def _call():
            try:
                fn()
            except Exception as exc:
                log.error("Main-thread hop failed: %s", exc)
            return None

        bpy.app.timers.register(_call, first_interval=0.0)

    # ── State transition helpers (called on main thread) ──────────────

    def _set_state(self, state_name: str, message: str = "", tool_name: str = "") -> None:
        """Update the panel state. Resolved by name to avoid circular import."""
        from . import state as state_module
        state_module.set_state(state_name, message=message, tool_name=tool_name)

    def _append_assistant_chat(self, content: str) -> None:
        """Append a one-line assistant chat entry. Used by `tool.start`
        to render the per-tool run-log ("⏵ create_primitive(cube TableTop)")
        in the chat so the user sees each step as it dispatches.
        Main-thread only (touches wm collection + area redraw)."""
        try:
            import bpy
            wm = bpy.context.window_manager
            if not hasattr(wm, "animora_chat_history"):
                return
            entry = wm.animora_chat_history.add()
            entry.role = "assistant"
            entry.content = content
            if bpy.context.screen is not None:
                for area in bpy.context.screen.areas:
                    if area.type == "ANIMORA":
                        area.tag_redraw()
        except Exception as exc:
            log.debug("append_assistant_chat failed: %s", exc)

    def _on_first_token_or_continue(self) -> None:
        """A stream_token arrived. Move SUBMITTING/THINKING → STREAMING.
        No-op if we're already STREAMING."""
        from . import state as state_module
        if state_module.state.current != state_module.S.STREAMING:
            state_module.set_state(state_module.S.STREAMING)

    def _on_quality_notice(self, msg: dict) -> None:
        from . import state as state_module
        state_module.set_quality_notice(msg)
        # After the quality check completes, fade to COMPLETE so the
        # panel returns to a clean state.
        state_module.set_state(state_module.S.COMPLETE, "Reviewed.")


# Module-level singleton
client = AnimoraWSClient()


def register() -> None:
    pass


def unregister() -> None:
    client.disconnect()
