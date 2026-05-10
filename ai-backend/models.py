"""Pydantic schemas for WebSocket message protocol."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class UserMessage(BaseModel):
    type: Literal["user_message"]
    text: str
    session_id: str
    context: dict[str, bool] = Field(default_factory=dict)


class ResumeMessage(BaseModel):
    type: Literal["resume"]
    session_id: str


class ToolResultMessage(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    output: str = ""
    error: str = ""


class ViewportFrame(BaseModel):
    type: Literal["viewport_frame"]
    seq: int
    timestamp: float
    width: int
    height: int


class HDCapture(BaseModel):
    type: Literal["hd_capture"]
    trigger: str
    timestamp: float
    width: int
    height: int
    data: str  # base64-encoded JPEG/PNG


class SceneGraph(BaseModel):
    type: Literal["scene_graph"]
    timestamp: float
    graph: dict[str, Any]


class StreamToken(BaseModel):
    type: Literal["stream_token"] = "stream_token"
    token: str


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    tool_use_id: str
    input: dict[str, Any]


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    code: Optional[str] = None


class SessionInfo(BaseModel):
    type: Literal["session_info"] = "session_info"
    session_id: str
    plan: str
    history_count: int


# JWT token claims
class TokenClaims(BaseModel):
    user_id: str
    plan: str  # trial | standard | studio
    trial_end: Optional[float] = None
    device_id: str
    seats_used: int = 1
    exp: float
