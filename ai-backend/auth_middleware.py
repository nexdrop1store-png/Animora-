"""JWT authentication and plan enforcement for WebSocket connections."""

from __future__ import annotations

import logging
import time

from jose import JWTError, jwt

from .config import settings
from .models import TokenClaims

log = logging.getLogger("animora.auth")


class AuthError(Exception):
    def __init__(self, message: str, code: str = "auth_error"):
        super().__init__(message)
        self.code = code


def decode_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
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
