"""PKCE + CSRF-state contract tests (animora_panel.auth.pkce)."""

from __future__ import annotations

import base64
import hashlib

from animora_panel.auth import pkce


def test_pkce_round_trip_matches_s256_reference():
    verifier, challenge = pkce.generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected


def test_pkce_verifier_shape():
    verifier, challenge = pkce.generate_pkce()
    # 32 random bytes → 43-char unpadded base64url (within RFC 7636's 43–128)
    assert len(verifier) == 43
    assert len(challenge) == 43
    assert "=" not in verifier and "=" not in challenge
    # base64url alphabet only
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(verifier) <= allowed
    assert set(challenge) <= allowed


def test_pkce_values_unique_per_call():
    seen = {pkce.generate_pkce()[0] for _ in range(20)}
    assert len(seen) == 20


def test_state_entropy_and_uniqueness():
    states = {pkce.generate_state() for _ in range(20)}
    assert len(states) == 20
    assert all(len(s) == 43 for s in states)


def test_verify_state_match():
    s = pkce.generate_state()
    assert pkce.verify_state(s, s)


def test_verify_state_mismatch():
    assert not pkce.verify_state("expected", "different")


def test_verify_state_empty_either_side():
    assert not pkce.verify_state("", "got")
    assert not pkce.verify_state("expected", "")
    assert not pkce.verify_state("", "")
