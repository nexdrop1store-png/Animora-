"""
OAuth 2.0 + PKCE authorization endpoints.

Flow:
  GET  /authorize     → redirect to animora.tech/auth with code_challenge
  POST /token         → exchange auth code for access + refresh tokens
  POST /token/refresh → rotate refresh token
  DELETE /session     → revoke session (sign-out)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .db.models import AuthCode, Device, Session, User
from .device import check_fingerprint_abuse, get_or_create_device
from .tokens import (
    generate_auth_code,
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
)

log = logging.getLogger("animora.oauth")
router = APIRouter()


class TokenRequest(BaseModel):
    code: str
    code_verifier: str
    device_fingerprint: str
    platform: str = ""


class RefreshRequest(BaseModel):
    refresh_token: str
    device_fingerprint: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    user_id: str
    email: str
    plan: str
    trial_end: float | None = None


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


@router.post("/token", response_model=TokenResponse)
async def exchange_token(req: TokenRequest, db: AsyncSession = Depends(get_db)):
    # Look up auth code
    result = await db.execute(
        select(AuthCode).where(AuthCode.code == req.code, AuthCode.used == False)  # noqa: E712
    )
    auth_code = result.scalar_one_or_none()
    if not auth_code:
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
    if datetime.utcnow() > auth_code.expires_at:
        raise HTTPException(status_code=400, detail="Authorization code expired")

    # Verify PKCE
    if not _verify_pkce(req.code_verifier, auth_code.code_challenge):
        raise HTTPException(status_code=400, detail="PKCE verification failed")

    # Verify device fingerprint matches
    if req.device_fingerprint != auth_code.device_fingerprint:
        raise HTTPException(status_code=400, detail="Device fingerprint mismatch")

    # Mark code as used
    auth_code.used = True

    # Load user
    result = await db.execute(select(User).where(User.id == auth_code.user_id))
    user = result.scalar_one_or_none()
    if not user or user.is_banned:
        raise HTTPException(status_code=403, detail="Account not found or suspended")

    # Check abuse
    if await check_fingerprint_abuse(db, req.device_fingerprint):
        log.warning("Fingerprint abuse detected for user %s", user.id)

    # Get or create device binding
    try:
        device, _ = await get_or_create_device(db, user, req.device_fingerprint, req.platform)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Issue tokens
    access_token, exp = issue_access_token(
        user_id=user.id,
        email=user.email,
        plan=user.plan,
        device_id=device.id,
        trial_end=user.trial_end,
    )
    raw_refresh, hashed_refresh = generate_refresh_token()

    db_session = Session(
        user_id=user.id,
        device_id=device.id,
        refresh_token_hash=hashed_refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(db_session)
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user_id=user.id,
        email=user.email,
        plan=user.plan,
        trial_end=user.trial_end.timestamp() if user.trial_end else None,
    )


@router.post("/token/refresh", response_model=TokenResponse)
async def refresh_token(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    hashed = hash_refresh_token(req.refresh_token)
    result = await db.execute(
        select(Session).where(Session.refresh_token_hash == hashed, Session.revoked == False)  # noqa: E712
    )
    db_session = result.scalar_one_or_none()
    if not db_session or datetime.utcnow() > db_session.expires_at:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    result = await db.execute(select(User).where(User.id == db_session.user_id))
    user = result.scalar_one_or_none()
    if not user or user.is_banned:
        raise HTTPException(status_code=403, detail="Account suspended")

    # Rotate: revoke old session, issue new one
    db_session.revoked = True
    raw_refresh, hashed_refresh = generate_refresh_token()
    new_session = Session(
        user_id=user.id,
        device_id=db_session.device_id,
        refresh_token_hash=hashed_refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(new_session)

    access_token, exp = issue_access_token(
        user_id=user.id,
        email=user.email,
        plan=user.plan,
        device_id=db_session.device_id,
        trial_end=user.trial_end,
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user_id=user.id,
        email=user.email,
        plan=user.plan,
        trial_end=user.trial_end.timestamp() if user.trial_end else None,
    )


@router.delete("/session")
async def revoke_session(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401)

    from jose import JWTError, jwt

    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("user_id", "")
    device_id = payload.get("device_id", "")

    # Revoke all sessions for this device
    result = await db.execute(
        select(Session).where(
            Session.user_id == user_id,
            Session.device_id == device_id,
            Session.revoked == False,  # noqa: E712
        )
    )
    sessions = result.scalars().all()
    for s in sessions:
        s.revoked = True
    await db.commit()
    return {"status": "signed_out"}
