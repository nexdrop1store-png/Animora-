"""
Unit tests for the desktop auth contract (addons/animora_panel/auth_core.py).

Pure Python — no Blender, no network. We load auth_core.py directly from
its path so the bpy-importing package __init__ is never touched.

Run:
    pytest addons/tests/test_auth_core.py -v
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

_CORE_PATH = (Path(__file__).resolve().parent.parent
              / "animora_panel" / "auth_core.py")
_spec = importlib.util.spec_from_file_location("animora_auth_core", _CORE_PATH)
ac = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(ac)  # type: ignore[union-attr]


# ── PKCE (S256) ─────────────────────────────────────────────────────────
def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = ac.generate_pkce()
    # 32 bytes → 43-char base64url, no padding, within PKCE 43–128 range.
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge
    # Independently recompute the S256 challenge.
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_is_random_each_call():
    assert ac.generate_pkce()[0] != ac.generate_pkce()[0]


# ── state / CSRF ────────────────────────────────────────────────────────
def test_state_random_and_urlsafe():
    s1, s2 = ac.generate_state(), ac.generate_state()
    assert s1 != s2
    assert len(s1) >= 22  # >= 16 bytes base64url
    assert "=" not in s1


def test_verify_state_matches_and_rejects():
    s = ac.generate_state()
    assert ac.verify_state(s, s) is True
    assert ac.verify_state(s, s + "x") is False
    assert ac.verify_state("", s) is False
    assert ac.verify_state(s, "") is False


# ── sign-in URL contract ────────────────────────────────────────────────
def test_signin_url_matches_documented_contract():
    url = ac.build_signin_url(
        "https://animora.tech", code_challenge="CHAL", device_id="DEV id/+",
        state="ST", device_label="Win · HOST",
    )
    assert url.startswith("https://animora.tech/signin?next=")
    # `next` decodes to the /auth/device path with all params.
    next_val = parse_qs(urlparse(url).query)["next"][0]
    assert next_val.startswith("/auth/device?")
    inner = parse_qs(urlparse(next_val).query)
    assert inner["code_challenge"] == ["CHAL"]
    assert inner["device_id"] == ["DEV id/+"]          # decoded round-trip
    assert inner["state"] == ["ST"]
    assert inner["device"] == ["Win · HOST"]
    assert inner["redirect_uri"] == ["animora://auth/callback"]


def test_signup_variant_uses_signup_page():
    url = ac.build_signin_url(
        "https://animora.tech", code_challenge="C", device_id="D",
        state="S", device_label="L", signup=True)
    assert "/signup?next=" in url


def test_feedback_url():
    u = ac.feedback_url("http://localhost:8080", "1.0.0")
    assert u == "http://localhost:8080/feedback?source=app&v=1.0.0"


# ── callback parsing (security: only the exact path) ────────────────────
def test_parse_valid_callback():
    assert ac.parse_callback_url(
        "animora://auth/callback?code=ONE_TIME&state=ST") == ("ONE_TIME", "ST")


def test_parse_rejects_wrong_scheme_and_path():
    assert ac.parse_callback_url("https://auth/callback?code=c&state=s") is None
    assert ac.parse_callback_url("animora://auth/evil?code=c&state=s") is None
    assert ac.parse_callback_url("animora://other/callback?code=c&state=s") is None


def test_parse_rejects_missing_params():
    assert ac.parse_callback_url("animora://auth/callback?code=c") is None
    assert ac.parse_callback_url("animora://auth/callback?state=s") is None
    assert ac.parse_callback_url("") is None
    assert ac.parse_callback_url("not a url") is None


# ── Supabase request/response shaping ───────────────────────────────────
def test_exchange_request_shape():
    url, headers, body = ac.build_exchange_request("CODE", "VER", "DEVID")
    assert url.endswith("/functions/v1/auth-handoff-exchange")
    assert headers["apikey"] == ac.SUPABASE_PUBLISHABLE_KEY
    assert headers["Authorization"] == f"Bearer {ac.SUPABASE_PUBLISHABLE_KEY}"
    import json
    payload = json.loads(body)
    assert payload == {"code": "CODE", "code_verifier": "VER", "device_id": "DEVID"}


def test_refresh_request_shape():
    url, headers, body = ac.build_refresh_request("RT")
    assert "grant_type=refresh_token" in url
    assert headers["apikey"] == ac.SUPABASE_PUBLISHABLE_KEY
    import json
    assert json.loads(body) == {"refresh_token": "RT"}


def test_parse_session_response_nested_user_and_plan():
    norm = ac.parse_session_response({
        "access_token": "AT", "refresh_token": "RT",
        "expires_at": 1780000000,
        "user": {"id": "uuid-1", "email": "a@b.com"},
    })
    assert norm["access_token"] == "AT"
    assert norm["refresh_token"] == "RT"
    assert norm["expires_at"] == 1780000000.0
    assert norm["user_id"] == "uuid-1"
    assert norm["email"] == "a@b.com"
    assert norm["plan"] == "free"  # free V1 default


def test_parse_session_response_computes_expiry_from_expires_in():
    import time
    before = time.time()
    norm = ac.parse_session_response({
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
        "user": {"id": "u", "email": "e"},
    })
    assert before + 3500 <= norm["expires_at"] <= before + 3700


# ── secrets hygiene: no service-role key embedded ───────────────────────
def test_only_publishable_key_present():
    src = _CORE_PATH.read_text(encoding="utf-8")
    assert "service_role" not in src
    assert "sb_secret" not in src
    # The publishable key is the only credential, and it's the public one.
    assert ac.SUPABASE_PUBLISHABLE_KEY.startswith("sb_publishable_")


def test_device_label_nonempty():
    assert ac.device_label()
