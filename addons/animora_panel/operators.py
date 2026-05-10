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
import webbrowser
from urllib.parse import urlencode

import bpy
from bpy.types import Operator

from . import auth, ws_client
from .preferences import get_prefs

log = logging.getLogger("animora.operators")

# Shared state written by operators, read by panel
_pkce_verifier: str = ""
_pending_auth: bool = False


class OT_AnimoraSignIn(Operator):
    bl_idname = "animora.sign_in"
    bl_label = "Sign In"
    bl_description = "Sign in to your Animora account"

    def execute(self, context: bpy.types.Context):
        global _pkce_verifier, _pending_auth
        prefs = get_prefs()

        code_verifier, code_challenge = auth.generate_pkce()
        _pkce_verifier = code_verifier
        _pending_auth = True

        device_fp = auth.compute_device_fingerprint()
        params = urlencode({
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": "animora://auth",
            "device_fingerprint": device_fp,
        })
        url = f"{prefs.effective_auth_url()}/authorize?{params}"
        log.info("Opening auth URL in browser")
        webbrowser.open(url)
        self.report({"INFO"}, "Browser opened — complete sign-in, then return here")
        return {"FINISHED"}


class OT_AnimoraHandleAuthCallback(Operator):
    """Called by the animora:// URL handler with the authorization code."""
    bl_idname = "animora.handle_auth_callback"
    bl_label = "Handle Auth Callback"

    code: bpy.props.StringProperty()  # type: ignore[assignment]

    def execute(self, context: bpy.types.Context):
        global _pending_auth
        if not _pending_auth or not _pkce_verifier:
            self.report({"ERROR"}, "No pending auth — please click Sign In first")
            return {"CANCELLED"}

        def _exchange():
            ok = auth.exchange_code(self.code, _pkce_verifier)
            if ok:
                _connect_ws()
            else:
                log.error("Auth code exchange failed")

        threading.Thread(target=_exchange, daemon=True).start()
        _pending_auth = False
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
        self.report({"INFO"}, "Signed out of Animora")
        return {"FINISHED"}


class OT_AnimoraSendMessage(Operator):
    bl_idname = "animora.send_message"
    bl_label = "Send"
    bl_description = "Send message to Animora AI"

    def execute(self, context: bpy.types.Context):
        wm = context.window_manager
        text = getattr(wm, "animora_input_text", "").strip()
        if not text:
            return {"CANCELLED"}
        if not ws_client.client.connected:
            self.report({"WARNING"}, "Not connected — sign in first")
            return {"CANCELLED"}

        prefs = get_prefs()
        ws_client.client.send_message(text, context_flags={
            "share_viewport": prefs.share_viewport,
            "share_scene_graph": prefs.share_scene_graph,
        })

        # Append user message to chat history
        _append_chat("user", text)
        wm.animora_input_text = ""
        context.area.tag_redraw()
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
    ws_client.client.on_stream_token = _on_stream_token
    ws_client.client.on_tool_call = _on_tool_call
    ws_client.client.connect(
        url=prefs.effective_backend_url(),
        session_id=session_id,
        access_token=auth.session.access_token,
    )


def _on_stream_token(token: str) -> None:
    wm = bpy.context.window_manager
    history = wm.animora_chat_history
    if not history or history[-1].role != "assistant":
        item = history.add()
        item.role = "assistant"
        item.content = token
    else:
        history[-1].content += token
    for area in bpy.context.screen.areas:
        area.tag_redraw()


def _on_tool_call(msg: dict) -> None:
    tool_name = msg.get("tool")
    tool_input = msg.get("input", {})
    tool_use_id = msg.get("tool_use_id", "")

    if tool_name == "execute_blender_script":
        _execute_script(tool_use_id, tool_input.get("script", ""))
    else:
        log.warning("Unknown tool call: %s", tool_name)


def _execute_script(tool_use_id: str, script: str) -> None:
    result = {"tool_use_id": tool_use_id, "output": "", "error": ""}
    try:
        ns: dict = {}
        exec(compile(script, "<animora_tool>", "exec"), ns)  # noqa: S102
        result["output"] = ns.get("_result", "OK")
    except Exception as exc:
        result["error"] = str(exc)
        log.error("Script execution error: %s", exc)

    ws_client.client.send_json({"type": "tool_result", **result})


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
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.animora_input_text = bpy.props.StringProperty(
        name="Message", default=""
    )
    bpy.types.WindowManager.animora_chat_history = bpy.props.CollectionProperty(
        type=AnimoraChatItem
    )
    bpy.types.WindowManager.animora_chat_index = bpy.props.IntProperty(default=0)


def unregister() -> None:
    del bpy.types.WindowManager.animora_input_text
    del bpy.types.WindowManager.animora_chat_history
    del bpy.types.WindowManager.animora_chat_index
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
