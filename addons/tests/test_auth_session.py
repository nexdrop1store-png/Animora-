"""Session-layer tests (animora_panel.auth.session) — fake keyring +
mocked urlopen; no Blender, no network."""

from __future__ import annotations

import io
import json
import threading
import types
import urllib.error
import urllib.request

import pytest

from animora_panel.auth import session as session_mod
from animora_panel.auth import supabase


class FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))

    def delete_password(self, service, key):
        if (service, key) not in self.store:
            raise KeyError(key)
        del self.store[(service, key)]


@pytest.fixture()
def fake_keyring(monkeypatch):
    kr = FakeKeyring()
    module = types.ModuleType("keyring")
    module.set_password = kr.set_password
    module.get_password = kr.get_password
    module.delete_password = kr.delete_password
    monkeypatch.setitem(__import__("sys").modules, "keyring", module)
    return kr


@pytest.fixture(autouse=True)
def reset_session():
    session_mod.session.__init__()
    yield
    session_mod.session.__init__()
    session_mod._last_refresh_rejected = False
    session_mod._last_auth_error = ""


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example", code=code, msg="err", hdrs=None, fp=io.BytesIO(b"detail")
    )


def _ok_response(payload: dict):
    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


# ── Storage ──────────────────────────────────────────────────────────────

def test_save_load_clear_round_trip(fake_keyring):
    session_mod.save_tokens("access-token", "refresh-token")
    assert session_mod.load_tokens() == ("access-token", "refresh-token")

    session_mod.clear_tokens()
    assert session_mod.load_tokens() == ("", "")
    assert not session_mod.session.signed_in


def test_large_access_token_never_persisted(fake_keyring):
    big = "x" * 600
    session_mod.save_tokens(big, "refresh-token")
    # In-memory session holds it; the keyring must not.
    assert session_mod.session.access_token == big
    assert (session_mod.KEYRING_SERVICE, session_mod.KEYRING_ACCESS_TOKEN) not in fake_keyring.store
    assert fake_keyring.store[
        (session_mod.KEYRING_SERVICE, session_mod.KEYRING_REFRESH_TOKEN)
    ] == "refresh-token"


def test_has_restorable_session(fake_keyring):
    assert not session_mod.has_restorable_session()
    session_mod.session.refresh_token = "r"
    assert session_mod.has_restorable_session()


# ── Response parsing ─────────────────────────────────────────────────────

def test_parse_session_response_expires_at():
    norm = supabase.parse_session_response(
        {"access_token": "a", "refresh_token": "r", "expires_at": 1000.0,
         "user": {"id": "u1", "email": "e@x.com"}}
    )
    assert norm["expires_at"] == 1000.0
    assert norm["user_id"] == "u1"
    assert norm["email"] == "e@x.com"
    assert norm["plan"] == supabase.DEFAULT_PLAN


def test_parse_session_response_expires_in(monkeypatch):
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 500.0)
    norm = supabase.parse_session_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    assert norm["expires_at"] == pytest.approx(500.0 + 3600)


def test_parse_session_response_flat_fields_win():
    norm = supabase.parse_session_response(
        {"access_token": "a", "refresh_token": "r", "expires_at": 1.0,
         "user_id": "flat", "email": "flat@x.com", "plan": "studio",
         "user": {"id": "nested", "email": "nested@x.com"}}
    )
    assert norm["user_id"] == "flat"
    assert norm["email"] == "flat@x.com"
    assert norm["plan"] == "studio"


# ── Exchange ─────────────────────────────────────────────────────────────

def test_exchange_code_success(fake_keyring, monkeypatch):
    monkeypatch.setattr(session_mod, "compute_device_fingerprint", lambda: "dev-fp")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=0: _ok_response(
            {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
             "user": {"id": "u1", "email": "e@x.com"}}
        ),
    )
    assert session_mod.exchange_code("code", "verifier")
    assert session_mod.session.signed_in
    assert session_mod.session.device_id == "dev-fp"
    assert session_mod.last_auth_error() == ""


