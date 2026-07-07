"""PKCE + CSRF-state primitives for the Animora sign-in flow.

Pure and bpy-free: one implementation, unit-tested off-device
(addons/tests/test_auth_pkce.py). S256 per RFC 7636.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


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
