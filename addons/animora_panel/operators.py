"""
Animora addon operators.

OT_AnimoraSignIn       — opens browser to auth page (PKCE flow)
OT_AnimoraSignOut      — signs out and clears tokens
OT_AnimoraSendMessage  — sends user message to AI backend
OT_AnimoraStartRecording — starts voice recording (Deepgram)
OT_AnimoraHandleAuthCallback — receives animora:// URL code
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser

import bpy
from bpy.types import Operator

from . import auth, auth_core, deep_link, state, ws_client
from .preferences import get_prefs

log = logging.getLogger("animora.operators")

# Pending-auth state, kept together until the callback returns (Step 1 of
# the device hand-off). Written by OT_AnimoraSignIn, read by the poll timer.
_pending_auth: dict[str, dict[str, float | str]] = {}
_PENDING_AUTH_TIMEOUT_SEC = 180.0


class OT_AnimoraSignIn(Operator):
    bl_idname = "animora.sign_in"
    bl_label = "Sign In"
    bl_description = "Sign in to your Animora account"

    def execute(self, context: bpy.types.Context):
        prefs = get_prefs()

        # Dev-mode shortcut: skip PKCE/browser/Supabase entirely and connect
        # straight to the local dev backend. Real sign-in (Dev Mode off) runs
        # the full Supabase device hand-off below.
        if prefs.dev_mode:
            auth.dev_signin()
            state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
            _connect_ws()
            self.report({"INFO"}, "Connected to local dev backend")
            return {"FINISHED"}

        # Step 1 — generate + keep together until the callback returns.
        code_verifier, code_challenge = auth.generate_pkce()
        signin_state = auth.generate_state()
        _pending_auth[signin_state] = {
            "verifier": code_verifier,
            "started_at": time.monotonic(),
        }

        # A stale callback from an older attempt should never auto-complete a
        # fresh sign-in request.
        deep_link.read_and_consume_callback()
        try:
            deep_link.register_scheme()
        except Exception as exc:
            log.warning("animora:// scheme refresh skipped: %s", exc)

        state.set_auth_status(
            state.AuthS.PENDING_BROWSER,
            "Waiting for browser confirmation",
        )
        device_id = auth.compute_device_fingerprint()
        # Step 2 — open the system browser to the website's sign-in page.
        url = auth_core.build_signin_url(
            prefs.effective_website_base(),
            code_challenge=code_challenge,
            device_id=device_id,
            state=signin_state,
            device_label=auth_core.device_label(),
        )
        log.info("Opening Animora sign-in in the browser")
        webbrowser.open(url)
        self.report({"INFO"}, "Browser opened — complete sign-in, then return here")
        return {"FINISHED"}


class OT_AnimoraHandleAuthCallback(Operator):
    """Manual fallback: complete sign-in from a pasted animora:// callback
    URL. The normal path is the file-drop poll timer (_poll_auth_callback);
    this exists for environments where the OS scheme couldn't be wired."""
    bl_idname = "animora.handle_auth_callback"
    bl_label = "Handle Auth Callback"

    url: bpy.props.StringProperty()  # type: ignore[assignment]

    def execute(self, context: bpy.types.Context):
        parsed = auth_core.parse_callback_url(self.url)
        if not parsed:
            self.report({"ERROR"}, "Not a valid animora://auth/callback URL")
            return {"CANCELLED"}
        code, state = parsed
        if not _complete_auth(code, state):
            self.report({"ERROR"}, "Sign-in failed — please click Sign In again")
            return {"CANCELLED"}
        return {"FINISHED"}


class OT_AnimoraSignOut(Operator):
    bl_idname = "animora.sign_out"
    bl_label = "Sign Out"
    bl_description = "Sign out of Animora"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context: bpy.types.Context):
        ws_client.client.disconnect()
        auth.sign_out()
        state.set_auth_status(state.AuthS.SIGNED_OUT, "")
        self.report({"INFO"}, "Signed out of Animora")
        return {"FINISHED"}


# Re-entrancy guard: set while we programmatically clear the input field so
# the property `update` callback doesn't treat the clear as a new submit.
_suppress_input_update = False


def _send_current_input() -> bool:
    """Send whatever is currently in the input field, then clear it. Shared
    by the SEND button and the Enter/commit path so there is exactly one
    send implementation. Returns True if a message was sent."""
    from . import state as state_module
    wm = bpy.context.window_manager
    text = (getattr(wm, "animora_input_text", "") or "").strip()
    if not text:
        return False
    if not state.auth_can_send() or not ws_client.client.connected:
        log.warning("Send ignored — not connected (sign in first)")
        return False

    prefs = get_prefs()
    ws_client.client.send_message(text, context_flags={
        "share_viewport": prefs.share_viewport,
        "share_scene_graph": prefs.share_scene_graph,
    })

    _append_chat("user", text)
    state_module.set_quality_notice(None)
    assistant_item = wm.animora_chat_history.add()
    assistant_item.role = "assistant"
    assistant_item.content = ""

    state_module.set_state(state_module.S.SUBMITTING, "Sent. Waiting for Animora…")

    def _to_thinking():
        if state_module.state.current == state_module.S.SUBMITTING:
            state_module.set_state(state_module.S.THINKING)
        return None
    bpy.app.timers.register(_to_thinking, first_interval=0.25)

    # Clear the field WITHOUT re-triggering the commit→send callback.
    global _suppress_input_update
    _suppress_input_update = True
    try:
        wm.animora_input_text = ""
    finally:
        _suppress_input_update = False

    for area in (bpy.context.screen.areas if bpy.context.screen else []):
        if area.type == "VIEW_3D":
            area.tag_redraw()
    return True


def _on_input_committed(self, context) -> None:
    """`update` callback on animora_input_text. Fires when the field is
    COMMITTED (the user presses Enter, or clicks away/onto SEND). Sending
    here makes Enter submit (#1) and a single SEND click submit (#2) — the
    first click commits the field, which sends. The actual send is deferred
    to a 0-delay timer because calling the WS / ops from inside a property
    update callback is unsafe."""
    if _suppress_input_update:
        return
    text = (getattr(self, "animora_input_text", "") or "").strip()
    if not text:
        return

    def _deferred():
        try:
            _send_current_input()
        except Exception as exc:  # a timer must never raise
            log.error("Deferred send failed: %s", exc)
        return None
    bpy.app.timers.register(_deferred, first_interval=0.0)


class OT_AnimoraSendMessage(Operator):
    bl_idname = "animora.send_message"
    bl_label = "Send"
    bl_description = "Send your message to Animora (or just press Enter)"

    def execute(self, context: bpy.types.Context):
        if not state.auth_can_send() or not ws_client.client.connected:
            self.report({"WARNING"}, "Not connected — sign in first")
            return {"CANCELLED"}
        # The field's commit callback may have already sent (single click /
        # Enter); if so the field is empty and this is a harmless no-op.
        _send_current_input()
        return {"FINISHED"}


class OT_AnimoraStartRecording(Operator):
    bl_idname = "animora.start_recording"
    bl_label = "Start Voice Recording"
    bl_description = "Record voice and transcribe with Deepgram"

    _recording = False
    _stop_event: threading.Event | None = None

    def execute(self, context: bpy.types.Context):
        self.report({"INFO"}, "Voice recording: coming in Phase 9")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Chat history helpers
# ---------------------------------------------------------------------------

def _append_chat(role: str, content: str) -> None:
    import bpy

    wm = bpy.context.window_manager
    item = wm.animora_chat_history.add()
    item.role = role
    item.content = content


def _connect_ws() -> None:
    prefs = get_prefs()
    import uuid
    session_id = auth.session.user_id or str(uuid.uuid4())
    state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
    ws_client.client.on_stream_token = _on_stream_token
    ws_client.client.on_tool_call = _on_tool_call
    ws_client.client.connect(
        url=prefs.effective_backend_url(),
        session_id=session_id,
        access_token=auth.session.access_token,
    )


def _run_on_main_thread(fn) -> None:
    """Schedule `fn` on Blender's main thread (bpy is not thread-safe)."""
    def _once():
        fn()
        return None  # returning None unregisters this one-shot timer
    bpy.app.timers.register(_once, first_interval=0.0)


def _complete_auth(code: str, callback_state: str) -> bool:
    """Step 3–4: verify state, then exchange the one-time code for a Supabase
    session on a background thread; connect the WS on success. Returns False
    on a state mismatch (possible CSRF) or when no sign-in is pending."""
    global _pending_auth
    pending = _pending_auth.pop(callback_state, {})
    verifier = str(pending.get("verifier", ""))
    if not verifier:
        log.warning("Auth callback with no pending request — ignoring")
        return False
    log.info("Auth callback received for pending state")
    state.set_auth_status(
        state.AuthS.EXCHANGING_CODE,
        "Signing you in",
    )

    def _exchange():
        if auth.exchange_code(code, verifier):
            log.info("Auth code exchange succeeded")
            _run_on_main_thread(_connect_ws)
        else:
            log.error("Auth code exchange failed")
            auth.sign_out()
            message = auth.last_auth_error() or "Sign-in failed. Please try again."
            _run_on_main_thread(
                lambda: state.set_auth_status(
                    state.AuthS.FAILED,
                    message,
                )
            )

    threading.Thread(target=_exchange, daemon=True).start()
    return True


def _poll_auth_callback() -> float:
    """bpy.app.timers tick: pick up an animora:// callback dropped by the
    forwarder and complete sign-in. Cheap no-op when nothing is pending."""
    try:
        if _pending_auth:
            # Expire attempts individually — a stale first attempt must not
            # cancel a fresh second click of "Sign in".
            now = time.monotonic()
            expired = [
                key for key, item in _pending_auth.items()
                if now - float(item.get("started_at", 0.0)) > _PENDING_AUTH_TIMEOUT_SEC
            ]
            for key in expired:
                _pending_auth.pop(key, None)
            if expired and not _pending_auth:
                state.set_auth_status(
                    state.AuthS.FAILED,
                    "Browser confirmation timed out. Click Sign in again.",
                )
                log.warning("Pending auth timed out waiting for callback")
                return 1.0
            url = deep_link.read_and_consume_callback()
            if url:
                log.info("Consumed auth callback drop file")
                parsed = auth_core.parse_callback_url(url)
                if parsed:
                    _complete_auth(*parsed)
                else:
                    log.warning("Ignored malformed animora:// callback")
                    state.set_auth_status(
                        state.AuthS.FAILED,
                        "The browser returned an invalid sign-in callback.",
                    )
    except Exception as exc:  # a timer must never raise
        log.debug("Auth poll tick error: %s", exc)
    return 1.0  # seconds to next tick


# These callbacks run in bundle (recording) mode too: bundle.py signs in via
# auth.dev_signin() + _connect_ws(), and both the send gate (auth_can_send)
# and _draw_bundle_status key off AuthS.CONNECTED.

def _on_ws_connecting() -> None:
    if auth.has_restorable_session() or auth.session.signed_in:
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")


def _on_ws_connected() -> None:
    state.set_auth_status(state.AuthS.CONNECTED, "")


# Timestamp of the last automatic refresh-and-retry after a WS 401/403.
# Guards against a refresh/connect/reject loop when the backend keeps
# rejecting a token the auth stack keeps refreshing successfully.
_last_auth_retry_at: float = 0.0


def _on_ws_auth_rejected(message: str) -> None:
    global _last_auth_retry_at
    log.warning("WS auth rejected: %s", message)
    _pending_auth.clear()

    # The access token may simply have expired (e.g. laptop asleep past the
    # refresh window). Try one silent refresh before discarding the refresh
    # token and forcing a full browser sign-in.
    now = time.monotonic()
    if auth.session.refresh_token and now - _last_auth_retry_at > 60.0:
        _last_auth_retry_at = now
        state.set_auth_status(state.AuthS.CONNECTING, "Refreshing your session")
        if auth.restore_session_async(
            on_ready=lambda: _run_on_main_thread(_connect_ws),
            on_invalid=lambda: _run_on_main_thread(_restore_session_invalid),
        ):
            return

    auth.sign_out()
    state.set_auth_status(state.AuthS.FAILED, message or "Session expired — please sign in again.")


