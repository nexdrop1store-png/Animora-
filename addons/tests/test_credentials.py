"""
Security regression — the BYOK Anthropic key is NEVER written to disk in
plaintext. When no OS keyring is available, it is held in memory for the
session only (previously it was written as raw JSON, mislabeled "encrypted").

credentials.py imports bpy only inside a guarded function, so it loads in
plain Python.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

_MOD = Path(__file__).resolve().parent.parent / "animora_panel" / "credentials.py"


def _load():
    spec = importlib.util.spec_from_file_location("animora_credentials", _MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _FakeKeyring:
    """Minimal keyring stand-in backed by a dict."""
    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, user, value):
        self._store[(service, user)] = value

    def get_password(self, service, user):
        return self._store.get((service, user))

    def delete_password(self, service, user):
        self._store.pop((service, user), None)

    def get_keyring(self):
        return self  # for status_message()


def test_no_keyring_never_writes_plaintext(tmp_path):
    cr = _load()
    fake_file = tmp_path / "credentials.json"
    with mock.patch.object(cr, "_try_keyring", return_value=None), \
         mock.patch.object(cr, "_fallback_path", return_value=fake_file):
        cr.set_api_key("sk-ant-secret-123")
        # In memory, retrievable this session…
        assert cr.get_api_key() == "sk-ant-secret-123"
        # …but NOTHING written to disk.
        assert not fake_file.exists()


def test_keyring_roundtrip_and_no_file(tmp_path):
    cr = _load()
    fake_file = tmp_path / "credentials.json"
    kr = _FakeKeyring()
    with mock.patch.object(cr, "_try_keyring", return_value=kr), \
         mock.patch.object(cr, "_fallback_path", return_value=fake_file):
        cr.set_api_key("sk-ant-in-keyring")
        assert cr.get_api_key() == "sk-ant-in-keyring"
        assert not fake_file.exists()
        assert "keyring" in cr.status_message().lower()


def test_legacy_plaintext_file_is_read_then_migrated(tmp_path):
    cr = _load()
    fake_file = tmp_path / "credentials.json"
    fake_file.write_text('{"anthropic_api_key": "sk-ant-legacy"}', encoding="utf-8")
    kr = _FakeKeyring()
    cr._memory_key = None
    with mock.patch.object(cr, "_try_keyring", return_value=kr), \
         mock.patch.object(cr, "_fallback_path", return_value=fake_file):
        # Existing users aren't logged out…
        assert cr.get_api_key() == "sk-ant-legacy"
        # …and the plaintext file is migrated into the keyring + deleted.
        assert kr.get_password("Animora", "anthropic_api_key") == "sk-ant-legacy"
        assert not fake_file.exists()


def test_clear_removes_memory_and_legacy_file(tmp_path):
    cr = _load()
    fake_file = tmp_path / "credentials.json"
    fake_file.write_text('{"anthropic_api_key": "sk-ant-old"}', encoding="utf-8")
    with mock.patch.object(cr, "_try_keyring", return_value=None), \
         mock.patch.object(cr, "_fallback_path", return_value=fake_file):
        cr.set_api_key("sk-ant-temp")
        cr.clear_api_key()
        assert cr.get_api_key() is None
        assert not fake_file.exists()
