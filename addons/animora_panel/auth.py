"""
Authentication state management for the Animora addon.

Handles:
- OS-native secure token storage (keyring)
- PKCE code challenge/verifier generation
- Token refresh background thread
- Session state (signed in / signed out / trial)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import bpy

from . import auth_core

log = logging.getLogger("animora.auth")

KEYRING_SERVICE = "animora"
KEYRING_ACCESS_TOKEN = "access_token"
KEYRING_REFRESH_TOKEN = "refresh_token"
KEYRING_USER_EMAIL = "user_email"

_REFRESH_CHECK_INTERVAL = 300  # seconds (5 minutes)


@dataclass
class UserSession:
    user_id: str = ""
    email: str = ""
    plan: str = ""          # "trial" | "standard" | "studio"
    trial_end: Optional[float] = None
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: float = 0.0
    device_id: str = ""
    signed_in: bool = False


# Module-level session state (singleton)
session = UserSession()

_refresh_thread: Optional[threading.Thread] = None
_stop_refresh = threading.Event()


# ---------------------------------------------------------------------------
# Secure storage
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        log.warning("keyring not available — tokens stored in memory only (not persisted)")
        return False


def save_tokens(access_token: str, refresh_token: str) -> None:
    if _keyring_available():
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN, access_token)
        keyring.set_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN, refresh_token)
    session.access_token = access_token
    session.refresh_token = refresh_token


def load_tokens() -> tuple[str, str]:
    if _keyring_available():
        import keyring
        access = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN) or ""
        refresh = keyring.get_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN) or ""
        return access, refresh
    return session.access_token, session.refresh_token


def clear_tokens() -> None:
    if _keyring_available():
        import keyring
        for key in (KEYRING_ACCESS_TOKEN, KEYRING_REFRESH_TOKEN, KEYRING_USER_EMAIL):
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass
    session.access_token = ""
    session.refresh_token = ""
    session.signed_in = False


# ---------------------------------------------------------------------------
# Dev-mode sign-in bypass
# ---------------------------------------------------------------------------

def dev_signin() -> None:
    """Synthesise a local-dev session without going through PKCE.

    Used when Preferences > Add-ons > Animora > Dev Mode is enabled. The
    backend (`ai-backend/dev_server.py`) accepts any token string as a
    trial-plan claim, so we use a placeholder access_token of "dev". No
    keyring write — the synthetic token has no value and shouldn't persist
    beyond the running session.

    Production sign-in (PKCE → auth-server → real JWT) is unchanged.
    """
    session.user_id = "dev-user"
    session.email = "dev@local"
    session.plan = "trial"
    session.trial_end = time.time() + 365 * 86400
    session.access_token = "dev"
    session.refresh_token = ""
    session.token_expires_at = time.time() + 365 * 86400
    session.device_id = "dev-device"
    session.signed_in = True
    log.info("Dev sign-in: synthetic session created (no PKCE, no auth-server)")


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge). Delegates to the pure,
    unit-tested core so there is one PKCE implementation."""
    return auth_core.generate_pkce()


def generate_state() -> str:
    """Random CSRF state (delegates to the pure core)."""
    return auth_core.generate_state()


# ---------------------------------------------------------------------------
# Device fingerprint
# ---------------------------------------------------------------------------

def compute_device_fingerprint() -> str:
    """Stable, hardware-bound device identifier."""
    import hashlib
    import platform
    import socket
    import uuid

    components: list[str] = [
        platform.processor(),
        str(round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3) if hasattr(os, "sysconf") else ""),
        platform.node(),
        str(uuid.getnode()),  # MAC address
    ]

    # Windows: machine GUID from registry
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as k:
                components.append(winreg.QueryValueEx(k, "MachineGuid")[0])
        except Exception:
            pass

    # macOS / Linux: machine ID
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            components.append(open(path).read().strip())
            break
        except FileNotFoundError:
            pass

    # Install key (random, written once)
    install_key_path = _install_key_path()
    if not install_key_path.exists():
        install_key_path.parent.mkdir(parents=True, exist_ok=True)
        install_key_path.write_text(str(uuid.uuid4()))
    components.append(install_key_path.read_text().strip())

    raw = "|".join(sorted(c for c in components if c))
    return hashlib.sha256(raw.encode()).hexdigest()