def test_exchange_code_http_error_sets_message(fake_keyring, monkeypatch):
    monkeypatch.setattr(session_mod, "compute_device_fingerprint", lambda: "dev-fp")

    def _raise(req, timeout=0):
        raise _http_error(400)

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert not session_mod.exchange_code("code", "verifier")
    assert "400" in session_mod.last_auth_error()
    assert not session_mod.session.signed_in


# ── Refresh ──────────────────────────────────────────────────────────────

def test_refresh_4xx_is_definitive_rejection(fake_keyring, monkeypatch):
    session_mod.session.refresh_token = "r"

    def _raise(req, timeout=0):
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert not session_mod.refresh_access_token()
    assert session_mod.last_refresh_rejected()


def test_refresh_5xx_is_transient(fake_keyring, monkeypatch):
    session_mod.session.refresh_token = "r"

    def _raise(req, timeout=0):
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert not session_mod.refresh_access_token()
    assert not session_mod.last_refresh_rejected()


def test_refresh_network_error_is_transient(fake_keyring, monkeypatch):
    session_mod.session.refresh_token = "r"

    def _raise(req, timeout=0):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert not session_mod.refresh_access_token()
    assert not session_mod.last_refresh_rejected()


def test_refresh_success_rotates_tokens(fake_keyring, monkeypatch):
    session_mod.session.refresh_token = "old-refresh"
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=0: _ok_response(
            {"access_token": "new-access", "refresh_token": "new-refresh",
             "expires_in": 3600}
        ),
    )
    assert session_mod.refresh_access_token()
    assert session_mod.session.refresh_token == "new-refresh"
    assert fake_keyring.store[
        (session_mod.KEYRING_SERVICE, session_mod.KEYRING_REFRESH_TOKEN)
    ] == "new-refresh"


# ── Restore ──────────────────────────────────────────────────────────────

def test_restore_transient_failure_keeps_tokens(fake_keyring, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTORE_RETRY_DELAYS", (0.0, 0.0))
    session_mod.session.access_token = "a"
    session_mod.session.refresh_token = "r"

    invalid = threading.Event()
    monkeypatch.setattr(session_mod, "refresh_access_token", lambda: False)
    monkeypatch.setattr(session_mod, "last_refresh_rejected", lambda: False)

    assert session_mod.restore_session_async(on_invalid=invalid.set)
    assert invalid.wait(2.0)
    assert session_mod.session.refresh_token == "r"  # tokens survive


def test_restore_definitive_rejection_clears_tokens(fake_keyring, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTORE_RETRY_DELAYS", (0.0,))
    session_mod.session.access_token = "a"
    session_mod.session.refresh_token = "r"

    invalid = threading.Event()
    monkeypatch.setattr(session_mod, "refresh_access_token", lambda: False)
    monkeypatch.setattr(session_mod, "last_refresh_rejected", lambda: True)

    assert session_mod.restore_session_async(on_invalid=invalid.set)
    assert invalid.wait(2.0)
    assert session_mod.session.refresh_token == ""
    assert not session_mod.session.signed_in


def test_restore_success_calls_ready(fake_keyring, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTORE_RETRY_DELAYS", (0.0,))
    session_mod.session.refresh_token = "r"

    ready = threading.Event()

    def _refresh():
        session_mod.session.signed_in = True
        return True

    monkeypatch.setattr(session_mod, "refresh_access_token", _refresh)
    assert session_mod.restore_session_async(on_ready=ready.set)
    assert ready.wait(2.0)
    assert session_mod.session.signed_in


def test_restore_without_tokens_returns_false(fake_keyring):
    assert not session_mod.restore_session_async()


def test_restore_missing_refresh_token_signs_out(fake_keyring, monkeypatch):
    session_mod.session.access_token = "only-access"
    session_mod.session.refresh_token = ""

    invalid = threading.Event()
    assert session_mod.restore_session_async(on_invalid=invalid.set)
    assert invalid.wait(2.0)
    assert session_mod.session.access_token == ""


# ── Dev sign-in ──────────────────────────────────────────────────────────

def test_dev_signin_never_touches_keyring(fake_keyring):
    session_mod.dev_signin()
    assert session_mod.session.signed_in
    assert session_mod.session.access_token == "dev"
    assert fake_keyring.store == {}
