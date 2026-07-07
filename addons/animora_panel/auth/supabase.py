"""Supabase request builders + response shaping for Animora desktop auth.

Pure and bpy-free: every function is a pure transform (no network, no
keyring) so the provider contract is unit-testable off-device. Supabase is
the single production provider — the website mints one-time hand-off codes
via the `issue_device_handoff` RPC and the desktop redeems them at the
`auth-handoff-exchange` Edge Function.

SECURITY: the only credential here is the Supabase PUBLISHABLE (anon) key,
which is public by design and safe to ship in the client. No service-role
key, DB credential, or access token is ever embedded.
"""

from __future__ import annotations

import json
import platform
import socket
import time
from urllib.parse import quote

# ── Fixed config (PUBLIC values — safe to ship in the client) ───────────
SUPABASE_URL = "https://iyvchfmuyllovfoztbfw.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_23yhg9XIzsNmc9SbiDe-dg_tSFfAS59"

WEBSITE_BASE_PROD = "https://animora.tech"
WEBSITE_BASE_LOCAL = "http://localhost:8080"

TOKEN_EXCHANGE_ENDPOINT = f"{SUPABASE_URL}/functions/v1/auth-handoff-exchange"
REFRESH_ENDPOINT = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"

# Plan assigned to every signed-in user for the free V1. Paid tiers come
# later and must be server-authoritative (never trusted from the client).
DEFAULT_PLAN = "free"


def device_label() -> str:
    """e.g. 'Windows · DESKTOP-AB12'. device_id (the stable fingerprint) is
    computed in session.py; this is only the friendly name shown on the site."""
    try:
        host = socket.gethostname() or "device"
    except Exception:
        host = "device"
    return f"{platform.system() or 'Desktop'} · {host}"


def build_signin_url(
    base: str, *, code_challenge: str, device_id: str, state: str,
    device_label: str, redirect_uri: str, signup: bool = False,
) -> str:
    """Build {base}/signin?next=<encoded /auth/device?...> per the website
    contract. `redirect_uri` is the app's loopback callback endpoint
    (http://127.0.0.1:{port}/auth/callback). Every value is URL-encoded,
    then the whole `next` is encoded again as the query value."""
    nxt = (
        f"/auth/device?code_challenge={quote(code_challenge, safe='')}"
        f"&code_challenge_method=S256"
        f"&device_id={quote(device_id, safe='')}"
        f"&device_fingerprint={quote(device_id, safe='')}"
        f"&state={quote(state, safe='')}"
        f"&device={quote(device_label, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
    )
    page = "signup" if signup else "signin"
    return f"{base}/{page}?next={quote(nxt, safe='')}"


def feedback_url(base: str, app_version: str) -> str:
    return f"{base}/feedback?source=app&v={quote(app_version, safe='')}"


def build_exchange_request(
    code: str, code_verifier: str, device_id: str,
) -> tuple[str, dict[str, str], bytes]:
    """(url, headers, body) for the one-time-code → session exchange at the
    Supabase Edge Function."""
    headers = {
        "Content-Type": "application/json",
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
    }
    body = json.dumps({
        "code": code,
        "code_verifier": code_verifier,
        "device_id": device_id,
    }).encode("utf-8")
    return TOKEN_EXCHANGE_ENDPOINT, headers, body


def build_refresh_request(refresh_token: str) -> tuple[str, dict[str, str], bytes]:
    """(url, headers, body) for a Supabase refresh-token grant. Supabase
    ROTATES the refresh token on every grant and revokes the whole session
    family on reuse — callers must persist the new token immediately and
    never refresh the same token from two processes."""
    headers = {
        "Content-Type": "application/json",
        "apikey": SUPABASE_PUBLISHABLE_KEY,
    }
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    return REFRESH_ENDPOINT, headers, body


def parse_session_response(data: dict) -> dict:
    """Normalize a Supabase session payload (from either the exchange or a
    refresh) into the fields the addon's session model needs. Handles the
    nested `user` object and computes an absolute expiry."""
    user = data.get("user") or {}
    expires_at = data.get("expires_at")
    if not expires_at:
        expires_at = time.time() + float(data.get("expires_in", 3600) or 3600)
    user_id = data.get("user_id", "") or user.get("id", "")
    email = data.get("email", "") or user.get("email", "")
    plan = data.get("plan", "") or DEFAULT_PLAN
    return {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": float(expires_at),
        "user_id": user_id,
        "email": email,
        "plan": plan,
        "trial_end": data.get("trial_end"),
    }