def _install_key_path():
    from pathlib import Path
    return Path.home() / ".animora" / "install.key"


# ---------------------------------------------------------------------------
# Token exchange and refresh
# ---------------------------------------------------------------------------

def exchange_code(code: str, code_verifier: str) -> bool:
    """Exchange the one-time code + verifier + device_id for a Supabase
    session at the Edge Function. Returns True on success. On any HTTP
    failure (expired/used code, wrong verifier, device mismatch) returns
    False so the caller discards the pending request and restarts."""
    import json
    import urllib.error
    import urllib.request

    device_id = compute_device_fingerprint()
    url, headers, body = auth_core.build_exchange_request(code, code_verifier, device_id)
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        _apply_session(auth_core.parse_session_response(data))
        session.device_id = device_id
        return True
    except urllib.error.HTTPError as exc:
        log.error("Token exchange failed: HTTP %s (code single-use / device mismatch)", exc.code)
        return False
    except Exception as exc:
        log.error("Token exchange failed: %s", exc)
        return False


def _apply_session(norm: dict) -> None:
    """Apply a normalized Supabase session (from exchange or refresh)."""
    session.access_token = norm["access_token"]
    session.refresh_token = norm["refresh_token"]
    session.token_expires_at = norm["expires_at"]
    session.user_id = norm["user_id"]
    session.email = norm["email"]
    session.plan = norm["plan"]            # "free" for V1 (server-authoritative later)
    session.signed_in = True
    save_tokens(session.access_token, session.refresh_token)
    if session.email and _keyring_available():
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER_EMAIL, session.email)
    log.info("Signed in as %s (plan: %s)", session.email, session.plan)


def refresh_access_token() -> bool:
    """Refresh the Supabase session using the stored refresh token. Supabase
    rotates the refresh token, so we persist the new one."""
    import json
    import urllib.request

    if not session.refresh_token:
        return False

    url, headers, body = auth_core.build_refresh_request(session.refresh_token)
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        _apply_session(auth_core.parse_session_response(data))
        return True
    except Exception as exc:
        log.warning("Token refresh failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Background refresh thread
# ---------------------------------------------------------------------------

def _refresh_loop() -> None:
    while not _stop_refresh.wait(_REFRESH_CHECK_INTERVAL):
        if not session.signed_in:
            continue
        remaining = session.token_expires_at - time.time()
        if remaining < 120:  # refresh when < 2 minutes left
            log.debug("Refreshing access token (expires in %.0fs)", remaining)
            refresh_access_token()


def start_refresh_thread() -> None:
    global _refresh_thread
    _stop_refresh.clear()
    _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True, name="animora-token-refresh")
    _refresh_thread.start()


def stop_refresh_thread() -> None:
    _stop_refresh.set()


# ---------------------------------------------------------------------------
# Sign-out
# ---------------------------------------------------------------------------

def sign_out() -> None:
    """Global sign-out: clear the OS-secure-store tokens and reset session.
    Supabase sessions are stateless JWTs + a rotating refresh token, so
    discarding the refresh token locally is a complete sign-out for the
    device (no server round trip required)."""
    clear_tokens()
    session.__init__()  # type: ignore[misc]
    log.info("Signed out")


# ---------------------------------------------------------------------------
# Blender registration
# ---------------------------------------------------------------------------

def register() -> None:
    # Attempt to restore session from secure storage
    access, refresh = load_tokens()
    if access:
        session.access_token = access
        session.refresh_token = refresh
        session.signed_in = True
        # Kick off a refresh to validate and get fresh claims
        threading.Thread(target=refresh_access_token, daemon=True).start()
    start_refresh_thread()


def unregister() -> None:
    stop_refresh_thread()
