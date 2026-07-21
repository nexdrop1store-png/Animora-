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


class HelloMessage(BaseModel):
    """First message after WS accept. Carries the optional BYOK key
    (if absent or empty, backend falls back to its pooled master key)
    plus client-side settings (chosen model, temperature, max_tokens,
    streaming on/off, debug mode)."""
    type: Literal["hello"]
    api_key: str = ""  # BYOK — empty string means use pooled
    animora_version: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class InterruptMessage(BaseModel):
    """User-initiated cancellation of the in-flight LLM stream."""
    type: Literal["interrupt"]
    reason: str = "user_cancel"


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


# ── Phase 5.5: quality-retry WS message types ───────────────────────────
# Surfaced by the streaming loop when artist's-eye fails and a revision
# pass is being applied. The panel uses these to show a non-blocking
# "refining..." status (retrying) and to silently clear it (succeeded)
# or escalate to the existing quality_notice flow (exhausted).
#
# These are emitted directly via websocket.send_json — they're not (yet)
# routed through a Pydantic-validated send path, so the schemas are
# documentation more than enforcement. Codifying here so the panel /
# tests / future migrations have a single source of truth.

class QualityRetrying(BaseModel):
    type: Literal["quality.retrying"] = "quality.retrying"
    attempt: int        # 1-indexed; 1 = first retry, 2 = second, ...
    max_retries: int
    summary: str = ""   # brief artist's-eye summary (<= 200 chars)


class QualityRetrySucceeded(BaseModel):
    type: Literal["quality.retry_succeeded"] = "quality.retry_succeeded"
    retries_used: int   # how many retries were needed before passing


class QualityNotice(BaseModel):
    """Soft warning surfaced when quality failed AND retries are exhausted
    (or retry was disabled). The panel typically shows a non-blocking
    yellow banner with the failed_checks + fix_suggestions."""
    type: Literal["quality_notice"] = "quality_notice"
    severity: str = "warning"
    summary: str
    failed_checks: list[dict[str, str]] = Field(default_factory=list)
    fix_suggestions: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    retries_used: int = 0


# JWT token claims
class TokenClaims(BaseModel):
    user_id: str
    plan: str  # trial | standard | studio
    trial_end: Optional[float] = None
    device_id: str
    seats_used: int = 1
    exp: float
    # v1.3 — populated for Supabase-authenticated users (from the
    # /auth/v1/user response); empty for local dev JWTs that carry no
    # email claim. Used only for the admin-usage-visibility allowlist
    # check (usage_ledger.py) — never treat this as an identity
    # guarantee elsewhere, user_id is the real identity key.
    email: str = ""
