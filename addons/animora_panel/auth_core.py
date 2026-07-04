"""
Pure authentication logic for the Animora desktop (Blender) app.

bpy-FREE on purpose: every function here is a pure transform (no Blender,
no network, no keyring) so the auth contract can be unit-tested off-device
(see addons/tests/test_auth_core.py). The bpy-aware layer (session state,
keyring, refresh thread, browser, deep-link receiver) lives in auth.py /
operators.py / deep_link.py and imports from here.

Implements the Supabase PKCE device hand-off documented for animora.tech:
  1. app generates code_verifier/challenge (S256) + state + device id/label
  2. app opens {WEBSITE_BASE}/signin?next=/auth/device?...  in the browser
  3. website redirects to animora://auth/callback?code=...&state=...
  4. app verifies state, exchanges code+verifier+device_id at the Supabase
     Edge Function for a Supabase session, then refreshes via Supabase auth.

SECURITY: the only credential here is the Supabase PUBLISHABLE (anon) key,
which is public by design and safe to ship in the client. No service-role
key, DB credential, or access token is ever embedded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import platform
import secrets
import socket
import time
from urllib.parse import parse_qs, quote, urlparse

# ── Fixed config (PUBLIC values — safe to ship in the client) ───────────
SUPABASE_URL = "https://iyvchfmuyllovfoztbfw.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_23yhg9XIzsNmc9SbiDe-dg_tSFfAS59"
APP_URL_SCHEME = "animora"
REDIRECT_URI = "animora://auth/callback"

WEBSITE_BASE_PROD = "https://animora.tech"
WEBSITE_BASE_LOCAL = "http://localhost:8080"

TOKEN_EXCHANGE_ENDPOINT = f"{SUPABASE_URL}/functions/v1/auth-handoff-exchange"
REFRESH_ENDPOINT = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"

# Plan assigned to every signed-in user for the free V1. Paid tiers come
# later and must be server-authoritative (never trusted from the client).
DEFAULT_PLAN = "free"


# ── PKCE + CSRF ─────────────────────────────────────────────────────────
def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge). S256: challenge =
    base64url(sha256(verifier)). 32 random bytes → a 43-char base64url
    verifier (within the PKCE 43–128 range), no padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def generate_state() -> str:
    """Random CSRF state, base64url, >= 16 bytes of entropy."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def verify_state(expected: str, got: str) -> bool:
    """Constant-time state comparison. False if either is empty."""
    if not expected or not got:
        return False
    return hmac.compare_digest(expected, got)


# ── Device label (the human-readable companion to device_id) ────────────
def device_label() -> str:
    """e.g. 'Windows · DESKTOP-AB12'. device_id (the stable fingerprint) is
    computed in auth.py; this is only the friendly name shown on the site."""
    try:
        host = socket.gethostname() or "device"
    except Exception:
        host = "device"
    return f"{platform.system() or 'Desktop'} · {host}"


# ── URL building ────────────────────────────────────────────────────────
def build_signin_url(
    base: str, *, code_challenge: str, device_id: str, state: str,
    device_label: str, signup: bool = False,
) -> str:
    """Build {base}/signin?next=<encoded /auth/device?...> exactly per the
    documented contract. Every value is URL-encoded, then the whole `next`
    is encoded again as the query value."""
    nxt = (
        f"/auth/device?code_challenge={quote(code_challenge, safe='')}"
        f"&code_challenge_method=S256"
        f"&device_id={quote(device_id, safe='')}"
        f"&device_fingerprint={quote(device_id, safe='')}"
        f"&state={quote(state, safe='')}"
        f"&device={quote(device_label, safe='')}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
    )
    page = "signup" if signup else "signin"
    return f"{base}/{page}?next={quote(nxt, safe='')}"


def feedback_url(base: str, app_version: str) -> str:
    return f"{base}/feedback?source=app&v={quote(app_version, safe='')}"


# ── Callback parsing ────────────────────────────────────────────────────
def parse_callback_url(url: str) -> tuple[str, str] | None:
    """Parse animora://auth/callback?code=..&state=.. → (code, state).

    Returns None for any URL that isn't exactly our scheme + callback path,
    or is missing code/state (we ignore everything else, per the security
    rule 'only accept the exact animora://auth/callback path')."""
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    if p.scheme != APP_URL_SCHEME:
        return None
    # Accept both the current `animora://auth/callback` shape and the older
    # `animora://auth?code=...&state=...` shape still used by some builds.
    path = p.path.rstrip("/")
    if p.netloc != "auth" or path not in ("", "/callback"):
        return None
    q = parse_qs(p.query)
    code = (q.get("code") or [""])[0]
    state = (q.get("state") or [""])[0]
    if not code or not state:
        return None
    return code, state


# ── Supabase request/response shaping ───────────────────────────────────
def build_exchange_request(
    code: str, code_verifier: str, device_id: str,
) -> tuple[str, dict[str, str], bytes]:
    """(url, headers, body) for the one-time-code → session exchange at the
    Supabase Edge Function."""
    import json
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


def build_auth_server_exchange_request(
    auth_base: str, code: str, code_verifier: str, device_fingerprint: str,
    *, platform_name: str,
) -> tuple[str, dict[str, str], bytes]:
    import json

    headers = {"Content-Type": "application/json"}
    body = json.dumps({
        "code": code,
        "code_verifier": code_verifier,
        "device_fingerprint": device_fingerprint,
        "platform": platform_name,
    }).encode("utf-8")
    return auth_base.rstrip("/") + "/token", headers, body


def build_refresh_request(refresh_token: str) -> tuple[str, dict[str, str], bytes]:
    """(url, headers, body) for a Supabase refresh-token grant."""
    import json
    headers = {
        "Content-Type": "application/json",
        "apikey": SUPABASE_PUBLISHABLE_KEY,
    }
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    return REFRESH_ENDPOINT, headers, body


def build_auth_server_refresh_request(
    auth_base: str, refresh_token: str, device_fingerprint: str,
) -> tuple[str, dict[str, str], bytes]:
    import json

    headers = {"Content-Type": "application/json"}
    body = json.dumps({
        "refresh_token": refresh_token,
        "device_fingerprint": device_fingerprint,
    }).encode("utf-8")
    return auth_base.rstrip("/") + "/token/refresh", headers, body


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
