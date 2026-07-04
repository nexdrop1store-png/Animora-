from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path


_PKG = Path(__file__).resolve().parent.parent / "animora_panel"


def _load_auth_module():
    sys.modules.setdefault("bpy", types.ModuleType("bpy"))

    pkg = types.ModuleType("animora_panel")
    pkg.__path__ = [str(_PKG)]  # type: ignore[attr-defined]
    sys.modules["animora_panel"] = pkg

    core_spec = importlib.util.spec_from_file_location(
        "animora_panel.auth_core",
        _PKG / "auth_core.py",
    )
    core_mod = importlib.util.module_from_spec(core_spec)  # type: ignore[arg-type]
    sys.modules["animora_panel.auth_core"] = core_mod
    core_spec.loader.exec_module(core_mod)  # type: ignore[union-attr]

    prefs_mod = types.ModuleType("animora_panel.preferences")

    class _Prefs:
        def effective_auth_url(self):
            return "https://auth.animora.tech"

    prefs_mod.get_prefs = lambda: _Prefs()
    sys.modules["animora_panel.preferences"] = prefs_mod

    auth_spec = importlib.util.spec_from_file_location(
        "animora_panel.auth",
        _PKG / "auth.py",
    )
    auth_mod = importlib.util.module_from_spec(auth_spec)  # type: ignore[arg-type]
    sys.modules["animora_panel.auth"] = auth_mod
    auth_spec.loader.exec_module(auth_mod)  # type: ignore[union-attr]
    return auth_mod


def test_restore_session_async_success(monkeypatch):
    auth = _load_auth_module()
    auth.session.access_token = "access"
    auth.session.refresh_token = "refresh"
    auth.session.signed_in = False

    ready = threading.Event()
    invalid = threading.Event()

    def _refresh():
        auth.session.signed_in = True
        return True

    monkeypatch.setattr(auth, "refresh_access_token", _refresh)

    assert auth.restore_session_async(
        on_ready=ready.set,
        on_invalid=invalid.set,
    )
    assert ready.wait(2.0)
    assert not invalid.is_set()
    assert auth.session.signed_in is True


def test_restore_session_async_invalidates_rejected_refresh(monkeypatch):
    auth = _load_auth_module()
    auth.session.access_token = "access"
    auth.session.refresh_token = "refresh"
    auth.session.signed_in = False

    invalid = threading.Event()
    monkeypatch.setattr(auth, "refresh_access_token", lambda: False)
    monkeypatch.setattr(auth, "last_refresh_rejected", lambda: True)

    assert auth.restore_session_async(on_invalid=invalid.set)
    assert invalid.wait(2.0)
    assert auth.session.access_token == ""
    assert auth.session.refresh_token == ""
    assert auth.session.signed_in is False


def test_restore_session_async_keeps_tokens_on_transient_failure(monkeypatch):
    """Offline at launch must NOT wipe the refresh token — only a definitive
    rejection by the auth providers may sign the user out."""
    auth = _load_auth_module()
    auth.session.access_token = "access"
    auth.session.refresh_token = "refresh"
    auth.session.signed_in = False

    invalid = threading.Event()
    monkeypatch.setattr(auth, "_RESTORE_RETRY_DELAYS", (0.0, 0.01, 0.01))
    monkeypatch.setattr(auth, "refresh_access_token", lambda: False)
    monkeypatch.setattr(auth, "last_refresh_rejected", lambda: False)

    assert auth.restore_session_async(on_invalid=invalid.set)
    assert invalid.wait(2.0)
    assert auth.session.refresh_token == "refresh"
    assert auth.session.signed_in is False


def test_refresh_rejection_flag(monkeypatch):
    """last_refresh_rejected: True only when every provider answered 4xx."""
    import io
    import urllib.error

    auth = _load_auth_module()
    auth.session.refresh_token = "refresh"

    def _raise_401(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", _raise_401)
    assert auth.refresh_access_token() is False
    assert auth.last_refresh_rejected() is True

    def _raise_unreachable(req, timeout=0):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", _raise_unreachable)
    assert auth.refresh_access_token() is False
    assert auth.last_refresh_rejected() is False


def test_apply_session_preserves_trial_end():
    auth = _load_auth_module()
    auth._apply_session(  # noqa: SLF001 - targeted unit test
        {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_at": 1780000000.0,
            "user_id": "u1",
            "email": "u@example.com",
            "plan": "trial",
            "trial_end": 1779999999.0,
        }
    )
    assert auth.session.trial_end == 1779999999.0


def test_save_tokens_keeps_large_access_token_in_memory(monkeypatch):
    auth = _load_auth_module()

    calls = []

    class _Keyring:
        def set_password(self, service, key, value):
            calls.append(("set", service, key, value))

        def delete_password(self, service, key):
            calls.append(("delete", service, key))

    monkeypatch.setattr(auth, "_keyring_available", lambda: True)
    monkeypatch.setitem(sys.modules, "keyring", _Keyring())

    auth.save_tokens("A" * 1024, "refresh-token")

    assert auth.session.access_token == "A" * 1024
    assert auth.session.refresh_token == "refresh-token"
    assert ("set", auth.KEYRING_SERVICE, auth.KEYRING_REFRESH_TOKEN, "refresh-token") in calls
    assert ("delete", auth.KEYRING_SERVICE, auth.KEYRING_ACCESS_TOKEN) in calls


def test_register_restores_from_refresh_token_only(monkeypatch):
    auth = _load_auth_module()

    monkeypatch.setattr(auth, "load_tokens", lambda: ("", "refresh-only"))
    started = []
    monkeypatch.setattr(auth, "start_refresh_thread", lambda: started.append(True))

    auth.register()

    assert auth.session.access_token == ""
    assert auth.session.refresh_token == "refresh-only"
    assert auth.session.signed_in is False
    assert started == [True]


def test_apply_session_tolerates_email_keyring_write_failure(monkeypatch):
    auth = _load_auth_module()

    class _Keyring:
        def set_password(self, service, key, value):
            if key == auth.KEYRING_USER_EMAIL:
                raise RuntimeError("CredWrite failed")

        def delete_password(self, service, key):
            return None

        def get_password(self, service, key):
            return ""

    monkeypatch.setattr(auth, "_keyring_available", lambda: True)
    monkeypatch.setitem(sys.modules, "keyring", _Keyring())

    auth._apply_session(  # noqa: SLF001 - targeted unit test
        {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_at": 1780000000.0,
            "user_id": "u1",
            "email": "u@example.com",
            "plan": "free",
            "trial_end": None,
        }
    )

    assert auth.session.signed_in is True
    assert auth.session.email == "u@example.com"
