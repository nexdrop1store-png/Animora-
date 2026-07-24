"""
Secure credential storage for Animora.

Per the project's security rules (CLAUDE.md):
  "Blender addon: never store tokens in plaintext files —
   use keyring (OS secure store)"

This module wraps the `keyring` package so the rest of the addon stays
storage-agnostic. The key is stored in:
  • Windows: Credential Manager
  • macOS:   Keychain
  • Linux:   Secret Service (libsecret). If no keyring is available
             (minimal Linux), the key is held in memory for the session
             only — never written to disk in plaintext.

One stored item:

  ANTHROPIC_API_KEY     The BYOK Anthropic key. Sent in the WS hello.
                        Never written to disk in plaintext.

(The Supabase session tokens live in auth/session.py under the "animora"
keyring service — this module is only the Anthropic-key store.)

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

# Security fix — when no OS keyring is available (rare: minimal Linux with no
# Secret Service), we NEVER write the key to disk in plaintext. It's held in
# memory for the session only. This honors CLAUDE.md's absolute rule ("never
# store tokens in plaintext — use keyring"); the previous fallback wrote the
# raw key as JSON, which the module even mislabeled "encrypted-file storage".
_memory_key: Optional[str] = None


def _try_keyring():
    """Return the keyring module if importable AND a usable backend is
    available. Returns None if we should fall back to memory-only storage
    (Linux minimal installs) — we never persist the key in plaintext."""
    try:
        import keyring  # type: ignore
    except ImportError:
        log.warning("'keyring' module not available — key will be memory-only this session")
        return None
    try:
        backend = keyring.get_keyring()
        backend_name = backend.__class__.__name__ if backend else "None"
        if "Fail" in backend_name or "Null" in backend_name:
            log.warning("Keyring backend unusable (%s) — key will be memory-only this session", backend_name)
            return None
        return keyring
    except Exception as exc:
        log.warning("Keyring backend init failed: %s — key will be memory-only this session", exc)
        return None


def _fallback_path() -> Path:
    """Location of the LEGACY plaintext credential file written by older
    builds. Read-only now (for migration) — we never write it anymore."""
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


def _delete_fallback_file() -> None:
    """Remove any pre-existing plaintext fallback file (from older builds
    that persisted the key insecurely). Best-effort."""
    try:
        p = _fallback_path()
        if p.is_file():
            p.unlink()
            log.info("Removed legacy plaintext credential file (migrated to secure store).")
    except Exception as exc:
        log.debug("Could not remove legacy credential file: %s", exc)


# ── Public API ─────────────────────────────────────────────────────────

def set_api_key(key: str) -> None:
    """Persist the Anthropic API key. Empty string clears it.

    Uses the OS keyring when available. When it isn't, the key is kept in
    memory for THIS SESSION ONLY — never written to disk in plaintext (that
    would violate the project's no-plaintext-tokens rule). The user is told
    it won't survive a restart until a secure store is available."""
    global _memory_key
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
            _memory_key = key or None
            _delete_fallback_file()  # keyring is now the source of truth
            return
        except Exception as exc:
            log.warning("Keyring write failed: %s — holding key in memory only", exc)

    # No usable keyring: memory-only. Do NOT write plaintext to disk.
    _memory_key = key or None
    if key:
        log.warning(
            "No OS keyring available — API key kept in memory for this session "
            "only and NOT saved to disk (plaintext storage is disallowed). "
            "You'll need to re-enter it next launch, or install a Secret "
            "Service provider (e.g. gnome-keyring) to persist it securely."
        )


def get_api_key() -> Optional[str]:
    """Return the stored Anthropic API key, or None.

    Order: keyring → in-memory (this session) → a legacy plaintext file from
    an older build (read once, then migrated into the keyring and deleted if
    a keyring is available, so it doesn't linger insecurely)."""
    global _memory_key
    kr = _try_keyring()
    if kr is not None:
        try:
            val = kr.get_password(_SERVICE_NAME, _KEY_USERNAME)
            if val:
                return val
        except Exception as exc:
            log.warning("Keyring read failed: %s — trying memory/legacy", exc)

    if _memory_key:
        return _memory_key

    # Legacy plaintext file from an older build. Read it so existing users
    # aren't logged out, but migrate it off plaintext immediately.
    legacy = _fallback_read().get(_KEY_USERNAME)
    if legacy:
        _memory_key = legacy
        if kr is not None:
            try:
                kr.set_password(_SERVICE_NAME, _KEY_USERNAME, legacy)
                _delete_fallback_file()
                log.info("Migrated API key from legacy plaintext file into the OS keyring.")
            except Exception as exc:
                log.debug("Legacy key migration to keyring failed: %s", exc)
        else:
            log.warning(
                "Found an API key in a legacy PLAINTEXT file and no keyring to "
                "migrate it into. Loaded it for this session; consider clearing "
                "it and installing a secure store."
            )
        return legacy
    return None


def clear_api_key() -> None:
    global _memory_key
    _memory_key = None
    _delete_fallback_file()
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
    return (
        f"Held in memory for this session only (no OS keyring available — "
        f"the key is not saved to disk). Fingerprint: {fingerprint()}"
    )


