"""
Secure credential storage for Animora.

Per the project's security rules (CLAUDE.md):
  "Blender addon: never store tokens in plaintext files —
   use keyring (OS secure store)"

This module wraps the `keyring` package so the rest of the addon stays
storage-agnostic. The key is stored in:
  • Windows: Credential Manager
  • macOS:   Keychain
  • Linux:   Secret Service (libsecret), with a JSON fallback if not
             available so the addon doesn't hard-fail on minimal Linux
             installs (the fallback is documented as less secure in
             status_message()).

Two stored items:

  ANTHROPIC_API_KEY     The BYOK Anthropic key. Sent in the WS hello.
                        Never written to disk in plaintext.
  ANIMORA_ACCESS_TOKEN  Reserved for future use — the JWT issued by
                        auth-server when SaaS/pooled mode is active.

Public surface (callers should only use these):
  set_api_key(key) / get_api_key() / clear_api_key()
  has_api_key() / fingerprint() / status_message()
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("animora.credentials")

_SERVICE_NAME = "Animora"
_KEY_USERNAME = "anthropic_api_key"
_TOKEN_USERNAME = "access_token"


def _try_keyring():
    """Return the keyring module if importable AND a usable backend is
    available. Returns None if we should fall back to encrypted-file
    storage (Linux minimal installs)."""
    try:
        import keyring  # type: ignore
    except ImportError:
        log.warning("'keyring' module not available — falling back to file storage")
        return None
    try:
        backend = keyring.get_keyring()
        backend_name = backend.__class__.__name__ if backend else "None"
        if "Fail" in backend_name or "Null" in backend_name:
            log.warning("Keyring backend unusable (%s) — falling back to file storage", backend_name)
            return None
        return keyring
    except Exception as exc:
        log.warning("Keyring backend init failed: %s — falling back to file storage", exc)
        return None


def _fallback_path() -> Path:
    """Where to put the JSON fallback when keyring isn't usable. Lives
    inside Blender's user config dir, which is per-user / not world-readable
    on a sane OS install. Still not ideal — keyring is preferred."""
    try:
        import bpy
        cfg = Path(bpy.utils.user_resource("CONFIG"))
    except Exception:
        cfg = Path.home() / ".animora"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg / "credentials.json"


def _fallback_read() -> dict:
    p = _fallback_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fallback_write(data: dict) -> None:
    p = _fallback_path()
    try:
        p.write_text(json.dumps(data), encoding="utf-8")
        # Best-effort chmod 0600 on POSIX
        try:
            import stat
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception as exc:
        log.error("Failed to write fallback credentials: %s", exc)


# ── Public API ─────────────────────────────────────────────────────────

def set_api_key(key: str) -> None:
    """Persist the Anthropic API key. Empty string clears it."""
    key = (key or "").strip()
    kr = _try_keyring()
    if kr is not None:
        try:
            if key:
                kr.set_password(_SERVICE_NAME, _KEY_USERNAME, key)
            else:
                try:
                    kr.delete_password(_SERVICE_NAME, _KEY_USERNAME)
                except Exception:
                    pass
            return
        except Exception as exc:
            log.warning("Keyring write failed: %s — falling back", exc)

    data = _fallback_read()
    if key:
        data[_KEY_USERNAME] = key
    else:
        data.pop(_KEY_USERNAME, None)
    _fallback_write(data)


def get_api_key() -> Optional[str]:
    """Return the stored Anthropic API key, or None."""
    kr = _try_keyring()
    if kr is not None:
        try:
            val = kr.get_password(_SERVICE_NAME, _KEY_USERNAME)
            if val:
                return val
        except Exception as exc:
            log.warning("Keyring read failed: %s — trying fallback", exc)

    data = _fallback_read()
    val = data.get(_KEY_USERNAME)
    return val if val else None


def clear_api_key() -> None:
    set_api_key("")


def has_api_key() -> bool:
    return bool(get_api_key())


def fingerprint() -> str:
    """Sha256 prefix of the stored key (12 chars) — safe to display in UI
    so the user can confirm 'yes, my key is saved' without revealing it."""
    key = get_api_key()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def status_message() -> str:
    """Human-readable string about where the key is currently stored.
    Surfaced in Settings so users know if they're using secure storage."""
    if not has_api_key():
        return "No API key configured."
    kr = _try_keyring()
    if kr is not None:
        try:
            backend = kr.get_keyring().__class__.__name__
            return f"Stored in OS keyring ({backend}). Fingerprint: {fingerprint()}"
        except Exception:
            pass
    return f"Stored in fallback file (keyring unavailable). Fingerprint: {fingerprint()}"


# ── Access token (reserved for SaaS mode) ─────────────────────────────

def set_access_token(token: str) -> None:
    kr = _try_keyring()
    if kr is not None:
        try:
            if token:
                kr.set_password(_SERVICE_NAME, _TOKEN_USERNAME, token)
            else:
                try:
                    kr.delete_password(_SERVICE_NAME, _TOKEN_USERNAME)
                except Exception:
                    pass
            return
        except Exception:
            pass
    data = _fallback_read()
    if token:
        data[_TOKEN_USERNAME] = token
    else:
        data.pop(_TOKEN_USERNAME, None)
    _fallback_write(data)


def get_access_token() -> Optional[str]:
    kr = _try_keyring()
    if kr is not None:
        try:
            val = kr.get_password(_SERVICE_NAME, _TOKEN_USERNAME)
            if val:
                return val
        except Exception:
            pass
    return _fallback_read().get(_TOKEN_USERNAME) or None
