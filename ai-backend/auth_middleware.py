"""JWT authentication and plan enforcement for WebSocket connections."""

from __future__ import annotations

import logging
import os
import time

from jose import JWTError, jwt

from .config import settings
from .models import TokenClaims

log = logging.getLogger("animora.auth")


class AuthError(Exception):
    def __init__(self, message: str, code: str = "auth_error"):
        super().__init__(message)
        self.code = code


# ── Supabase token validation ───────────────────────────────────────────
# The desktop app signs in via Supabase (PKCE device hand-off) and sends a
# Supabase access token. Those are signed by Supabase, not our JWT_SECRET,
# so the legacy decode_token() below can't verify them. We verify by asking
# Supabase who the token belongs to (GET /auth/v1/user) — this uses the
# PUBLIC publishable/anon key, needs no Supabase JWT secret, and works for
# both HS256 and asymmetric Supabase projects.
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://iyvchfmuyllovfoztbfw.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY", "sb_publishable_23yhg9XIzsNmc9SbiDe-dg_tSFfAS59")
# Free V1 plan for every signed-in user; paid tiers are server-authoritative later.
_DEFAULT_PLAN = "free"


def _looks_like_supabase(token: str) -> bool:
    """Peek at the unverified issuer to route Supabase tokens to Supabase
    validation (vs. our own dev-mode JWTs)."""
    try:
        iss = str(jwt.get_unverified_claims(token).get("iss", ""))
    except JWTError:
        return False
    return "supabase" in iss or iss.endswith("/auth/v1")


async def _validate_supabase(token: str) -> TokenClaims:
    import httpx
    try:
        unverified = jwt.get_unverified_claims(token)
    except JWTError as exc:
        raise AuthError(f"Malformed token: {exc}", "invalid_token") from exc
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY},
            )
    except Exception as exc:  # network / DNS
        raise AuthError(f"Auth service unreachable: {exc}", "auth_unreachable") from exc
    if resp.status_code != 200:
        raise AuthError("Invalid or expired session", "invalid_token")
    user = resp.json()
    uid = user.get("id") or str(unverified.get("sub", ""))
    if not uid:
        raise AuthError("No user id in token", "invalid_token")
    return TokenClaims(
        user_id=uid,
        plan=_DEFAULT_PLAN,
        trial_end=None,
        device_id=str((user.get("user_metadata") or {}).get("device_id", "")),
        seats_used=1,
        exp=float(unverified.get("exp", time.time() + 3600)),
        email=str(user.get("email") or ""),
    )


async def validate_token(token: str) -> TokenClaims:
    """Single async entry point used by the WS handler. Routes Supabase
    tokens to Supabase verification; everything else (dev_server / a future
    Supabase) uses the local-JWT decode_token() below."""
    if _looks_like_supabase(token):
        return await _validate_supabase(token)
    return decode_token(token)


def decode_token(token: str) -> TokenClaims:
    # H5 — Enforce issuer + audience. python-jose validates both when the
    # `issuer` / `audience` kwargs are passed; if a claim is missing or
    # doesn't match, it raises JWTError. Auth-server (when deployed) must
    # mint tokens with these claims; dev_server.py already does.
    #
    # In dev mode, we permit tokens without an audience claim so the dev
    # bypass continues to work. Production never sees this branch.
    env_dev = os.environ.get("ANIMORA_ENV", "").lower() in ("dev", "development", "local")
    decode_kwargs = {
        "algorithms": [settings.jwt_algorithm],
        "issuer": settings.jwt_issuer,
        "audience": settings.jwt_audience,
    }
    if env_dev:
        # Dev path: don't enforce iss/aud so dev tokens with bare claims
        # (synthesised by dev_server) still pass. Production requires both.
        decode_kwargs.pop("issuer", None)
        decode_kwargs.pop("audience", None)

    try:
        payload = jwt.decode(token, settings.jwt_secret, **decode_kwargs)
    except JWTError as exc:
        raise AuthError(f"Invalid token: {exc}", "invalid_token") from exc

    if payload.get("exp", 0) < time.time():
        raise AuthError("Token expired", "token_expired")

    return TokenClaims(**payload)


def check_plan_access(claims: TokenClaims, requested_model: str | None = None) -> None:
    plan = claims.plan

    if plan == "trial":
        if claims.trial_end and time.time() > claims.trial_end:
            raise AuthError("Trial period expired — please upgrade", "trial_expired")

    if requested_model and "opus" in requested_model.lower():
        if plan != "studio":
            raise AuthError("Opus model requires Studio plan", "plan_insufficient")


async def check_rate_limit(redis, user_id: str, plan: str) -> None:
    """Redis token bucket rate limiter."""
    import redis.asyncio as aioredis

    hour_key = f"rate:{user_id}:hour:{int(time.time() // 3600)}"
    day_key = f"rate:{user_id}:day:{int(time.time() // 86400)}"

    limits = {
        "trial": (settings.rate_trial_hour, settings.rate_trial_day),
        "standard": (settings.rate_standard_hour, settings.rate_standard_day),
        "studio": (settings.rate_studio_hour, settings.rate_studio_day),
    }
    hour_limit, day_limit = limits.get(plan, limits["trial"])

    pipe = redis.pipeline()
    pipe.incr(hour_key)
    pipe.expire(hour_key, 3600)
    pipe.incr(day_key)
    pipe.expire(day_key, 86400)
    results = await pipe.execute()

    hour_count, _, day_count, _ = results

    if hour_count > hour_limit:
        raise AuthError(f"Rate limit exceeded: {hour_count}/{hour_limit} messages this hour", "rate_limit")
    if day_count > day_limit:
        raise AuthError(f"Rate limit exceeded: {day_count}/{day_limit} messages today", "rate_limit")
