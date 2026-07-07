"""Session state, secure token storage, and token refresh for Animora.

bpy-free: keyring/network/threads only, so the whole session layer is
unit-testable off-device (addons/tests/test_auth_session.py). Blender
integration (timers, status, WS) lives in controller.py.

Storage contract (unchanged from V1 so existing signed-in installs survive):
- keyring service "animora"; the ROTATING refresh token is the persistence
  anchor; access tokens are persisted only when they fit the Windows
  Credential Manager blob limit (<= 512 chars), else kept in memory and
  re-derived via refresh on the next launch.
- Supabase rotates the refresh token on EVERY grant and revokes the whole
  session family on reuse — never refresh the same token from two
  processes, and never treat a transient network failure as a rejection.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import platform
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import supabase

log = logging.getLogger("animora.auth")

KEYRING_SERVICE = "animora"
KEYRING_ACCESS_TOKEN = "access_token"
KEYRING_REFRESH_TOKEN = "refresh_token"
KEYRING_USER_EMAIL = "user_email"

REFRESH_CHECK_INTERVAL = 300  # seconds (5 minutes)
RESTORE_RETRY_DELAYS = (0.0, 5.0, 15.0)  # transient-failure retries at startup

_last_auth_error = ""
_last_refresh_rejected = False


@dataclass
class Session:
    user_id: str = ""
    email: str = ""
    plan: str = ""          # "free" for V1 (server-authoritative later)
    trial_end: float | None = None
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: float = 0.0
    device_id: str = ""
    signed_in: bool = False


# Module-level session state (singleton)
session = Session()

_refresh_thread: threading.Thread | None = None
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
    if not _keyring_available():
        return
    import keyring
    try:
        if refresh_token:
            keyring.set_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN, refresh_token)
        else:
            with contextlib.suppress(Exception):
                keyring.delete_password(KEYRING_SERVICE, KEYRING_REFRESH_TOKEN)
    except Exception as exc:
        log.warning("Failed to persist refresh token in keyring: %s", exc)

    # Windows Credential Manager rejects large blobs; access tokens are
    # ephemeral anyway, so keep them in memory and rely on refresh token
    # restore across app launches.
    try:
        if access_token and len(access_token) <= 512:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN, access_token)
        else:
            with contextlib.suppress(Exception):
                keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCESS_TOKEN)
    except Exception as exc:
        log.warning("Access token was kept in memory only: %s", exc)


def load_tokens() -> tuple[str, str]:
    if not _keyring_available():
        return session.access_token, session.refresh_token
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


def load_email() -> str:
    if not _keyring_available():
        return ""
    import keyring
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER_EMAIL) or ""
    except Exception:
        return ""


def clear_tokens() -> None:
    if _keyring_available():
        import keyring
        for key in (KEYRING_ACCESS_TOKEN, KEYRING_REFRESH_TOKEN, KEYRING_USER_EMAIL):
            with contextlib.suppress(Exception):
                keyring.delete_password(KEYRING_SERVICE, key)
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

    Used when Preferences > Add-ons > Animora > Dev Mode is enabled (and by
    the recording bundle). The backend (`ai-backend/dev_server.py`) accepts
    any token string as a trial-plan claim, so we use a placeholder
    access_token of "dev". No keyring write — the synthetic token has no
    value and shouldn't persist beyond the running session.
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
    log.info("Dev sign-in: synthetic session created (no PKCE, no browser)")


# ---------------------------------------------------------------------------
# Device fingerprint
# ---------------------------------------------------------------------------

def compute_device_fingerprint() -> str:
    """Stable, hardware-bound device identifier. Unchanged from V1 so the
    server-side device_bindings row for this machine keeps matching."""
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
            components.append(Path(path).read_text().strip())
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


def _install_key_path() -> Path:
    return Path.home() / ".animora" / "install.key"


# ---------------------------------------------------------------------------
# Token exchange and refresh (Supabase — the single provider)
# ---------------------------------------------------------------------------

def exchange_code(code: str, code_verifier: str) -> bool:
    """Exchange the one-time hand-off code for a Supabase session."""
    import json
    import urllib.error
    import urllib.request

    global _last_auth_error
    _last_auth_error = ""
    device_id = compute_device_fingerprint()
    url, headers, body = supabase.build_exchange_request(code, code_verifier, device_id)
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        _last_auth_error = f"HTTP {exc.code}: {detail}".strip()
        log.error("Token exchange failed: %s", _last_auth_error)
        return False
    except Exception as exc:
        _last_auth_error = f"{exc}"
        log.error("Token exchange failed: %s", exc)
        return False

    _apply_session(supabase.parse_session_response(data))
    session.device_id = device_id
    log.info("Token exchange succeeded")
    return True


def _apply_session(norm: dict) -> None:
    """Apply a normalized Supabase session (from exchange or refresh)."""
    session.access_token = norm["access_token"]
    session.refresh_token = norm["refresh_token"]
    session.token_expires_at = norm["expires_at"]
    session.user_id = norm["user_id"]
    session.email = norm["email"]
    session.plan = norm["plan"]
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
    """Refresh the current Supabase session.

    Sets the module-level rejection flag (see last_refresh_rejected):
    a failure only counts as a definitive rejection when Supabase answered
    with a 4xx (invalid/rotated/revoked token). If it was unreachable or
    returned a 5xx, the refresh token may still be good and must not be
    discarded by callers."""
    import json
    import urllib.error
    import urllib.request

    global _last_refresh_rejected

    if not session.refresh_token:
        return False

    url, headers, body = supabase.build_refresh_request(session.refresh_token)
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        _last_refresh_rejected = 400 <= exc.code < 500
        log.warning("Token refresh failed: HTTP %s %s", exc.code, detail)
        return False
    except Exception as exc:
        _last_refresh_rejected = False
        log.warning("Token refresh failed: %s", exc)
        return False

    _apply_session(supabase.parse_session_response(data))
    _last_refresh_rejected = False
    log.info("Token refresh succeeded")
    return True


def last_refresh_rejected() -> bool:
    """True when the most recent refresh_access_token() failure was a
    definitive rejection (token invalid/rotated/revoked), as opposed to a
    transient network/server error."""
    return _last_refresh_rejected


def last_auth_error() -> str:
    return _last_auth_error


# ---------------------------------------------------------------------------
# Background refresh thread
# ---------------------------------------------------------------------------

def _refresh_loop() -> None:
    while not _stop_refresh.wait(REFRESH_CHECK_INTERVAL):
        if not session.signed_in:
            continue
        remaining = session.token_expires_at - time.time()
        # Refresh whenever the token could expire before the NEXT check —
        # a threshold smaller than the check interval leaves a window where
        # the token dies between ticks and reconnects start 401ing.
        if remaining < REFRESH_CHECK_INTERVAL + 120:
            log.debug("Refreshing access token (expires in %.0fs)", remaining)
            refresh_access_token()


def start_refresh_thread() -> None:
    global _refresh_thread
    _stop_refresh.clear()
    _refresh_thread = threading.Thread(
        target=_refresh_loop, daemon=True, name="animora-token-refresh"
    )
    _refresh_thread.start()


def stop_refresh_thread() -> None:
    _stop_refresh.set()


# ---------------------------------------------------------------------------
# Sign-out and restore
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
    Transient failures (offline at launch, server hiccup) get a couple of
    retries and NEVER discard the refresh token — signing the user out
    because their wifi was down would lose a perfectly good session. Only
    a definitive rejection clears it.
    """
    if not has_restorable_session():
        return False

    def _restore() -> None:
        if not session.refresh_token:
            log.info("Persisted session missing refresh token; clearing local auth state")
            sign_out()
            _invoke_callback(on_invalid)
            return

        for delay in RESTORE_RETRY_DELAYS:
            if delay and _stop_refresh.wait(delay):
                return
            if refresh_access_token():
                session.signed_in = True
                _invoke_callback(on_ready)
                return
            if last_refresh_rejected():
                log.info("Persisted session rejected; clearing local auth state")
                sign_out()
                _invoke_callback(on_invalid)
                return

        log.info("Persisted session refresh failed (network); keeping tokens for a later retry")
        _invoke_callback(on_invalid)

    threading.Thread(target=_restore, daemon=True, name="animora-session-restore").start()
    return True


def load_persisted() -> None:
    """Load tokens + email from the OS secure store into the session object
    WITHOUT marking it signed in (that requires a successful refresh)."""
    access, refresh = load_tokens()
    if access or refresh:
        session.access_token = access
        session.refresh_token = refresh
        session.signed_in = False
        session.email = load_email()