def _on_ws_transport_disconnected(message: str) -> None:
    if auth.session.signed_in or auth.has_restorable_session():
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
    if message:
        log.warning("WS transport disconnected: %s", message)


def _restore_session_invalid() -> None:
    if auth.last_refresh_rejected():
        state.set_auth_status(state.AuthS.FAILED, "Session expired — please sign in again.")
    else:
        state.set_auth_status(
            state.AuthS.FAILED,
            "Couldn't reach Animora — check your connection, then sign in to retry.",
        )


def _configure_ws_callbacks() -> None:
    ws_client.client.on_connecting = _on_ws_connecting
    ws_client.client.on_connected = _on_ws_connected
    ws_client.client.on_auth_rejected = _on_ws_auth_rejected
    ws_client.client.on_transport_disconnected = _on_ws_transport_disconnected
    ws_client.client.token_provider = lambda: auth.session.access_token


def _app_version() -> str:
    import importlib
    try:
        pkg = importlib.import_module(__package__ or "animora_panel")
        return ".".join(str(x) for x in getattr(pkg, "bl_info", {}).get("version", (1, 0, 0)))
    except Exception:
        return "1.0.0"


class OT_AnimoraFeedback(Operator):
    bl_idname = "animora.feedback"
    bl_label = "Send Feedback"
    bl_description = "Open the Animora feedback page in your browser"

    def execute(self, context: bpy.types.Context):
        prefs = get_prefs()
        url = auth_core.feedback_url(prefs.effective_website_base(), _app_version())
        webbrowser.open(url)
        self.report({"INFO"}, "Opened Animora feedback in your browser")
        return {"FINISHED"}


def _on_stream_token(token: str) -> None:
    """Append the new token to the latest assistant turn. The empty
    assistant item is pre-allocated by OT_AnimoraSendMessage so the
    panel can show the streaming-cursor placeholder during THINKING."""
    wm = bpy.context.window_manager
    history = wm.animora_chat_history
    if not history or history[-1].role != "assistant":
        item = history.add()
        item.role = "assistant"
        item.content = token
    else:
        history[-1].content += token
    # Tag the ANIMORA area for redraw on EVERY token — that's what makes
    # the streaming visible. The state.py tick handles dot animation.
    if bpy.context.screen is not None:
        for area in bpy.context.screen.areas:
            if area.type == "ANIMORA":
                area.tag_redraw()


def _on_tool_call(msg: dict) -> None:
    tool_name = msg.get("tool")
    tool_input = msg.get("input", {})
    tool_use_id = msg.get("tool_use_id", "")
    iteration = msg.get("iteration", 0)
    user_intent = msg.get("user_intent", "")

    # Post-mortem fix — make the viewport render-responsive ONCE per
    # session, before the first build step. Enables background shader
    # compilation (so EEVEE doesn't freeze the main thread compiling
    # materials) and flips to MATERIAL_PREVIEW while the scene is still
    # empty (instant switch; later materials then compile incrementally
    # off the main thread). This is what fixes the "compiling EEVEE
    # shaders" hang.
    _ensure_render_responsive()

    # Sprint 4E — Per-iteration undo grouping. Push ONE
    # `bpy.ops.ed.undo_push` per new (session, iteration) tuple. All
    # subsequent atomic tools within that iteration roll into the same
    # undo entry (Blender behavior: a single push before N data
    # mutations means Ctrl-Z rolls back to before the push). The user
    # gets "Undo Animora: <intent>" in their menu and one Ctrl-Z drops
    # the whole agent step at once.
    _maybe_push_iteration_undo(iteration, user_intent)

    # ── Escape-hatch code execution ────────────────────────────────────
    # Live name is `execute_animora_code` (Sprint 4F — kills the "blender"
    # leak in the model's narration). Older names accepted for back-compat
    # with in-flight scripts: `execute_blender_code` (Sprint 4D pivot),
    # `execute_blender_script` (pre-pivot).
    if tool_name in (
        "execute_animora_code",
        "execute_blender_code",
        "execute_blender_script",
    ):
        _execute_script(
            tool_use_id,
            tool_input.get("script", ""),
            tool_input.get("intent_summary", ""),
        )
        return

    # ── Atomic ops (Sprint 4D — MCP pivot) ──────────────────────────────
    # Each handler returns immediately, posts a one-line confirmation to
    # the chat, sends a tool_result with the scene_graph diff, and tags
    # the viewport for redraw. No long-running paths — the LLM composes
    # builds from many of these so the user sees geometry appear live.
    atomic_handlers = {
        "get_scene_info":      _atomic_get_scene_info,
        "viewport_screenshot": _atomic_viewport_screenshot,
        "create_primitive":    _atomic_create_primitive,
        "create_light":        _atomic_create_light,
        "create_camera":       _atomic_create_camera,
        "set_transform":       _atomic_set_transform,
        "add_modifier":        _atomic_add_modifier,
        "apply_material":      _atomic_apply_material,
        "set_parent":          _atomic_set_parent,
        "delete_object":       _atomic_delete_object,
        "duplicate_object":    _atomic_duplicate_object,
        "set_world":           _atomic_set_world,
    }
    if tool_name in atomic_handlers:
        atomic_handlers[tool_name](tool_use_id, tool_input)
        return

    # ── Existing supporting tools ───────────────────────────────────────
    if tool_name == "get_object_info":
        _get_object_info(tool_use_id, tool_input.get("name", ""))
    elif tool_name == "render_preview":
        _render(tool_use_id, samples=32, label="preview")
    elif tool_name == "render_final":
        _render(tool_use_id, samples=256, label="final")
    elif tool_name == "suggest_next_steps":
        _show_suggested_steps(tool_input.get("steps", []))
    elif tool_name == "load_asset":
        # Sprint 3B: backend has already fetched the asset to a local
        # path; we just apply it to the active scene per its kind.
        _load_asset(
            tool_use_id,
            asset_id=tool_input.get("asset_id", ""),
            kind=tool_input.get("kind", ""),
            local_path=tool_input.get("local_path", ""),
            name=tool_input.get("name", ""),
            target=tool_input.get("target", ""),
        )
    else:
        log.warning("Unknown tool call: %s", tool_name)
        # Send an explicit error tool_result so the backend's agentic
        # loop doesn't hang waiting for a response that will never come.
        _send_tool_result({
            "tool_use_id": tool_use_id,
            "output": "",
            "error": f"Unknown tool: {tool_name}",
        })


def _find_view3d_context() -> dict | None:
    """Find a 3D viewport area + its WINDOW region so we can override the
    context when running scripts from a timer callback.

    Without this override, `bpy.ops.mesh.primitive_cube_add()` and most
    other bpy operators silently fail their `poll()` check because the
    context's area is None or the ANIMORA editor (not VIEW_3D). This is
    THE single most common reason LLM-generated scripts "do nothing"."""
    import bpy
    if bpy.context.screen is None:
        return None
    for area in bpy.context.screen.areas:
        if area.type != "VIEW_3D":
            continue
        # Need a WINDOW region — that's the actual 3D viewport region,
        # not the toolbar/header/sidebar regions.
        window_region = next(
            (r for r in area.regions if r.type == "WINDOW"), None
        )
        if window_region is None:
            continue
        space = next(
            (s for s in area.spaces if s.type == "VIEW_3D"), None
        )
        return {
            "window": bpy.context.window,
            "screen": bpy.context.screen,
            "area": area,
            "region": window_region,
            "space_data": space,
            "scene": bpy.context.scene,
        }
    return None


def _build_exec_namespace() -> dict:
    """Build the namespace the script runs inside. Seeded with the
    modules the master prompt expects the script to use (bpy + the
    handful of pure-Python helpers in the BANNED_IMPORTS whitelist)."""
    import math
    import random
    import bpy
    import bmesh
    import mathutils
    return {
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        "math": math,
        "random": random,
        # Keep __builtins__ — Python sets it automatically anyway, and
        # quality_enforcer already blocks the dangerous ones at the
        # static-analysis layer.
    }


def _post_to_chat(role: str, content: str) -> None:
    """Append a system-flavored note to the visible chat history so the
    user sees what happened. Uses role='assistant' because it renders
    in the Animora-styled message box; we differentiate via the message
    prefix (✓/✗/⚠)."""
    import bpy
    wm = bpy.context.window_manager
    if not hasattr(wm, "animora_chat_history"):
        return
    item = wm.animora_chat_history.add()
    item.role = role
    item.content = content
    if bpy.context.screen is not None:
        for area in bpy.context.screen.areas:
            if area.type == "ANIMORA":
                area.tag_redraw()


# Sprint 4E — Per-iteration undo state. Tracks the most recent
# (iteration_index) we've already pushed an undo entry for, so that
# subsequent tool_calls in the SAME iteration don't add new entries.
# Reset to -1 on session restart / addon reload.
_last_iteration_undo_pushed: int = -1

# Post-mortem fix — one-time-per-session viewport responsiveness setup.
# Set once we've enabled background shader compilation + switched to
# MATERIAL_PREVIEW. Cleared on addon reload (module re-import).
_render_responsive_done: bool = False


def _ensure_render_responsive() -> None:
    """Show material colors WITHOUT triggering EEVEE shader compilation.

    The "compiling EEVEE shaders → Animora not responding" hang is
    EEVEE compiling material shaders on Blender's main thread whenever
    the viewport is in MATERIAL_PREVIEW or RENDERED mode. We sidestep
    it completely:

      • Keep the viewport in SOLID mode (no EEVEE, no shader compile,
        never freezes).
      • Set SOLID mode's color source to 'MATERIAL' so it shows each
        material's flat viewport-display color (`material.diffuse_color`,
        which `_atomic_apply_material` now writes from the base color).

    SOLID + color='MATERIAL' reads a flat per-material color property —
    instant, no compile, no GPU shader work. The user sees colored
    geometry instead of grey, and the viewport stays responsive no
    matter how many materials the build creates. Full PBR (roughness,
    metallic, lighting) is still there for when the user hits F12 or
    switches to Rendered themselves.

    Idempotent: guarded by `_render_responsive_done`, runs at most once
    between addon reloads. Never raises into the caller.
    """
    global _render_responsive_done
    if _render_responsive_done:
        return
    _render_responsive_done = True

    import bpy

    try:
        if bpy.context.screen is None:
            # No screen yet (early in startup) — un-set the guard so we
            # retry on the next tool_call when the screen exists.
            _render_responsive_done = False
            return
        switched = False
        for area in bpy.context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                shading = space.shading
                # Only adjust SOLID — never override an explicit
                # RENDERED / MATERIAL / WIREFRAME choice the user made.
                if shading.type == "SOLID":
                    try:
                        shading.color_type = "MATERIAL"
                        switched = True
                    except (AttributeError, TypeError):
                        pass
        # Report the FINAL viewport state via print() so it actually
        # shows in the Blender system console (the addon's log.info is
        # suppressed — Blender only surfaces print() + WARNING+).
        states = []
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        sh = space.shading
                        states.append(
                            f"type={sh.type} "
                            f"color_type={getattr(sh, 'color_type', 'N/A')}"
                        )
        print(f"[ANIMORA DIAG] render_responsive: switched={switched} "
              f"viewport_shading=[{'; '.join(states) or '(no VIEW_3D)'}]")
        _force_viewport_redraw()
    except Exception as exc:
        print(f"[ANIMORA DIAG] render_responsive failed: {exc}")


