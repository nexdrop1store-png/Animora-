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
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import bpy

from . import auth_core
from .preferences import get_prefs

log = logging.getLogger("animora.auth")

KEYRING_SERVICE = "animora"
KEYRING_ACCESS_TOKEN = "access_token"
KEYRING_REFRESH_TOKEN = "refresh_token"
KEYRING_USER_EMAIL = "user_email"

_REFRESH_CHECK_INTERVAL = 300  # seconds (5 minutes)
_RESTORE_RETRY_DELAYS = (0.0, 5.0, 15.0)  # transient-failure retries at startup
_last_auth_error = ""
_last_refresh_rejected = False


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
    session.access_token = access_token
    session.refresh_token = refresh_token
    if _keyring_available():
        import keyring
        try:
            if refresh_token:
                keyring.set_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN, refresh_token)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Failed to persist refresh token in keyring: %s", exc)

        # Windows Credential Manager rejects large blobs; access tokens are
        # ephemeral anyway, so keep them in memory and rely on refresh token
        # restore across app launches.
        try:
            if access_token and len(access_token) <= 512:
                keyring.set_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN, access_token)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Access token was kept in memory only: %s", exc)


def load_tokens() -> tuple[str, str]:
    if _keyring_available():
        import keyring
        try:
            access = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN) or ""
        except Exception as exc:
            log.warning("Could not read access token from keyring: %s", exc)
            access = ""
        try:
            refresh = keyring.get_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN) or ""
        except Exception as exc:
            log.warning("Could not read refresh token from keyring: %s", exc)
            refresh = ""
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


def has_restorable_session() -> bool:
    return bool(session.access_token or session.refresh_token)


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
    """Exchange the one-time code for a desktop session.

    We support both production auth shapes:
    - auth.animora.tech `/token` with `device_fingerprint`
    - the older Supabase Edge Function handoff with `device_id`
    """
    import json
    import urllib.error
    import urllib.request

    global _last_auth_error
    _last_auth_error = ""
    device_id = compute_device_fingerprint()
    prefs = get_prefs()
    attempts = [
        (
            "auth_server",
            *auth_core.build_auth_server_exchange_request(
                prefs.effective_auth_url(),
                code,
                code_verifier,
                device_id,
                platform_name=platform.system() or "Desktop",
            ),
        ),
        ("supabase", *auth_core.build_exchange_request(code, code_verifier, device_id)),
    ]
    last_error = ""
    for name, url, headers, body in attempts:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            _apply_session(auth_core.parse_session_response(data))
            session.device_id = device_id
            _last_auth_error = ""
            log.info("Token exchange succeeded via %s", name)
            return True
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            last_error = f"{name} HTTP {exc.code}: {detail}".strip()
            log.warning("Token exchange via %s failed: HTTP %s %s", name, exc.code, detail)
        except Exception as exc:
            last_error = f"{name}: {exc}"
            log.warning("Token exchange via %s failed: %s", name, exc)
    _last_auth_error = last_error or "Sign-in failed."
    log.error("Token exchange failed across all providers: %s", _last_auth_error)
    return False


def _apply_session(norm: dict) -> None:
    """Apply a normalized Supabase session (from exchange or refresh)."""
    session.access_token = norm["access_token"]
    session.refresh_token = norm["refresh_token"]
    session.token_expires_at = norm["expires_at"]
    session.user_id = norm["user_id"]
    session.email = norm["email"]
    session.plan = norm["plan"]            # "free" for V1 (server-authoritative later)
    session.trial_end = norm.get("trial_end")
    session.signed_in = True
    save_tokens(session.access_token, session.refresh_token)
    if session.email and _keyring_available():
        import keyring
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER_EMAIL, session.email)
        except Exception as exc:
            log.warning("User email was kept in memory only: %s", exc)
    log.info("Signed in as %s (plan: %s)", session.email, session.plan)


def _invoke_callback(cb: Callable[[], None] | None) -> None:
    if cb is None:
        return
    try:
        cb()
    except Exception as exc:
        log.warning("Session callback failed: %s", exc)


