"""JWT token issuance and refresh token management."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta

from jose import jwt

from .config import settings


def _sign(payload: dict) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_access_token(
    user_id: str,
    email: str,
    plan: str,
    device_id: str,
    trial_end: datetime | None = None,
    seats_used: int = 1,
) -> tuple[str, float]:
    """Return (access_token, expires_at_timestamp)."""
    exp = time.time() + settings.access_token_expire_minutes * 60
    payload = {
        "sub": user_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "user_id": user_id,
        "email": email,
        "plan": plan,
        "device_id": device_id,
        "seats_used": seats_used,
        "exp": exp,
    }
    if trial_end:
        payload["trial_end"] = trial_end.timestamp()
    return _sign(payload), exp


def generate_refresh_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash). Store hash in DB, send raw to client."""
    raw = secrets.token_urlsafe(48)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_auth_code() -> str:
    return secrets.token_urlsafe(32)