def log_material_diagnostic() -> None:
    """Ground-truth material diagnostic — logs, for every mesh in the
    scene, whether it has a material and what its viewport display color
    is. This is THE answer to 'why is the build grey':

      • mesh with materials=[] (or all None)  → materials NOT applied
        (the model skipped apply_material, or the rescue didn't fire).
      • mesh with a material whose diffuse_color is greyish
        (R≈G≈B, mid value)                    → grey COLOR was chosen.
      • mesh with a vivid diffuse_color but viewport still grey
        → the SOLID color_type switch didn't take (see
          render_responsive log above).

    Called once at turn_complete. Uses print() so it ALWAYS shows in the
    Blender system console — the addon's log.info is suppressed (Blender
    only surfaces print() + WARNING+ to the console).
    """
    try:
        import bpy
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        if not meshes:
            print("[ANIMORA DIAG] material_diagnostic: no mesh objects in scene")
            return
        n_with = 0
        n_without = 0
        sample_lines: list[str] = []
        for o in meshes:
            mats = [m for m in (o.data.materials or []) if m is not None]
            if mats:
                n_with += 1
                m = mats[0]
                dc = tuple(round(c, 3) for c in m.diffuse_color)
                # Also pull the Principled BSDF Base Color node value, in
                # case diffuse_color (viewport display) wasn't set but the
                # node was — that pinpoints exactly which write happened.
                base = None
                try:
                    if m.use_nodes:
                        bsdf = m.node_tree.nodes.get("Principled BSDF")
                        if bsdf is not None:
                            base = tuple(round(c, 3)
                                         for c in bsdf.inputs["Base Color"].default_value)
                except Exception:
                    pass
                r, g, b = dc[0], dc[1], dc[2]
                greyish = (max(r, g, b) - min(r, g, b)) < 0.06
                if len(sample_lines) < 8:
                    sample_lines.append(
                        f"{o.name}→{m.name} diffuse={dc} base={base}"
                        f"{' [GREYISH]' if greyish else ''}"
                    )
            else:
                n_without += 1
                if len(sample_lines) < 8:
                    sample_lines.append(f"{o.name}→NO MATERIAL")
        print(
            f"[ANIMORA DIAG] material_diagnostic: {n_with}/{len(meshes)} "
            f"meshes have a material, {n_without} have NONE.\n"
            f"[ANIMORA DIAG]   " + "\n[ANIMORA DIAG]   ".join(sample_lines)
        )
    except Exception as exc:
        print(f"[ANIMORA DIAG] material_diagnostic failed: {exc}")


def _maybe_push_iteration_undo(iteration: int, user_intent: str) -> None:
    """Push exactly ONE bpy.ops.ed.undo_push per new agent iteration.

    Blender's undo system records a snapshot at each push; a single push
    before N data mutations means a single Ctrl-Z rolls back all of
    them. Calling this once per iteration (rather than once per atomic
    tool) gives the user "Undo Animora: build a wooden chair" as a
    single menu entry instead of 8 separate "Undo apply_material" steps.
    """
    global _last_iteration_undo_pushed
    try:
        idx = int(iteration)
    except (TypeError, ValueError):
        return
    if idx == _last_iteration_undo_pushed:
        return  # already pushed for this iteration
    import bpy
    label = (user_intent or "agent step").strip().replace("\n", " ")[:60] or "agent step"
    try:
        bpy.ops.ed.undo_push(message=f"Animora: {label}")
    except Exception as exc:
        log.debug("undo_push failed (%s) — continuing", exc)
    _last_iteration_undo_pushed = idx


# ── Sprint 1 Deep — Batched deferred-cleanup drain ────────────────────
# On a hero turn (e.g., the v17 chair example with ~22 atomic calls in
# iteration 1) the previous "one bpy.app.timers.register per tool" path
# pushed 22+ callbacks onto the main thread in rapid succession. Each
# callback did: append a chat history entry → tag_redraw ANIMORA area
# → vision.end_exec_pause. tag_redraw forces panel.py's draw() to run,
# and panel.py iterates the full wm.animora_chat_history (which grew
# to 50+ entries during the turn). The compounded O(N) per-redraw cost
# manifested as a 30-60s main-thread freeze — looked like a hang.
#
# Fix: a single shared drain timer that batches every queued cleanup
# per tick, appends ALL chat entries in one Python loop, calls
# `tag_redraw` exactly once at the end, and balances the vision
# exec-pause counter for each queued entry. Callers append to
# `_pending_cleanups` instead of registering their own timer.

# Queue of {"chat_line": str, "balance_exec_pause": bool, "force_redraw": bool}.
# Strings only — no closures held — so the queue is cheap to drain.
_pending_cleanups: list[dict] = []
_drain_timer_registered: bool = False

# Per-turn cap on tool-success chat lines (the ✓ entries). When 22
# atomic tools fire in one iteration the chat becomes noise; cap at
# this many displayed ✓ lines per turn and emit a single
# "… N more steps completed" summary at the end. Error chat lines are
# never capped — failures always show.
#
# Sprint 1.x: raised from 8 → 16 so the user can see both the
# iteration 0 blockout AND iteration 1 material/parent phase land in
# the chat. The cofounder's couch test showed 8 lines all from iter 0
# (legs being placed) and 14 hidden behind "… N more build steps
# completed", which hid the entire material-apply phase from view.
# 16 covers a typical hero turn (10-14 blockout + 4-6 material/parent).
_TOOL_RESULT_CHAT_CAP_PER_TURN = 16
_tool_result_chat_count_this_turn: int = 0
_tool_result_chat_suppressed_count: int = 0


def reset_per_turn_chat_caps() -> None:
    """Called by ws_client at the start of a new user_message so the per-turn
    chat caps reset. Idempotent — safe to call when no turn is active."""
    global _tool_result_chat_count_this_turn, _tool_result_chat_suppressed_count
    _tool_result_chat_count_this_turn = 0
    _tool_result_chat_suppressed_count = 0


def _enqueue_cleanup(
    *,
    chat_line: str = "",
    balance_exec_pause: bool = False,
    force_redraw: bool = True,
) -> None:
    """Append one cleanup entry to the drain queue and ensure the drain
    timer is registered. The drain timer runs on the main thread and
    processes ALL queued entries in a single tick."""
    global _drain_timer_registered, _tool_result_chat_count_this_turn
    global _tool_result_chat_suppressed_count
    # Per-turn cap on ✓ tool-success lines: count and either keep or
    # bucket-into-summary.
    if chat_line and chat_line.startswith("✓"):
        if _tool_result_chat_count_this_turn >= _TOOL_RESULT_CHAT_CAP_PER_TURN:
            _tool_result_chat_suppressed_count += 1
            chat_line = ""  # drop the per-tool line; the summary will surface at end-of-turn
        else:
            _tool_result_chat_count_this_turn += 1
    _pending_cleanups.append({
        "chat_line": chat_line,
        "balance_exec_pause": balance_exec_pause,
        "force_redraw": force_redraw,
    })
    if not _drain_timer_registered:
        try:
            import bpy
            bpy.app.timers.register(_drain_cleanups, first_interval=0.0)
            _drain_timer_registered = True
        except Exception as exc:
            # If the timer won't register, drain inline so we still ship
            # the cleanup (loses the latency win, but stays correct).
            log.debug("drain.timer.register_failed: %s", exc)
            _drain_cleanups()


def _drain_cleanups():
    """Single main-thread tick: process every queued cleanup entry,
    coalesce the panel redraw to ONE tag_redraw at the end, and
    balance the vision exec-pause counter. Returns None to drop the
    timer registration when the queue is empty."""
    global _drain_timer_registered, _tool_result_chat_suppressed_count
    if not _pending_cleanups:
        _drain_timer_registered = False
        return None

    # Snapshot + clear so callers can enqueue more while we process.
    pending = _pending_cleanups.copy()
    _pending_cleanups.clear()

    try:
        import bpy
        from . import vision
        wm = bpy.context.window_manager
        has_chat = hasattr(wm, "animora_chat_history")
        chat_added = 0
        needs_redraw = False
        exec_pauses_to_balance = 0

        for entry in pending:
            line = entry.get("chat_line") or ""
            if line and has_chat:
                try:
                    item = wm.animora_chat_history.add()
                    item.role = "assistant"
                    item.content = line
                    chat_added += 1
                except Exception as exc:
                    log.debug("drain.chat_append failed: %s", exc)
            if entry.get("force_redraw"):
                needs_redraw = True
            if entry.get("balance_exec_pause"):
                exec_pauses_to_balance += 1

        # ONE tag_redraw at the end — covers every panel append this batch.
        if needs_redraw or chat_added:
            try:
                if bpy.context.screen is not None:
                    for area in bpy.context.screen.areas:
                        if area.type in ("ANIMORA", "VIEW_3D"):
                            area.tag_redraw()
            except Exception as exc:
                log.debug("drain.tag_redraw failed: %s", exc)

        # Balance the vision exec-pause counter — once per queued entry
        # that incremented it on the critical path.
        for _ in range(exec_pauses_to_balance):
            try:
                vision.end_exec_pause()
            except Exception:
                pass
    except Exception as exc:
        log.warning("drain.tick.crashed: %s", exc)

    # If more entries arrived during processing, keep the timer alive.
    if _pending_cleanups:
        return 0.0
    _drain_timer_registered = False
    return None


def flush_turn_end_chat_summary() -> None:
    """Called at end-of-turn (by main.py via WS turn_complete handler in
    ws_client.py) to surface the suppressed tool-result count if we hit
    the per-turn cap. Cheap; idempotent."""
    global _tool_result_chat_suppressed_count
    if _tool_result_chat_suppressed_count > 0:
        _enqueue_cleanup(
            chat_line=f"… {_tool_result_chat_suppressed_count} more build steps completed.",
            balance_exec_pause=False,
            force_redraw=True,
        )
        _tool_result_chat_suppressed_count = 0


def _force_viewport_redraw() -> None:
    """Update the depsgraph + redraw every 3D viewport so the just-made
    changes are immediately visible. Without this, edits via the data
    API may not show until the user clicks somewhere."""
    import bpy
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    if bpy.context.screen is None:
        return
    for area in bpy.context.screen.areas:
        if area.type in ("VIEW_3D", "ANIMORA", "PROPERTIES", "OUTLINER"):
            area.tag_redraw()