def refresh_access_token() -> bool:
    """Refresh the current desktop session across either auth stack.

    Sets the module-level rejection flag (see last_refresh_rejected):
    a failure only counts as a definitive rejection when EVERY provider
    answered with a 4xx (invalid/rotated/revoked token). If any provider
    was unreachable or returned a 5xx, the refresh token may still be
    good and must not be discarded by callers."""
    import json
    import urllib.error
    import urllib.request

    global _last_refresh_rejected

    if not session.refresh_token:
        return False

    prefs = get_prefs()
    device_id = compute_device_fingerprint()
    attempts = [
        (
            "auth_server",
            *auth_core.build_auth_server_refresh_request(
                prefs.effective_auth_url(),
                session.refresh_token,
                device_id,
            ),
        ),
        ("supabase", *auth_core.build_refresh_request(session.refresh_token)),
    ]
    all_rejected = True
    for name, url, headers, body in attempts:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            _apply_session(auth_core.parse_session_response(data))
            _last_refresh_rejected = False
            log.info("Token refresh succeeded via %s", name)
            return True
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            if not 400 <= exc.code < 500:
                all_rejected = False
            log.warning("Token refresh via %s failed: HTTP %s %s", name, exc.code, detail)
        except Exception as exc:
            all_rejected = False
            log.warning("Token refresh via %s failed: %s", name, exc)
    _last_refresh_rejected = all_rejected
    return False


def last_refresh_rejected() -> bool:
    """True when the most recent refresh_access_token() failure was a
    definitive rejection by every provider (token invalid/rotated), as
    opposed to a transient network/server error."""
    return _last_refresh_rejected


def last_auth_error() -> str:
    return _last_auth_error


# ---------------------------------------------------------------------------
# Background refresh thread
# ---------------------------------------------------------------------------

def _refresh_loop() -> None:
    while not _stop_refresh.wait(_REFRESH_CHECK_INTERVAL):
        if not session.signed_in:
            continue
        remaining = session.token_expires_at - time.time()
        # Refresh whenever the token could expire before the NEXT check —
        # a threshold smaller than the check interval leaves a window where
        # the token dies between ticks and reconnects start 401ing.
        if remaining < _REFRESH_CHECK_INTERVAL + 120:
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


def restore_session_async(
    *,
    on_ready: Callable[[], None] | None = None,
    on_invalid: Callable[[], None] | None = None,
) -> bool:
    """Refresh persisted credentials and call back with the result.

    A restored session is only considered usable once refresh succeeds.
    This avoids the panel claiming the user is signed in while the backend
    would reject the stale token on the next WebSocket connect attempt.
    """
    if not has_restorable_session():
        return False

    def _restore() -> None:
        if not session.refresh_token:
            log.info("Persisted session missing refresh token; clearing local auth state")
            sign_out()
            _invoke_callback(on_invalid)
            return

        # Transient failures (offline at launch, server hiccup) get a couple
        # of retries and NEVER discard the refresh token — signing the user
        # out because their wifi was down would lose a perfectly good
        # session. Only a definitive rejection by every provider clears it.
        for delay in _RESTORE_RETRY_DELAYS:
            if delay and _stop_refresh.wait(delay):
                return
            if refresh_access_token():
                session.signed_in = True
                _invoke_callback(on_ready)
                return
            if last_refresh_rejected():
                log.info("Persisted session rejected by auth providers; clearing local auth state")
                sign_out()
                _invoke_callback(on_invalid)
                return

        log.info("Persisted session refresh failed (network); keeping tokens for a later retry")
        _invoke_callback(on_invalid)

    threading.Thread(target=_restore, daemon=True, name="animora-session-restore").start()
    return True


# ---------------------------------------------------------------------------
# Blender registration
# ---------------------------------------------------------------------------

def register() -> None:
    # Attempt to restore session from secure storage
    access, refresh = load_tokens()
    if access or refresh:
        session.access_token = access
        session.refresh_token = refresh
        session.signed_in = False
        if _keyring_available():
            try:
                import keyring
                session.email = keyring.get_password(KEYRING_SERVICE, KEYRING_USER_EMAIL) or ""
            except Exception:
                pass
    start_refresh_thread()


def unregister() -> None:
    stop_refresh_thread()
