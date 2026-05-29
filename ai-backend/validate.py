"""
REST endpoint: POST /validate-key

Lets the Animora addon test an Anthropic key (typed by the user in Settings)
WITHOUT opening a full WebSocket session. The endpoint is unauthenticated
because the client is providing the key itself — there's nothing to
authenticate against. The key is used once for a single Haiku ping, then
discarded.

Response shape:
  200 OK   { ok: true, model_pinged, elapsed_ms }
  200 OK   { ok: false, error_code, error_message } (validation failed
            but the request itself was fine — UI surfaces the error)
  4xx      { ok: false, error_code: "bad_request", ... } (malformed input)

Why a separate endpoint instead of WS hello + close:
  • The "test connection" button in Settings shouldn't open a session.
  • We can rate-limit /validate-key independently in case someone tries
    to use it as a key-guesser.
  • Plain HTTP is simpler to call from the addon's settings UI thread
    than a one-shot WS dance.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .anthropic_client import AnthropicClient
from .key_source import looks_like_anthropic_key
from .observability import logger
from .session_manager import get_redis

log = logger("animora.validate")

router = APIRouter()


class ValidateKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=10, max_length=200)
    # Optional: which model to ping. Defaults to cheapest (Haiku).
    model: str = "claude-haiku-4-5-20251001"


class ValidateKeyResponse(BaseModel):
    ok: bool
    error_code: str = ""
    error_message: str = ""
    model_pinged: str = ""
    elapsed_ms: int = 0


# Per-IP rate limit. Backed by Redis so the limit holds across multiple
# uvicorn workers / Fargate tasks — an in-memory dict would let an
# attacker get N×limit by spraying across workers (security audit H3).
_RATE_LIMIT_PER_MINUTE = 10
_RATE_WINDOW_SEC = 60


async def _rate_check(client_ip: str) -> bool:
    """Return True if the request is within the rate limit.

    Uses Redis INCR + EXPIRE on a per-(ip, minute) key. Atomic via
    pipeline. Falls open (returns True) if Redis is unreachable — we
    log + alert rather than DoSing legitimate users on infra failure.
    """
    try:
        r = await get_redis()
        minute_bucket = int(time.time() // _RATE_WINDOW_SEC)
        key = f"rate:validate:{client_ip}:{minute_bucket}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _RATE_WINDOW_SEC)
        results = await pipe.execute()
        count = int(results[0])
        return count <= _RATE_LIMIT_PER_MINUTE
    except Exception as exc:
        # If Redis is down, we fail OPEN (allow the request) but log
        # loudly. Failing closed would let a Redis outage take down
        # validation for all users.
        log.error("validate.rate_check_failed_open", extra={"error": str(exc)})
        return True


# Generic message returned for ALL validation failures (security audit H4).
# Specific error codes go to server logs only — don't help attackers
# distinguish "invalid key" from "permission denied" from "rate limited".
_GENERIC_FAILURE_MESSAGE = "Could not validate API key. Check the key and try again."


@router.post("/validate-key", response_model=ValidateKeyResponse)
async def validate_key(req: ValidateKeyRequest, request: Request) -> Any:
    client_ip = request.client.host if request.client else "unknown"

    if not await _rate_check(client_ip):
        log.warning("validate.rate_limited", extra={
            "client_ip": client_ip, "limit_per_min": _RATE_LIMIT_PER_MINUTE,
        })
        return JSONResponse(
            status_code=429,
            content=ValidateKeyResponse(
                ok=False, error_code="rate_limited",
                error_message="Too many validation attempts. Wait a minute.",
            ).model_dump(),
        )

    if not looks_like_anthropic_key(req.api_key):
        # Format check is the ONE place where a specific message is safe
        # — it tells the user how to fix their input without leaking
        # whether any particular key exists at Anthropic.
        return ValidateKeyResponse(
            ok=False, error_code="bad_format",
            error_message="Key doesn't look like an Anthropic key (should start with 'sk-ant-').",
        )

    client = AnthropicClient(req.api_key, session_id="validate", max_retries=1)
    detail_code = ""
    detail_message = ""
    try:
        result = await asyncio.wait_for(client.validate(req.model), timeout=15.0)
        if result.ok:
            return ValidateKeyResponse(
                ok=True,
                model_pinged=result.model_pinged,
                elapsed_ms=result.elapsed_ms,
            )
        detail_code = result.error_code
        detail_message = result.error_message
    except asyncio.TimeoutError:
        detail_code = "timeout"
        detail_message = "Anthropic took too long to respond."
    except Exception as exc:
        detail_code = "internal"
        detail_message = f"{type(exc).__name__}"

    # Detail is for our logs only — client gets the generic message.
    log.warning("validate.failed", extra={
        "client_ip": client_ip,
        "detail_code": detail_code,
        "detail_message": detail_message,
    })
    return ValidateKeyResponse(
        ok=False, error_code="validation_failed",
        error_message=_GENERIC_FAILURE_MESSAGE,
    )