def _execute_script(tool_use_id: str, script: str, intent_summary: str = "") -> None:
    """Run a sandboxed bpy script with proper viewport context override.

    AST-split execution (Sprint 4D):
        The script is parsed into top-level statements; each statement
        runs in its own bpy.app.timers callback. Between callbacks the
        main thread returns to Blender's event loop — the viewport
        redraws, the UI stays responsive, and the panel can update its
        "step N of M" indicator. The cofounder's session feedback was
        "the viewport feels dead during execution, Animora goes
        unresponsive" — this is the fix. Sharing one mutable namespace
        across statements preserves the exec-as-one-script semantics
        (variables defined in step 0 visible in step 5, etc).

    Operational requirements still addressed:
      1. `bpy.context.temp_override(area=VIEW_3D, region=WINDOW)` — without
         this, bpy.ops.* operators that require a viewport silently
         fail poll(). Override is re-entered per statement.
      2. Errors visible in the chat — surfaced as an assistant message
         with the failing-statement source so the user knows what broke.
      3. Forced viewport redraw + view_layer.update() between statements
         so geometry appears live as it's built (not after the whole
         script is done).
      4. Guaranteed tool_result send — the backend awaits this; any path
         that fails to send leaves the loop stuck in EXECUTING forever.
    """
    import ast
    import bpy
    import time as _time
    from . import state as state_module, vision

    _exec_started = _time.monotonic()
    label = intent_summary.strip() or "AI script"
    log.info("execute_animora_code start: %s (%d chars)", label, len(script))

    # Sprint 4E — undo grouping is now handled by
    # `_maybe_push_iteration_undo` upstream in `_on_tool_call`. One
    # push per iteration covers the whole agent step; calling it again
    # here would create a second undo entry that Ctrl-Z would have to
    # traverse separately. Keep the redundant push commented for the
    # legacy/standalone path (selftest) where iteration isn't dispatched.

    # Sprint 4G — skip the pre-script scene_graph serialize entirely.
    # The model already has the scene_graph (depsgraph-update push), and
    # the post-script scene_diff is informational. On dense scenes this
    # call alone takes 50-300 ms per script invocation — pure overhead
    # on the critical path between the LLM dispatch and the user seeing
    # geometry land in the viewport.
    pre_graph: dict = {}

    view3d_ctx = _find_view3d_context()
    result: dict = {"tool_use_id": tool_use_id, "output": "", "error": ""}

    if view3d_ctx is None:
        err = (
            "No 3D Viewport in the current workspace. Switch to the "
            "Layout workspace (top tab) so the script has a viewport "
            "to operate on, then try again."
        )
        log.warning("execute_blender_script: %s", err)
        result["error"] = err
        _post_to_chat("assistant", f"✗ {err}")
        _send_tool_result(result)
        return

    # Parse + compile per top-level statement so we can run them on
    # successive timer ticks. A single SyntaxError fails the whole script
    # — there's no useful partial run.
    try:
        tree = ast.parse(script, filename=f"<animora:{tool_use_id}>", mode="exec")
    except SyntaxError as exc:
        err = f"Syntax error in generated script: {exc.msg} (line {exc.lineno})"
        result["error"] = err
        log.error(err)
        _post_to_chat("assistant", f"✗ {err}\n\nThe AI's generated script didn't parse. Try rephrasing.")
        _send_tool_result(result)
        return

    # Empty script — model emitted a tool_use with no body. Surface
    # explicitly so the backend retry path can correct.
    if not tree.body:
        err = "Script was empty — the model emitted execute_blender_script with no statements."
        result["error"] = err
        log.warning(err)
        _send_tool_result(result)
        return

    # Compile each top-level statement independently. Each compilation
    # carries the original line numbers so tracebacks point back into
    # the model's script (not "<unknown:1>"), which keeps the model's
    # error-recovery prompts accurate.
    compiled_steps: list = []
    for node in tree.body:
        try:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.copy_location(mod, node)
            compiled_steps.append(compile(mod, f"<animora:{tool_use_id}>", "exec"))
        except SyntaxError as exc:
            err = f"Compile error at line {getattr(node, 'lineno', '?')}: {exc.msg}"
            result["error"] = err
            log.error(err)
            _send_tool_result(result)
            return

    ns = _build_exec_namespace()
    total_steps = len(compiled_steps)
    log.info("execute_blender_script: %d top-level statements queued (%s)", total_steps, label)

    # Immediate visible signal in the chat — the panel state pill is
    # easy to miss, but a chat line catches the eye. Posted BEFORE the
    # first tick so the user sees confirmation that Animora is starting
    # to build, even if step 1 happens to be slow.
    _post_to_chat("assistant", f"⏺ Building: {label} ({total_steps} step{'s' if total_steps != 1 else ''})")

    state_module.mark_exec_started()
    # Sprint 4F — pause depsgraph-driven viewport stream for the whole
    # script. Each statement's tag_redraw + bpy.data mutation would
    # otherwise fire a synchronous offscreen capture; on a 24-statement
    # script that's 24 captures racing the main thread alongside the
    # statements themselves. Resumed in _finalize_success / _finalize_failure.
    vision.begin_exec_pause()
    # Initial "step 1/N" surface so the panel shows progress immediately.
    try:
        state_module.set_state(
            state_module.S.EXECUTING,
            f"{label} — step 1/{total_steps}",
            tool_name="execute_animora_code",
        )
    except Exception:
        pass

    runner = _ScriptRunner(
        tool_use_id=tool_use_id,
        label=label,
        compiled_steps=compiled_steps,
        namespace=ns,
        view3d_ctx=view3d_ctx,
        pre_graph=pre_graph,
        result=result,
    )
    runner.start()


def _send_tool_result(result: dict) -> None:
    """One-shot send of the addon's tool_result back to the backend.
    Centralised so failures land in one log line, not three."""
    try:
        ws_client.client.send_json({"type": "tool_result", **result})
    except Exception as send_exc:
        log.error("tool_result send failed: %s", send_exc)


class _ScriptRunner:
    """Drives statement-by-statement execution via bpy.app.timers.

    One instance per execute_blender_script call. Lives only as long as
    needed to drain the compiled_steps list. The timer callback returns
    a positive float to be re-scheduled with that delay, or None when
    the runner is done (drops the registration).
    """

    # Tick interval between statements. 0.0 is "as soon as possible"
    # but Blender batches timers at frame boundaries; 0.001 gives the
    # event loop a clean tick to redraw the viewport before the next
    # statement runs without adding perceptible latency.
    _TICK_INTERVAL = 0.001

    def __init__(
        self,
        *,
        tool_use_id: str,
        label: str,
        compiled_steps: list,
        namespace: dict,
        view3d_ctx: dict,
        pre_graph: dict,
        result: dict,
    ) -> None:
        self.tool_use_id = tool_use_id
        self.label = label
        self.compiled_steps = compiled_steps
        self.namespace = namespace
        self.view3d_ctx = view3d_ctx
        self.pre_graph = pre_graph
        self.result = result
        self.total = len(compiled_steps)
        self.index = 0
        self._done = False

    def start(self) -> None:
        import bpy
        bpy.app.timers.register(self._tick, first_interval=0.0)

    def _tick(self):
        """Run ONE statement, redraw viewport, schedule the next tick.

        Returning a float schedules another tick; returning None drops
        the registration. Any exception inside this callback aborts the
        whole script (we don't try to recover mid-run — partial scripts
        leave the scene in a wedged state)."""
        import bpy
        import traceback
        from . import state as state_module

        if self._done:
            return None

        if self.index >= self.total:
            self._finalize_success()
            return None

        step = self.compiled_steps[self.index]
        step_num = self.index + 1
        # Update the panel BEFORE running, so the user sees current
        # progress while the (possibly slow) statement runs.
        try:
            state_module.set_state(
                state_module.S.EXECUTING,
                f"{self.label} — step {step_num}/{self.total}",
                tool_name="execute_blender_script",
            )
        except Exception:
            pass

        import time as _time
        step_started = _time.monotonic()
        try:
            with bpy.context.temp_override(**self.view3d_ctx):
                exec(step, self.namespace)  # noqa: S102
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            err = f"{type(exc).__name__}: {exc} (step {step_num}/{self.total})"
            self.result["error"] = err + "\n\n" + tb
            log.error("execute_blender_script error at step %d/%d: %s\n%s",
                      step_num, self.total, err, tb)
            _post_to_chat(
                "assistant",
                f"✗ Script failed at step {step_num}/{self.total}: {err}\n\n"
                f"Animora's script ran partially — you can ask me to try again "
                f"with a different approach.",
            )
            self._finalize_failure()
            return None
        # Surface long-running statements to logs — useful diagnostic
        # for "which step is actually slow" when the panel feels frozen.
        # bpy.ops.* operators occasionally take seconds even for simple
        # primitives on first call; this just flags it for our awareness.
        step_elapsed = _time.monotonic() - step_started
        if step_elapsed > 2.0:
            log.warning(
                "execute_blender_script: slow step %d/%d took %.2fs (%s)",
                step_num, self.total, step_elapsed, self.label,
            )

        # Redraw between statements so geometry appears live, not in
        # one frozen burst at the end. Skip view_layer.update() inside
        # the loop — it's expensive on dense scenes and is implicitly
        # triggered by tag_redraw. We do call it once in finalize().
        try:
            if bpy.context.screen is not None:
                for area in bpy.context.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
        except Exception:
            pass

        self.index += 1
        return self._TICK_INTERVAL

    def _finalize_success(self) -> None:
        import bpy
        from . import state as state_module, vision

        import time as _time
        finalize_started = _time.monotonic()

        self._done = True
        self.result["output"] = str(self.namespace.get("_result", "OK"))
        log.info("execute_animora_code ok: %s (%d steps)", self.label, self.total)

        state_module.mark_exec_finished()
        # Sprint 4F — counterpart to begin_exec_pause in _execute_script.
        try:
            vision.end_exec_pause()
        except Exception:
            pass

        # Sprint 4G — SEND THE TOOL_RESULT FIRST, before any of the
        # potentially-expensive cleanup work below. The backend's
        # coordinator is ticking down a 45 s timeout; the model can
        # start composing the next iteration the instant we send. The
        # diff / HD capture / chat post are nice-to-have UX; the backend
        # has the model-facing fields (output, tool_use_id) already so
        # nothing else is load-bearing for the loop to advance.
        _send_tool_result(self.result)
        try:
            state_module.set_state(state_module.S.COMPLETE, self.label)
        except Exception:
            pass

        # ── Off-critical-path cleanup (deferred to a fresh timer tick) ──
        # Scene-graph serialize, scene_diff compute, chat post, HD
        # capture. None of these are needed for the next iteration; the
        # model gets a fresh scene_graph via the depsgraph-update push.
        # On dense scenes the scene_graph serialize alone has been
        # observed at 200-500 ms — pushing it off the critical path
        # avoids tripping the 45 s coordinator timeout when several
        # tools chain together.
        result_ref = self.result
        pre_graph = self.pre_graph
        label_ref = self.label

        def _deferred_finalize():
            try:
                _force_viewport_redraw()
            except Exception as exc:
                log.debug("deferred.force_redraw failed: %s", exc)
            try:
                post_graph = vision.serialize_scene_graph()
                diff = _scene_graph_diff_brief(pre_graph, post_graph)
            except Exception as exc:
                log.debug("deferred.serialize failed: %s", exc)
                diff = {"error": f"serialize_failed: {exc}", "added": [], "removed": []}
            added = diff.get("added", [])
            removed = diff.get("removed", [])
            bits: list[str] = []
            if added:
                names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in added]
                names = [n for n in names if n]
                if names:
                    bits.append(f"added {', '.join(names[:4])}{'…' if len(names) > 4 else ''}")
            if removed:
                bits.append(f"removed {', '.join(removed[:4])}{'…' if len(removed) > 4 else ''}")
            try:
                if bits:
                    _post_to_chat("assistant", f"✓ {label_ref} — {'; '.join(bits)}.")
            except Exception as exc:
                log.debug("deferred.chat_post failed: %s", exc)
            # HD capture — used only by the backend's optional artist's-
            # eye check. The result already shipped; this lands in a
            # follow-up frame the orchestrator stitches onto its next
            # context if it's awaiting one. Best-effort.
            try:
                captured = vision.capture_post_script_hd_bytes()
                if captured is not None:
                    jpeg_bytes, media_type = captured
                    import base64 as _b64
                    result_ref["hd_capture_b64"] = _b64.b64encode(jpeg_bytes).decode()
                    result_ref["hd_media_type"] = media_type
            except Exception as exc:
                log.debug("deferred.hd_capture failed: %s", exc)
            return None

        try:
            bpy.app.timers.register(_deferred_finalize, first_interval=0.0)
        except Exception as exc:
            log.debug("deferred.timer.register_failed: %s", exc)

        finalize_ms = int((_time.monotonic() - finalize_started) * 1000)
        log.info("execute_animora_code finalize critical_path_ms=%d", finalize_ms)

    def _finalize_failure(self) -> None:
        from . import state as state_module, vision
        self._done = True
        state_module.mark_exec_finished()
        # Sprint 4F — counterpart to begin_exec_pause in _execute_script.
        try:
            vision.end_exec_pause()
        except Exception:
            pass
        _send_tool_result(self.result)
        try:
            state_module.set_state(state_module.S.ERROR, self.result.get("error", "")[:100])
        except Exception:
            pass


