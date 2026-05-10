"""
Animora AI Backend — FastAPI application.

WebSocket endpoint: /ws/{session_id}?token=<access_token>

Message flow:
  Client → server: user_message, resume, tool_result, viewport_frame (binary), hd_capture, scene_graph
  Server → client: stream_token, tool_call, session_info, error
"""

from __future__ import annotations

import json
import logging
import struct

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .auth_middleware import AuthError, check_plan_access, check_rate_limit, decode_token
from .config import settings
from .orchestrator import stream_response
from .scene_intelligence import build_scene_context
from .session_manager import (
    append_turn,
    delete_session,
    get_redis,
    get_session,
    save_session,
    update_scene_context,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("animora.main")

app = FastAPI(title="Animora AI Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://animora.tech", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
):
    await websocket.accept()

    # Authenticate
    try:
        claims = decode_token(token)
        check_plan_access(claims)
    except AuthError as exc:
        await websocket.send_json({"type": "error", "message": str(exc), "code": exc.code})
        await websocket.close(code=4001)
        return

    redis = await get_redis()
    session_data = await get_session(session_id)
    session_data["user_id"] = claims.user_id
    session_data["plan"] = claims.plan

    # Send session info to client
    await websocket.send_json({
        "type": "session_info",
        "session_id": session_id,
        "plan": claims.plan,
        "history_count": len(session_data.get("conversation_history", [])),
    })

    log.info("WS connected: user=%s session=%s plan=%s", claims.user_id, session_id, claims.plan)

    pending_tool_results: dict[str, dict] = {}

    try:
        while True:
            raw = await websocket.receive()

            # Binary frame (viewport stream / HD capture)
            if "bytes" in raw and raw["bytes"]:
                data = raw["bytes"]
                if data and data[0] == 0x01:
                    # Level 1 viewport frame — buffer for scene context, no LLM call
                    pass
                continue

            # Text frame
            if "text" not in raw or not raw["text"]:
                continue

            try:
                msg = json.loads(raw["text"])
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "resume":
                # Client reconnected — session already loaded above
                log.info("Session resumed: %s", session_id)
                await websocket.send_json({
                    "type": "session_info",
                    "session_id": session_id,
                    "plan": claims.plan,
                    "history_count": len(session_data.get("conversation_history", [])),
                })

            elif msg_type == "scene_graph":
                graph = msg.get("graph", {})
                await update_scene_context(session_id, graph)
                session_data["scene_context"] = graph

            elif msg_type == "hd_capture":
                # Store latest HD image in session for vision context
                session_data["last_hd_capture"] = msg.get("data", "")
                session_data["last_hd_trigger"] = msg.get("trigger", "")

            elif msg_type == "tool_result":
                tool_use_id = msg.get("tool_use_id", "")
                pending_tool_results[tool_use_id] = {
                    "output": msg.get("output", ""),
                    "error": msg.get("error", ""),
                }

            elif msg_type == "user_message":
                user_text = msg.get("text", "").strip()
                if not user_text:
                    continue

                # Rate limit check
                try:
                    await check_rate_limit(redis, claims.user_id, claims.plan)
                except AuthError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc), "code": exc.code})
                    continue

                # Build scene context string
                scene_graph = session_data.get("scene_context", {})
                scene_ctx = build_scene_context(scene_graph)

                # Stream callbacks
                async def send_token(token: str) -> None:
                    await websocket.send_json({"type": "stream_token", "token": token})

                async def send_tool_call(tool_name: str, tool_use_id: str, tool_input: dict) -> None:
                    await websocket.send_json({
                        "type": "tool_call",
                        "tool": tool_name,
                        "tool_use_id": tool_use_id,
                        "input": tool_input,
                    })

                history = session_data.get("conversation_history", [])
                full_response = await stream_response(
                    user_message=user_text,
                    conversation_history=history,
                    scene_context_str=scene_ctx,
                    plan=claims.plan,
                    scene_graph=scene_graph,
                    send_token_cb=send_token,
                    send_tool_call_cb=send_tool_call,
                )

                # Persist to session history
                await append_turn(session_id, "user", user_text, claims.plan)
                await append_turn(session_id, "assistant", full_response, claims.plan)
                session_data["conversation_history"] = (
                    session_data.get("conversation_history", [])
                    + [{"role": "user", "content": user_text}, {"role": "assistant", "content": full_response}]
                )

    except WebSocketDisconnect:
        log.info("WS disconnected: session=%s", session_id)
    except Exception as exc:
        log.exception("Unhandled WS error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "message": "Internal server error"})
        except Exception:
            pass
    finally:
        await save_session(session_id, session_data)