def _scene_graph_diff_brief(pre: dict, post: dict) -> dict:
    """Compact, value-aware diff for the tool_result payload.

    Phase 9 (2026-05-22): expanded from names-only to include enough
    field values for the LLM to reason about WHAT changed across loop
    iterations (per master prompt rule #18). The model now sees:
      - added: list of {name, type, location, modifiers, materials}
      - removed: list of names
      - modified: list of {name, fields_changed: {field: (before, after)}}
    capped at ~3 KB total (sentinel `(truncated)` if bigger).
    """
    pre_by = {o["name"]: o for o in pre.get("objects", [])}
    post_by = {o["name"]: o for o in post.get("objects", [])}

    pre_names = set(pre_by.keys())
    post_names = set(post_by.keys())

    def _round_vec(v, ndigits=3):
        if not isinstance(v, (list, tuple)):
            return v
        return [round(float(c), ndigits) for c in v]

    def _summarise_added(obj: dict) -> dict:
        out = {
            "name": obj.get("name", ""),
            "type": obj.get("type", ""),
            "location": _round_vec(obj.get("location") or [0, 0, 0]),
        }
        mods = obj.get("modifiers") or []
        if mods:
            out["modifiers"] = [
                {"name": m.get("name", ""), "type": m.get("type", "")}
                for m in mods[:6]
            ]
        mats = obj.get("materials") or []
        if mats:
            out["materials"] = [m if isinstance(m, str) else m.get("name", "")
                                for m in mats[:6]]
        return out

    def _diff_obj(a: dict, b: dict) -> dict:
        """Return {field: (before, after)} for fields that differ."""
        changes: dict = {}
        for key in ("location", "rotation_euler", "scale"):
            av, bv = _round_vec(a.get(key)), _round_vec(b.get(key))
            if av != bv:
                changes[key] = [av, bv]
        a_mods = [(m.get("name", ""), m.get("type", "")) for m in (a.get("modifiers") or [])]
        b_mods = [(m.get("name", ""), m.get("type", "")) for m in (b.get("modifiers") or [])]
        if a_mods != b_mods:
            changes["modifiers"] = {
                "before": [{"name": n, "type": t} for n, t in a_mods[:6]],
                "after":  [{"name": n, "type": t} for n, t in b_mods[:6]],
            }
        a_mats = sorted(m if isinstance(m, str) else m.get("name", "") for m in (a.get("materials") or []))
        b_mats = sorted(m if isinstance(m, str) else m.get("name", "") for m in (b.get("materials") or []))
        if a_mats != b_mats:
            changes["materials"] = {"before": a_mats[:6], "after": b_mats[:6]}
        if a.get("parent") != b.get("parent"):
            changes["parent"] = [a.get("parent"), b.get("parent")]
        return changes

    added_items = [_summarise_added(post_by[n]) for n in sorted(post_names - pre_names)]
    removed_items = sorted(pre_names - post_names)

    modified_items: list[dict] = []
    for n in sorted(pre_names & post_names):
        ch = _diff_obj(pre_by[n], post_by[n])
        if ch:
            modified_items.append({"name": n, "fields_changed": ch})

    diff = {
        "added": added_items,
        "removed": removed_items,
        "modified": modified_items[:24],  # cap detail-bearing list
        "object_count_before": len(pre_names),
        "object_count_after": len(post_names),
    }

    # Soft size cap. If the diff serialises to > 3 KB, replace the
    # detail-bearing arrays with name-only fallbacks. The LLM still gets
    # the names + counts; runaway scenes don't bloat the tool_result.
    import json
    serialised = json.dumps(diff, default=str)
    if len(serialised) > 3000:
        diff["added"] = [item.get("name", "") for item in added_items][:24]
        diff["modified"] = [m["name"] for m in modified_items][:24]
        diff["_truncated"] = "diff exceeded 3 KB; using names only"
    return diff


def _get_object_info(tool_use_id: str, name: str) -> None:
    import bpy
    obj = bpy.data.objects.get(name)
    if obj is None:
        ws_client.client.send_json({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "output": "",
            "error": f"Object '{name}' not found",
        })
        return
    # Stage 1 — primitive 2 (read_object). Material slot names are
    # included so the model can verify "did I apply a material to this
    # object" with a single tool call. Without this, checking
    # materials requires a full serialize_scene_graph walk. Empty list
    # means the object has no material slots (mesh / curve / etc.) or
    # no data block (empty, light, camera).
    materials: list[str] = []
    if obj.data is not None and hasattr(obj.data, "materials"):
        try:
            materials = [
                m.name if m is not None else ""
                for m in obj.data.materials
            ]
        except Exception:
            materials = []
    info = {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "parent": obj.parent.name if obj.parent else None,
        "modifiers": [{"type": m.type, "name": m.name} for m in obj.modifiers],
        "materials": materials,
        "vertex_count": len(obj.data.vertices) if obj.data and hasattr(obj.data, "vertices") else None,
    }
    ws_client.client.send_json({
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "output": str(info),
        "error": "",
    })


def _render(tool_use_id: str, samples: int, label: str) -> None:
    """Trigger a Cycles render. `samples` controls quality (32 = preview,
    256+ = final). The render runs asynchronously — completion is reported
    via the render_complete handler which sends an HD capture."""
    import bpy
    try:
        bpy.context.scene.render.engine = "CYCLES"
        bpy.context.scene.cycles.samples = samples
        bpy.context.scene.cycles.use_denoising = True
        # INVOKE_DEFAULT so the render runs in a non-blocking modal context
        bpy.ops.render.render("INVOKE_DEFAULT", write_still=False)
        ws_client.client.send_json({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "output": f"{label} render started ({samples} samples)",
            "error": "",
        })
    except Exception as exc:
        ws_client.client.send_json({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "output": "",
            "error": f"{label} render failed: {exc}",
        })


def _show_suggested_steps(steps: list) -> None:
    """Stash the suggested-steps list on the WindowManager so the panel
    can render them as one-click chips. (UI integration is Phase 8 — for
    now we just log + store.)"""
    import bpy
    wm = bpy.context.window_manager
    if not hasattr(wm, "animora_suggested_steps"):
        return  # property not registered yet
    try:
        wm.animora_suggested_steps.clear()
        for s in steps[:5]:
            item = wm.animora_suggested_steps.add()
            item.text = s
    except Exception as exc:
        log.debug("Failed to store suggested steps: %s", exc)
    log.info("AI suggested next steps: %s", steps)


# ---------------------------------------------------------------------------
# Sprint 3B — asset loader (Quality Plan §6.6)
# ---------------------------------------------------------------------------
# The backend has already fetched the asset to a local cache; this
# handler applies it to the active scene per its kind:
#   - HDRI → world environment texture
#   - texture → Principled BSDF material on the named target object
#   - mesh → wm.append the .blend's first object into the active collection

def _load_asset(
    tool_use_id: str,
    *,
    asset_id: str,
    kind: str,
    local_path: str,
    name: str = "",
    target: str = "",
) -> None:
    """Apply a fetched PolyHaven asset to the current Blender scene.

    Always sends a tool_result back to the orchestrator, even on
    failure — the agentic loop awaits this to advance.
    """
    import bpy
    import os
    log.info("load_asset.start id=%s kind=%s path=%s", asset_id, kind, local_path)

    def _send_result(ok: bool, output: str, error: str = "") -> None:
        result: dict[str, object] = {"tool_use_id": tool_use_id}
        if ok:
            result["output"] = output
        else:
            result["is_error"] = True
            result["output"] = ""
            result["error"] = error
        try:
            ws_client.client.send_json({"type": "tool_result", **result})
        except Exception as exc:
            log.error("load_asset: send_json failed: %s", exc)

    if not local_path or not os.path.isfile(local_path):
        msg = f"Asset local_path missing or not a file: {local_path!r}"
        log.warning(msg)
        _send_result(False, "", msg)
        return

    def _apply() -> None:
        try:
            if kind == "hdri":
                _apply_hdri(local_path, name or asset_id)
                _send_result(True, f"Applied HDRI '{name or asset_id}' as world environment.")
            elif kind == "texture":
                _apply_texture(local_path, target=target, asset_name=name or asset_id)
                _send_result(True, f"Applied texture '{name or asset_id}'" + (f" to '{target}'" if target else "."))
            elif kind == "mesh":
                _apply_mesh(local_path, target=target, asset_name=name or asset_id)
                _send_result(True, f"Linked mesh '{name or asset_id}' into the active collection.")
            else:
                _send_result(False, "", f"Unknown asset kind: {kind!r}")
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            log.error("load_asset apply failed: %s\n%s", exc, tb)
            _send_result(False, "", f"Apply failed: {type(exc).__name__}: {exc}")
        finally:
            _force_viewport_redraw()

    bpy.app.timers.register(_apply, first_interval=0.0)


def _apply_hdri(local_path: str, display_name: str) -> None:
    """Set the world's background to an HDRI environment texture.
    Creates the world if missing, replaces the existing environment
    texture node if present, otherwise inserts a new one."""
    import bpy
    scene = bpy.context.scene
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new(f"AnimoraWorld_{display_name}")
        scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    # Find / create Background node
    bg = next((n for n in nt.nodes if n.type == "BACKGROUND"), None)
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
    # Find / create Environment Texture node
    env = next((n for n in nt.nodes if n.type == "TEX_ENVIRONMENT"), None)
    if env is None:
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.location = (bg.location.x - 300, bg.location.y)
    # Load the .hdr image
    img = bpy.data.images.load(local_path, check_existing=True)
    env.image = img
    # Wire env → bg → world output
    out_node = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None)
    if out_node is None:
        out_node = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out_node.inputs["Surface"])
    log.info("hdri applied: %s", local_path)


def _apply_texture(local_path: str, *, target: str, asset_name: str) -> None:
    """Apply a PolyHaven texture .blend's PBR material to the target
    object. PolyHaven texture blends contain a pre-set-up material
    named after the texture id. We link it from the .blend and assign
    it to the target. If no target named, applies to the active obj."""
    import bpy
    # PolyHaven texture .blend filename: <id>_<res>.blend
    # The material inside is typically named after the slug. We
    # discover it by listing materials at link-time.
    with bpy.data.libraries.load(local_path, link=False) as (data_from, data_to):
        if data_from.materials:
            data_to.materials = data_from.materials[:1]  # take the first material
        else:
            raise RuntimeError(f"No material found inside texture blend {local_path}")
    new_mat = data_to.materials[0]
    if new_mat is None:
        raise RuntimeError(f"Failed to link material from {local_path}")
    new_mat.name = f"PH_{asset_name}"

    # Resolve the target object: explicit name, else active object
    obj = None
    if target:
        obj = bpy.data.objects.get(target)
    if obj is None:
        obj = bpy.context.active_object
    if obj is None or obj.type != "MESH":
        log.warning("texture applied but no MESH object to bind to (target=%r)", target)
        return  # material exists, just not assigned

    if obj.data.materials:
        obj.data.materials[0] = new_mat
    else:
        obj.data.materials.append(new_mat)
    log.info("texture applied to %s: %s", obj.name, local_path)


def _apply_mesh(local_path: str, *, target: str, asset_name: str) -> None:
    """Link the first mesh object from the PolyHaven .blend into the
    active collection. Optional `target` is a comma-separated 'x,y,z'
    location override; otherwise the object lands at its native origin
    or (0,0,0) if missing."""
    import bpy
    with bpy.data.libraries.load(local_path, link=False) as (data_from, data_to):
        if data_from.objects:
            data_to.objects = data_from.objects[:]
        else:
            raise RuntimeError(f"No objects found inside mesh blend {local_path}")
    coll = bpy.context.collection
    placed: list = []
    for obj in data_to.objects:
        if obj is None:
            continue
        coll.objects.link(obj)
        placed.append(obj)

    if not placed:
        raise RuntimeError(f"Mesh load yielded no usable objects from {local_path}")

    # Optional location override "x,y,z"
    if target and "," in target:
        try:
            parts = [float(p.strip()) for p in target.split(",")]
            if len(parts) == 3:
                placed[0].location = parts
        except ValueError:
            log.debug("ignoring invalid target=%r for mesh placement", target)

    # Select + activate the first linked object so the user / next
    # tool_call lands on it
    bpy.ops.object.select_all(action="DESELECT")
    placed[0].select_set(True)
    bpy.context.view_layer.objects.active = placed[0]
    log.info("mesh linked: %s (%d objects)", local_path, len(placed))


# ---------------------------------------------------------------------------
# Sprint 4D — MCP-style atomic op handlers
# ---------------------------------------------------------------------------
# Each handler is the addon-side implementation of one atomic tool
# defined in ai-backend/orchestrator/tools.py. Contract for every
# handler:
#   • Pull typed params from `tool_input`; fall back to safe defaults.
#   • Run bpy ops inside a `bpy.context.temp_override(VIEW_3D)` so the
#     operator's poll() doesn't fail when invoked from a timer.
#   • Post a one-line confirmation to the chat (`✓ Created Cube_1`).
#   • Send a tool_result with output + scene_diff (per-op delta).
#   • tag_redraw VIEW_3D so the change is visible immediately.
# Failures send an is_error tool_result so the orchestrator's coordinator
# resolves cleanly instead of timing out at 180s. Never raise into the
# addon's timer / WS callback paths.


def _atomic_run(tool_use_id: str, label: str, fn) -> None:
    """Wrap an atomic-op body with the MINIMUM boilerplate the contract
    requires, send the tool_result IMMEDIATELY after the bpy mutation,
    and defer every other side-effect (chat post, viewport redraw,
    scene-graph serialize, scene_diff) to an off-critical-path timer.

    Sprint 4G — root cause analysis of the cofounder's 100s-late
    tool_result symptom:

      The previous `_atomic_run` did, in order, on the main thread:
        (1) vision.serialize_scene_graph()  — pre   (full bpy.data walk)
        (2) fn(view3d_ctx)                  — the actual bpy mutation
        (3) bpy.context.view_layer.update() — depsgraph recompute
        (4) tag_redraw() × all VIEW_3D      — redraw kick
        (5) vision.serialize_scene_graph()  — post  (full bpy.data walk)
        (6) _scene_graph_diff_brief()       — compute diff
        (7) _post_to_chat(✓)                — wm.animora_chat_history.add
        (8) _force_viewport_redraw()        — another view_layer.update
        (9) _send_tool_result(result)       — finally enqueue WS frame

      Items 1, 5, 8 each walk the entire scene + recompute the depsgraph.
      Item 7 mutates a Blender property collection (triggers panel
      redraw of the chat history). On any non-trivial scene + chat
      history these stack into the 100s wallclock we measured. The
      backend's 45 s coordinator timeout fires THREE TIMES before
      iteration 0's tool_result even lands.

    Fix: items 1, 5, 6 are dropped entirely (the model already has the
    live scene_graph from the depsgraph-update push). Items 7 and 8 are
    deferred to a `bpy.app.timers.register` callback that runs AFTER
    the tool_result has been queued. Result: critical path is
    `fn() + view_layer.update() + tag_redraw() + send_tool_result()` —
    typically <30 ms for a simple primitive on any modern machine.

    `fn(view3d_ctx)` body contract:
      • Returns `(short_summary, output_payload)` — 2-tuple — for tools
        that don't create a uniquely named datablock (set_world etc.).
      • Returns `(short_summary, output_payload, ("domain", "name"))` —
        3-tuple — when the body created or modified a named datablock.
      `domain` is the `bpy.data.<domain>` collection name (`objects`,
      `materials`, `lights`, `cameras`, `meshes`, `worlds`).
    """
    import bpy
    import time as _time
    import traceback
    from . import vision

    result = {"tool_use_id": tool_use_id, "output": "", "error": ""}

    view3d_ctx = _find_view3d_context()
    if view3d_ctx is None:
        result["error"] = (
            "No 3D Viewport in the current workspace. Switch to the "
            "Layout workspace and try again."
        )
        _send_tool_result(result)
        return

    started = _time.monotonic()
    # Pause depsgraph-driven viewport captures while we mutate. Resumed
    # in the deferred cleanup below so the post-tool capture lands on the
    # next depsgraph tick once we're off the critical path.
    vision.begin_exec_pause()

    summary: str = ""
    expected: tuple[str, str] | None = None
    try:
        with bpy.context.temp_override(**view3d_ctx):
            rv = fn(view3d_ctx)
            if isinstance(rv, tuple) and len(rv) == 3:
                summary, payload, expected = rv  # type: ignore[misc]
            else:
                summary, payload = rv  # type: ignore[misc]
                payload = payload  # noqa
            # The ONE depsgraph update we keep on the critical path —
            # required so the next tool's `bpy.data` lookup reads the
            # mutation we just made. tag_redraw is cheap (no actual
            # GL work; just marks the area dirty for the next event-
            # loop tick).
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            try:
                if bpy.context.screen is not None:
                    for area in bpy.context.screen.areas:
                        if area.type == "VIEW_3D":
                            area.tag_redraw()
            except Exception:
                pass

        # Presence-verify INLINE — cheap (one dict lookup), catches the
        # silent "tool said OK, scene unchanged" failure mode.
        if expected is not None:
            domain, name = expected
            coll = getattr(bpy.data, domain, None)
            if coll is None or coll.get(name) is None:
                hint = (
                    f"{label} reported success but `bpy.data.{domain}['{name}']` "
                    f"is missing. The operator's poll() likely failed silently — "
                    f"check that the active workspace has a 3D Viewport."
                )
                result["error"] = hint
                log.warning("atomic.%s.presence_missing: %s", label, hint)
                _send_tool_result(result)
                vision.end_exec_pause()
                return

        result["output"] = payload if isinstance(payload, str) else str(payload)

        # ── CRITICAL PATH ENDS HERE — send the tool_result NOW ────────
        body_ms = int((_time.monotonic() - started) * 1000)
        log.info("atomic.%s.done body_ms=%d (tool_use_id=%s)",
                 label, body_ms, tool_use_id)
        _send_tool_result(result)

    except Exception as exc:
        tb = traceback.format_exc(limit=4)
        err = f"{type(exc).__name__}: {exc}"
        result["error"] = err
        log.warning("atomic.%s.failed: %s\n%s", label, err, tb)
        _send_tool_result(result)
        vision.end_exec_pause()
        return

    # ── Deferred off-critical-path cleanup ─────────────────────────────
    # Sprint 1 Deep: replaced "one timer per tool" with a single batched
    # drain queue. _enqueue_cleanup appends the chat line + signals the
    # vision exec-pause balance, and ensures the shared drain timer is
    # registered. On a 22-tool hero turn this collapses 22 main-thread
    # timer callbacks + 22 panel redraws into ONE drain pass with ONE
    # final tag_redraw — fixing the 30-60s freeze the cofounder hit.
    chat_summary = summary or label
    _enqueue_cleanup(
        chat_line=f"✓ {chat_summary}" if chat_summary else "",
        balance_exec_pause=True,
        force_redraw=True,
    )


# ── Inspect ────────────────────────────────────────────────────────────


def _atomic_get_scene_info(tool_use_id: str, tool_input: dict) -> None:
    from . import vision
    try:
        graph = vision.serialize_scene_graph()
        # The model gets a compact JSON-stringified snapshot. Cap the
        # objects list at 50 to bound the payload — hero scenes can
        # have hundreds, but the model only needs the top of the list.
        objs = graph.get("objects", [])
        graph["objects"] = objs[:50]
        if len(objs) > 50:
            graph["_object_truncation"] = f"showed first 50 of {len(objs)} objects"
        import json as _json
        out = _json.dumps(graph, default=str)[:6000]
        _send_tool_result({"tool_use_id": tool_use_id, "output": out, "error": ""})
    except Exception as exc:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"get_scene_info failed: {exc}"})


def _atomic_viewport_screenshot(tool_use_id: str, tool_input: dict) -> None:
    from . import vision
    try:
        captured = vision.capture_post_script_hd_bytes()
        result: dict = {"tool_use_id": tool_use_id, "output": "viewport captured", "error": ""}
        if captured is not None:
            jpeg_bytes, media_type = captured
            import base64 as _b64
            result["hd_capture_b64"] = _b64.b64encode(jpeg_bytes).decode()
            result["hd_media_type"] = media_type
        _send_tool_result(result)
    except Exception as exc:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"viewport_screenshot failed: {exc}"})


# ── Create ─────────────────────────────────────────────────────────────


# Sprint 4E — create handlers now use bpy.data (+ bmesh for primitive
# geometry) instead of bpy.ops.* operators. The bpy.ops route silently
# fails its poll() check when called outside a 3D viewport context (e.g.
# from a timer callback running in the Sculpting workspace), producing
# the "tool said OK but nothing's in the scene" failure mode the
# cofounder reported. The bpy.data route is context-free + atomic.


def _build_primitive_mesh(kind: str, name: str):
    """Return a fresh `bpy.types.Mesh` populated with the named
    primitive's vertex/face data. Uses bmesh.ops for tessellation —
    same shapes as bpy.ops.mesh.primitive_*_add, without the operator's
    context dependency. Torus falls back to a deferred bpy.ops call
    inside a temp_override (bmesh has no torus generator).
    """
    import bpy
    import bmesh
    mesh = bpy.data.meshes.new(name=f"{name}_Mesh")
    bm = bmesh.new()
    try:
        if kind == "cube":
            bmesh.ops.create_cube(bm, size=2.0)
        elif kind == "sphere":
            bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
        elif kind == "ico_sphere":
            bmesh.ops.create_icosphere(bm, subdivisions=2, radius=1.0)
        elif kind == "cylinder":
            bmesh.ops.create_cone(
                bm, segments=32, radius1=1.0, radius2=1.0, depth=2.0,
                cap_ends=True, cap_tris=False,
            )
        elif kind == "cone":
            bmesh.ops.create_cone(
                bm, segments=32, radius1=1.0, radius2=0.0, depth=2.0,
                cap_ends=True, cap_tris=False,
            )
        elif kind == "plane":
            bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
        else:
            raise ValueError(f"_build_primitive_mesh: unsupported kind {kind!r}")
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return mesh


def _atomic_create_primitive(tool_use_id: str, tool_input: dict) -> None:
    """Create a primitive using bpy.data + bmesh (no bpy.ops). Torus is
    handled separately because bmesh has no torus generator.

    Returns the named object via the presence-verify contract so the
    addon catches silent failures."""
    import bpy
    kind = str(tool_input.get("kind", "")).lower()
    name = str(tool_input.get("name", "")).strip() or kind.title()
    location = tuple(tool_input.get("location") or (0, 0, 0))[:3]
    rotation = tuple(tool_input.get("rotation") or (0, 0, 0))[:3]
    scale = tuple(tool_input.get("scale") or (1, 1, 1))[:3]

    if kind not in {"cube", "sphere", "ico_sphere", "cylinder", "cone",
                    "torus", "plane"}:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Unknown primitive kind: {kind!r}"})
        return

    def body(_ctx):
        if kind == "torus":
            # bmesh has no torus generator; fall back to bpy.ops (we
            # already hold a VIEW_3D temp_override so poll passes).
            bpy.ops.mesh.primitive_torus_add(location=location, rotation=rotation)
            obj = bpy.context.active_object
            if obj is not None:
                obj.name = name
                obj.scale = scale
        else:
            mesh = _build_primitive_mesh(kind, name)
            obj = bpy.data.objects.new(name, mesh)
            obj.location = location
            obj.rotation_euler = rotation
            obj.scale = scale
            # Link into the active collection so it's visible in the scene.
            (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
        return (
            f"Created {kind} '{name}' at {tuple(round(v, 2) for v in location)}",
            f"name={name}",
            ("objects", name),
        )

    _atomic_run(tool_use_id, f"create_primitive({kind})", body)


def _atomic_create_light(tool_use_id: str, tool_input: dict) -> None:
    """Create a light using bpy.data only — no bpy.ops, no poll() risk."""
    import bpy
    kind = str(tool_input.get("kind", "")).upper()
    name = str(tool_input.get("name", "")).strip() or f"{kind.title()}Light"
    location = tuple(tool_input.get("location") or (0, 0, 5))[:3]
    rotation = tuple(tool_input.get("rotation") or (0, 0, 0))[:3]
    energy = float(tool_input.get("energy", 1000))
    color = tuple(tool_input.get("color") or (1.0, 1.0, 1.0))[:3]
    size = float(tool_input.get("size", 1.0))

    if kind not in ("SUN", "POINT", "SPOT", "AREA"):
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Unknown light kind: {kind!r}"})
        return

    def body(_ctx):
        light_data = bpy.data.lights.new(name=f"{name}_Data", type=kind)
        light_data.energy = energy
        light_data.color = color
        if kind == "AREA":
            light_data.size = size
        obj = bpy.data.objects.new(name, light_data)
        obj.location = location
        obj.rotation_euler = rotation
        (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
        return (
            f"Added {kind.lower()} light '{name}' (energy={energy})",
            f"name={name}",
            ("objects", name),
        )

    _atomic_run(tool_use_id, f"create_light({kind.lower()})", body)


def _atomic_create_camera(tool_use_id: str, tool_input: dict) -> None:
    """Create a camera using bpy.data only."""
    import bpy
    name = str(tool_input.get("name", "")).strip() or "Camera"
    location = tuple(tool_input.get("location") or (7, -7, 5))[:3]
    rotation = tuple(tool_input.get("rotation") or (1.1, 0, 0.78))[:3]
    focal_length = float(tool_input.get("focal_length", 50))
    set_active = bool(tool_input.get("set_active", True))

    def body(_ctx):
        cam_data = bpy.data.cameras.new(name=f"{name}_Data")
        cam_data.lens = focal_length
        obj = bpy.data.objects.new(name, cam_data)
        obj.location = location
        obj.rotation_euler = rotation
        (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
        if set_active:
            bpy.context.scene.camera = obj
        return (
            f"Added camera '{name}' (lens={focal_length}mm)",
            f"name={name}",
            ("objects", name),
        )

    _atomic_run(tool_use_id, "create_camera", body)


# ── Modify ─────────────────────────────────────────────────────────────


def _atomic_set_transform(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    name = str(tool_input.get("name", "")).strip()
    if not name:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": "set_transform requires `name`"})
        return

    obj = bpy.data.objects.get(name)
    if obj is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Object '{name}' not found"})
        return

    location = tool_input.get("location")
    rotation = tool_input.get("rotation")
    scale = tool_input.get("scale")

    def body(_ctx):
        changed = []
        if location is not None:
            obj.location = tuple(location)[:3]
            changed.append("location")
        if rotation is not None:
            obj.rotation_euler = tuple(rotation)[:3]
            changed.append("rotation")
        if scale is not None:
            obj.scale = tuple(scale)[:3]
            changed.append("scale")
        return f"Updated {name} ({', '.join(changed) or 'no-op'})", f"name={name}"

    _atomic_run(tool_use_id, "set_transform", body)


def _atomic_add_modifier(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    target = str(tool_input.get("object", "")).strip()
    kind = str(tool_input.get("kind", "")).lower()
    params = tool_input.get("params") or {}

    if not target or not kind:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": "add_modifier requires `object` and `kind`"})
        return

    obj = bpy.data.objects.get(target)
    if obj is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Object '{target}' not found"})
        return

    # bpy modifier type names are upper-case with underscores
    bpy_type = {
        "bevel": "BEVEL",
        "subdivision_surface": "SUBSURF",
        "array": "ARRAY",
        "mirror": "MIRROR",
        "solidify": "SOLIDIFY",
        "decimate": "DECIMATE",
        "screw": "SCREW",
        "wireframe": "WIREFRAME",
    }.get(kind)
    if bpy_type is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Unknown modifier kind: {kind!r}"})
        return

    def body(_ctx):
        mod = obj.modifiers.new(name=kind.title(), type=bpy_type)
        # Apply kind-specific params. Unknown params are silently
        # ignored — the JSON schema is the source of truth for what's
        # supported; ignoring extras keeps this forward-compatible.
        if bpy_type == "BEVEL":
            if "width" in params: mod.width = float(params["width"])
            if "segments" in params: mod.segments = int(params["segments"])
        elif bpy_type == "SUBSURF":
            if "levels" in params: mod.levels = int(params["levels"])
            if "render_levels" in params: mod.render_levels = int(params["render_levels"])
        elif bpy_type == "ARRAY":
            if "count" in params: mod.count = int(params["count"])
            if "relative_offset" in params:
                ro = tuple(params["relative_offset"])[:3]
                mod.relative_offset_displace = ro
        elif bpy_type == "MIRROR":
            if "axis" in params:
                ax = params["axis"]
                if isinstance(ax, str):
                    mod.use_axis = [ax.lower() == "x", ax.lower() == "y", ax.lower() == "z"]
                elif isinstance(ax, (list, tuple)) and len(ax) >= 3:
                    mod.use_axis = [bool(a) for a in ax[:3]]
        elif bpy_type == "SOLIDIFY":
            if "thickness" in params: mod.thickness = float(params["thickness"])
        elif bpy_type == "DECIMATE":
            if "ratio" in params: mod.ratio = float(params["ratio"])
        elif bpy_type == "SCREW":
            if "axis" in params: mod.axis = params["axis"]
            if "angle" in params: mod.angle = float(params["angle"])
            if "steps" in params: mod.steps = int(params["steps"])
        elif bpy_type == "WIREFRAME":
            if "thickness" in params: mod.thickness = float(params["thickness"])
        return f"Added {kind} modifier to {target}", f"object={target}"

    _atomic_run(tool_use_id, f"add_modifier({kind})", body)


def _atomic_apply_material(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    target = str(tool_input.get("object", "")).strip()
    mat_name = str(tool_input.get("name", "")).strip() or f"Mat_{target}"
    base_color = tuple(tool_input.get("base_color") or (0.8, 0.8, 0.8, 1.0))[:4]
    if len(base_color) == 3:
        base_color = (*base_color, 1.0)
    roughness = float(tool_input.get("roughness", 0.5))
    metallic = float(tool_input.get("metallic", 0.0))
    emission = tool_input.get("emission")
    emission_strength = float(tool_input.get("emission_strength", 0.0))
    alpha = float(tool_input.get("alpha", 1.0))

    if not target:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": "apply_material requires `object`"})
        return

    obj = bpy.data.objects.get(target)
    if obj is None or obj.type != "MESH":
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Object '{target}' is not a mesh"})
        return

    def body(_ctx):
        mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = base_color
            bsdf.inputs["Roughness"].default_value = roughness
            bsdf.inputs["Metallic"].default_value = metallic
            if "Alpha" in bsdf.inputs:
                bsdf.inputs["Alpha"].default_value = alpha
            if emission is not None and "Emission Color" in bsdf.inputs:
                em = tuple(emission)[:4]
                if len(em) == 3: em = (*em, 1.0)
                bsdf.inputs["Emission Color"].default_value = em
                if "Emission Strength" in bsdf.inputs:
                    bsdf.inputs["Emission Strength"].default_value = emission_strength
        # Post-mortem fix — set the material's VIEWPORT DISPLAY color
        # (diffuse_color) to match the base color. SOLID shading mode
        # with color_type='MATERIAL' reads this flat property directly
        # and shows the color with ZERO EEVEE shader compilation. This
        # is how the user sees colors without the "compiling EEVEE
        # shaders" hang — we never need MATERIAL_PREVIEW / RENDERED.
        # Roughness/metallic on diffuse_color don't matter; it's a flat
        # solid-mode swatch. The full PBR values still live on the BSDF
        # for when the user renders.
        try:
            mat.diffuse_color = base_color
        except Exception:
            pass
        # Ensure target's first material slot is this material
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
        return (
            f"Applied material '{mat_name}' to {target}",
            f"object={target}",
            ("materials", mat_name),
        )

    _atomic_run(tool_use_id, "apply_material", body)


def _atomic_set_parent(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    child_name = str(tool_input.get("child", "")).strip()
    parent_name = str(tool_input.get("parent", "")).strip()
    keep_transform = bool(tool_input.get("keep_transform", True))

    child = bpy.data.objects.get(child_name)
    parent = bpy.data.objects.get(parent_name)
    if child is None or parent is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"set_parent: missing child={child_name!r} or parent={parent_name!r}"})
        return

    def body(_ctx):
        if keep_transform:
            world_matrix = child.matrix_world.copy()
            child.parent = parent
            child.matrix_world = world_matrix
        else:
            child.parent = parent
        return f"Parented {child_name} → {parent_name}", f"child={child_name}"

    _atomic_run(tool_use_id, "set_parent", body)


def _atomic_delete_object(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    name = str(tool_input.get("name", "")).strip()
    obj = bpy.data.objects.get(name)
    if obj is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Object '{name}' not found"})
        return

    def body(_ctx):
        bpy.data.objects.remove(obj, do_unlink=True)
        return f"Deleted {name}", f"name={name}"

    _atomic_run(tool_use_id, "delete_object", body)


def _atomic_duplicate_object(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    source_name = str(tool_input.get("source", "")).strip()
    new_name = str(tool_input.get("new_name", "")).strip() or f"{source_name}_dup"
    offset = tuple(tool_input.get("location_offset") or (0, 0, 0))[:3]

    src = bpy.data.objects.get(source_name)
    if src is None:
        _send_tool_result({"tool_use_id": tool_use_id, "output": "",
                            "error": f"Source '{source_name}' not found"})
        return

    def body(_ctx):
        new_obj = src.copy()
        if src.data is not None:
            new_obj.data = src.data  # linked-mesh duplicate
        new_obj.name = new_name
        new_obj.location = (src.location[0] + offset[0],
                             src.location[1] + offset[1],
                             src.location[2] + offset[2])
        (bpy.context.collection or bpy.context.scene.collection).objects.link(new_obj)
        return (
            f"Duplicated {source_name} → {new_name}",
            f"name={new_name}",
            ("objects", new_name),
        )

    _atomic_run(tool_use_id, "duplicate_object", body)


# ── Environment ────────────────────────────────────────────────────────


def _atomic_set_world(tool_use_id: str, tool_input: dict) -> None:
    import bpy
    color = tool_input.get("color")
    strength = tool_input.get("strength")

    def body(_ctx):
        scene = bpy.context.scene
        world = scene.world or bpy.data.worlds.new("World")
        scene.world = world
        world.use_nodes = True
        nt = world.node_tree
        bg = next((n for n in nt.nodes if n.type == "BACKGROUND"), None)
        if bg is None:
            bg = nt.nodes.new("ShaderNodeBackground")
        changes = []
        if color is not None:
            rgb = tuple(color)[:3]
            bg.inputs["Color"].default_value = (*rgb, 1.0)
            changes.append("color")
        if strength is not None:
            bg.inputs["Strength"].default_value = float(strength)
            changes.append("strength")
        return f"Updated world ({', '.join(changes) or 'no-op'})", "world"

    _atomic_run(tool_use_id, "set_world", body)


# ---------------------------------------------------------------------------
# New conversation — clear chat history
# ---------------------------------------------------------------------------

class OT_AnimoraNewConversation(Operator):
    bl_idname = "animora.new_conversation"
    bl_label = "New Conversation"
    bl_description = "Start a fresh chat (current history is cleared locally)"

    def execute(self, context):
        from . import state as state_module
        wm = context.window_manager
        wm.animora_chat_history.clear()
        wm.animora_chat_index = 0
        wm.animora_input_text = ""
        state_module.reset()
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({"INFO"}, "Started a new conversation")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Suggested-prompt button — fills the input with the prompt and sends
# ---------------------------------------------------------------------------

class OT_AnimoraSendSuggested(Operator):
    bl_idname = "animora.send_suggested"
    bl_label = "Send Suggested Prompt"
    bl_description = "Use this suggested prompt"

    prompt: bpy.props.StringProperty()  # type: ignore[assignment]

    def execute(self, context):
        wm = context.window_manager
        wm.animora_input_text = self.prompt
        if ws_client.client.connected:
            return bpy.ops.animora.send_message()
        # Not connected — just stage the prompt
        _append_chat("user", self.prompt)
        wm.animora_input_text = ""
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# File attachment placeholder
# ---------------------------------------------------------------------------

class OT_AnimoraAttachFile(Operator):
    bl_idname = "animora.attach_file"
    bl_label = "Attach File"
    bl_description = "Attach a reference image or model to the conversation"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")  # type: ignore[assignment]

    def execute(self, context):
        if self.filepath:
            self.report({"INFO"}, f"Attached: {self.filepath}")
            _append_chat("user", f"[attached: {self.filepath}]")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ---------------------------------------------------------------------------
# Header buttons — show history, show settings
# ---------------------------------------------------------------------------

class OT_AnimoraShowHistory(Operator):
    bl_idname = "animora.show_history"
    bl_label = "Conversation History"
    bl_description = "View past conversations with Animora"

    def execute(self, context):
        self.report({"INFO"}, "History panel — coming in a future update")
        return {"FINISHED"}


class OT_AnimoraShowSettings(Operator):
    bl_idname = "animora.show_settings"
    bl_label = "Animora Settings"
    bl_description = "Open Animora preferences"

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module="animora_panel")
        except Exception:
            self.report({"INFO"}, "Settings — open Preferences > Add-ons > Animora")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# API key management — paste, validate, clear
# ---------------------------------------------------------------------------

class OT_AnimoraSaveApiKey(Operator):
    bl_idname = "animora.save_api_key"
    bl_label = "Save API Key"
    bl_description = "Store the pasted Anthropic API key in your OS keyring"

    def execute(self, context):
        from . import credentials
        prefs = get_prefs()
        key = (prefs.pending_api_key or "").strip()
        if not key:
            self.report({"WARNING"}, "Paste your Anthropic API key first")
            return {"CANCELLED"}
        if not key.startswith("sk-ant-"):
            self.report(
                {"WARNING"},
                "Doesn't look like an Anthropic key — should start with 'sk-ant-'",
            )
            return {"CANCELLED"}

        credentials.set_api_key(key)
        prefs.pending_api_key = ""  # never display it back
        self.report({"INFO"}, f"API key saved ({credentials.status_message()})")

        # Reconnect WS so the new key takes effect immediately if a session
        # is already running.
        try:
            from . import ws_client as ws
            if ws.client.connected:
                ws.client.disconnect()
                # Panel will trigger reconnect on next user action
        except Exception:
            pass
        return {"FINISHED"}


class OT_AnimoraTestConnection(Operator):
    bl_idname = "animora.test_connection"
    bl_label = "Test Connection"
    bl_description = "Ping Claude with the stored key and verify the connection works"

    def execute(self, context):
        from . import api_validator, credentials
        from .preferences import connection_status

        key = credentials.get_api_key()
        if not key:
            self.report({"WARNING"}, "No API key saved. Paste a key and click Save first.")
            return {"CANCELLED"}

        prefs = get_prefs()
        connection_status.state = "testing"
        connection_status.message = "Calling Anthropic via backend…"

        def _on_result(result):
            import time as _time
            if result.ok:
                connection_status.state = "ok"
                connection_status.message = (
                    f"Connected. Pinged {result.model_pinged} in {result.elapsed_ms} ms."
                )
                connection_status.last_ok_at = _time.time()
            else:
                connection_status.state = "failed"
                pretty = {
                    "invalid_key": "Anthropic rejected this API key.",
                    "rate_limited": "Anthropic rate limit hit — try again in a minute.",
                    "timeout": "Anthropic took too long to respond.",
                    "network": "Couldn't reach the Animora backend.",
                    "bad_format": "Key format is wrong.",
                }.get(result.error_code, result.error_message or "Connection failed.")
                connection_status.message = pretty
            # Force the preferences window to redraw
            for area in bpy.context.screen.areas:
                if area.type == "PREFERENCES":
                    area.tag_redraw()

        api_validator.validate_async(
            backend_url=prefs.effective_backend_http_url(),
            api_key=key,
            on_result=_on_result,
        )
        self.report({"INFO"}, "Testing connection…")
        return {"FINISHED"}


class OT_AnimoraClearApiKey(Operator):
    bl_idname = "animora.clear_api_key"
    bl_label = "Remove API Key"
    bl_description = "Delete the stored Anthropic API key from your OS keyring"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from . import credentials
        from .preferences import connection_status
        credentials.clear_api_key()
        connection_status.state = "unknown"
        connection_status.message = ""
        self.report({"INFO"}, "API key removed.")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Stream interrupt — user pressed stop on an in-flight response
# ---------------------------------------------------------------------------

class OT_AnimoraInterrupt(Operator):
    bl_idname = "animora.interrupt"
    bl_label = "Stop"
    bl_description = "Stop Animora's current response"

    def execute(self, context):
        from . import ws_client
        if ws_client.client.connected:
            ws_client.client.send_json({"type": "interrupt", "reason": "user_cancel"})
            self.report({"INFO"}, "Stopping…")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Quick-settings popover — opens from the panel's header gear icon
# ---------------------------------------------------------------------------

class OT_AnimoraSelfTest(Operator):
    """Verify the execution pipeline works without making any LLM call.

    Runs three known-good scripts through the exact same _execute_script
    code path the AI uses, with three synthetic tool_use_ids. If a cube,
    a sun light, and a material show up in the scene, the execution
    pipeline is healthy and any failures from the AI are AI-side
    (prompt / generation) rather than addon-side."""
    bl_idname = "animora.self_test"
    bl_label = "Run Self-Test"
    bl_description = "Run three test scripts to verify the execution pipeline works"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import time
        tests = [
            (
                "cube",
                "import bpy\n"
                "bpy.ops.mesh.primitive_cube_add(location=(3, 0, 0))\n"
                "cube = bpy.context.active_object\n"
                "cube.name = 'Animora_SelfTest_Cube'\n"
            ),
            (
                "light",
                "import bpy\n"
                "bpy.ops.object.light_add(type='SUN', location=(0, 0, 8))\n"
                "light = bpy.context.active_object\n"
                "light.name = 'Animora_SelfTest_Sun'\n"
                "light.data.energy = 3.0\n"
            ),
            (
                "material",
                "import bpy\n"
                "mat = bpy.data.materials.new(name='Animora_SelfTest_Material')\n"
                "mat.use_nodes = True\n"
                "bsdf = mat.node_tree.nodes['Principled BSDF']\n"
                "bsdf.inputs['Base Color'].default_value = (0.1, 0.4, 0.9, 1.0)\n"
                "bsdf.inputs['Metallic'].default_value = 1.0\n"
                "cube = bpy.data.objects.get('Animora_SelfTest_Cube')\n"
                "if cube and cube.data:\n"
                "    cube.data.materials.append(mat)\n"
            ),
        ]
        for name, script in tests:
            _execute_script(
                tool_use_id=f"selftest_{name}_{int(time.time() * 1000)}",
                script=script,
                intent_summary=f"Self-test: {name}",
            )

        self.report({"INFO"}, "Self-test complete — check the viewport + chat for results.")
        return {"FINISHED"}


class OT_AnimoraQuickSettings(Operator):
    """Compact popover with the common AI settings. Full settings live in
    Blender Preferences > Add-ons > Animora."""
    bl_idname = "animora.quick_settings"
    bl_label = "Animora Settings"
    bl_description = "Quick AI settings — full settings in Preferences"

    def execute(self, context):
        return context.window_manager.invoke_popup(self, width=380)

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=380)

    def draw(self, context):
        from . import credentials
        from .preferences import connection_status
        layout = self.layout
        prefs = get_prefs()

        # Header
        header = layout.row()
        header.label(text="Animora AI", icon="LOCKED")
        header.operator("animora.show_settings", text="", icon="PREFERENCES", emboss=False)

        layout.separator()

        # API key section
        if credentials.has_api_key():
            row = layout.row()
            row.label(text=f"Key: {credentials.fingerprint()}…", icon="CHECKMARK")
            actions = layout.row(align=True)
            actions.operator("animora.test_connection", icon="LINKED", text="Test")
            actions.operator("animora.clear_api_key", icon="TRASH", text="Remove")
        else:
            layout.label(text="No API key configured.", icon="ERROR")
            paste = layout.row(align=True)
            paste.prop(prefs, "pending_api_key", text="")
            paste.operator("animora.save_api_key", icon="FILE_TICK", text="Save")

        # Connection status
        cs = connection_status
        if cs.state in ("ok", "failed", "testing"):
            layout.separator()
            icon = {"ok": "CHECKMARK", "failed": "ERROR", "testing": "SORTTIME"}[cs.state]
            layout.label(text=cs.message, icon=icon)

        # Common settings
        layout.separator()
        col = layout.column(align=True)
        col.prop(prefs, "default_model")
        col.prop(prefs, "streaming_enabled")


# ---------------------------------------------------------------------------
# Window manager properties for chat state
# ---------------------------------------------------------------------------

class AnimoraChatItem(bpy.types.PropertyGroup):
    role: bpy.props.StringProperty()    # type: ignore[assignment]
    content: bpy.props.StringProperty() # type: ignore[assignment]


_classes = [
    AnimoraChatItem,
    OT_AnimoraSignIn,
    OT_AnimoraHandleAuthCallback,
    OT_AnimoraSignOut,
    OT_AnimoraSendMessage,
    OT_AnimoraStartRecording,
    OT_AnimoraNewConversation,
    OT_AnimoraSendSuggested,
    OT_AnimoraAttachFile,
    OT_AnimoraShowHistory,
    OT_AnimoraShowSettings,
    OT_AnimoraSaveApiKey,
    OT_AnimoraTestConnection,
    OT_AnimoraClearApiKey,
    OT_AnimoraInterrupt,
    OT_AnimoraSelfTest,
    OT_AnimoraQuickSettings,
    OT_AnimoraFeedback,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.animora_input_text = bpy.props.StringProperty(
        name="Message", default="",
        # Send on commit so Enter and a single SEND click both submit.
        update=_on_input_committed,
    )
    bpy.types.WindowManager.animora_chat_history = bpy.props.CollectionProperty(
        type=AnimoraChatItem
    )
    bpy.types.WindowManager.animora_chat_index = bpy.props.IntProperty(default=0)

    # Register the animora:// scheme (runtime fallback to the installer) and
    # start the callback poll timer that completes browser sign-in.
    try:
        deep_link.register_scheme()
    except Exception as exc:
        log.warning("animora:// scheme registration skipped: %s", exc)
    if not bpy.app.timers.is_registered(_poll_auth_callback):
        bpy.app.timers.register(_poll_auth_callback, first_interval=1.0)
    _configure_ws_callbacks()
    if auth.has_restorable_session():
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
        auth.restore_session_async(
            on_ready=lambda: _run_on_main_thread(_connect_ws),
            on_invalid=lambda: _run_on_main_thread(_restore_session_invalid),
        )
    else:
        state.set_auth_status(state.AuthS.SIGNED_OUT, "")


def unregister() -> None:
    if bpy.app.timers.is_registered(_poll_auth_callback):
        bpy.app.timers.unregister(_poll_auth_callback)
    del bpy.types.WindowManager.animora_input_text
    del bpy.types.WindowManager.animora_chat_history
    del bpy.types.WindowManager.animora_chat_index
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
