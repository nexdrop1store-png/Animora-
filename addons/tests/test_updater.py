"""Tests for the in-app update checker (animora_panel.updater).

bpy-free at module level (only launch_installer_and_quit imports bpy,
locally) — version comparison, the release-check HTTP call, and the
download/checksum-verify logic are all exercised here without a live
Blender. Network calls are mocked; nothing here hits the real network.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from animora_panel import updater

# ── parse_version / is_newer ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("v", "expected"),
    [
        ("1.3", (1, 3)),
        ("1.3.0", (1, 3, 0)),
        ("1.0.0", (1, 0, 0)),
        ("2", (2,)),
        ("1.a.0", (1, 0, 0)),  # malformed component coerces to 0, doesn't raise
        ("", (0,)),
    ],
)
def test_parse_version(v, expected):
    assert updater.parse_version(v) == expected


def test_is_newer_simple():
    assert updater.is_newer("1.3", "1.2") is True
    assert updater.is_newer("1.2", "1.3") is False
    assert updater.is_newer("1.3", "1.3") is False


def test_is_newer_pads_mismatched_lengths():
    # "1.3" vs "1.3.0" must compare equal, not mismatched.
    assert updater.is_newer("1.3", "1.3.0") is False
    assert updater.is_newer("1.3.0", "1.3") is False
    assert updater.is_newer("1.3.1", "1.3") is True


def test_is_newer_major_version_bump():
    assert updater.is_newer("2.0.0", "1.9.9") is True


def test_current_version_reads_bl_info():
    from animora_panel import bl_info
    assert updater.current_version() == ".".join(str(p) for p in bl_info["version"])


# ── update_available ─────────────────────────────────────────────────────


def test_update_available_none_release():
    assert updater.update_available(None) is False


def test_update_available_empty_version():
    assert updater.update_available({"version": ""}) is False


def test_update_available_true_when_remote_newer(monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.0")
    assert updater.update_available({"version": "1.3.0"}) is True


def test_update_available_false_when_same_or_older(monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.3.0")
    assert updater.update_available({"version": "1.3.0"}) is False
    assert updater.update_available({"version": "1.2.0"}) is False


# ── check_latest_release — best-effort, never raises ─────────────────────


def test_check_latest_release_returns_first_row():
    with patch.object(updater, "_http_get_json", return_value=[
        {"version": "1.3.0", "windows_url": "https://example.com/setup.exe",
         "windows_sha256": "abc123"},
    ]):
        release = updater.check_latest_release()
    assert release is not None
    assert release["version"] == "1.3.0"


def test_check_latest_release_empty_rows_returns_none():
    with patch.object(updater, "_http_get_json", return_value=[]):
        assert updater.check_latest_release() is None


def test_check_latest_release_swallows_network_exception():
    with patch.object(updater, "_http_get_json", side_effect=ConnectionError("offline")):
        # Must not raise — an update check can never break the addon.
        assert updater.check_latest_release() is None


# ── download_and_verify ───────────────────────────────────────────────────


def test_download_and_verify_refuses_without_checksum(tmp_path):
    result = updater.download_and_verify(
        "https://example.com/setup.exe", "", dest_dir=tmp_path,
    )
    assert result is None
    assert list(tmp_path.iterdir()) == []  # never even attempted a download


def test_download_and_verify_succeeds_on_matching_checksum(tmp_path):
    content = b"fake installer bytes"
    expected = hashlib.sha256(content).hexdigest()

    def _fake_download(url, dest_path):
        dest_path.write_bytes(content)

    with patch.object(updater, "_download_file", side_effect=_fake_download):
        result = updater.download_and_verify(
            "https://example.com/setup.exe", expected, dest_dir=tmp_path,
        )
    assert result is not None
    assert result.read_bytes() == content


def test_download_and_verify_rejects_and_deletes_on_mismatch(tmp_path):
    content = b"fake installer bytes"
    wrong_hash = hashlib.sha256(b"different content").hexdigest()

    def _fake_download(url, dest_path):
        dest_path.write_bytes(content)

    with patch.object(updater, "_download_file", side_effect=_fake_download):
        result = updater.download_and_verify(
            "https://example.com/setup.exe", wrong_hash, dest_dir=tmp_path,
        )
    assert result is None
    # The mismatched file must not be left behind for anything to
    # accidentally pick up later.
    assert list(tmp_path.iterdir()) == []


def test_download_and_verify_returns_none_on_download_failure(tmp_path):
    with patch.object(updater, "_download_file", side_effect=ConnectionError("network down")):
        result = updater.download_and_verify(
            "https://example.com/setup.exe", "somehash", dest_dir=tmp_path,
        )
    assert result is None


def test_download_and_verify_case_insensitive_hash_compare(tmp_path):
    content = b"fake installer bytes"
    expected = hashlib.sha256(content).hexdigest().upper()  # uppercase on purpose

    def _fake_download(url, dest_path):
        dest_path.write_bytes(content)

    with patch.object(updater, "_download_file", side_effect=_fake_download):
        result = updater.download_and_verify(
            "https://example.com/setup.exe", expected, dest_dir=tmp_path,
        )
    assert result is not None


# ── launch_installer_and_quit — platform guard ───────────────────────────


def test_launch_installer_skips_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    fake_installer = tmp_path / "setup.exe"
    fake_installer.write_bytes(b"x")
    assert updater.launch_installer_and_quit(fake_installer) is False


# ── Session-scoped cache ──────────────────────────────────────────────────


def test_get_cached_release_none_before_any_check(monkeypatch):
    monkeypatch.setattr(updater, "_cached_release", None)
    assert updater.get_cached_release() is None
    assert updater.is_update_pending() is False


def test_is_update_pending_reflects_cached_release(monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "_cached_release", {"version": "1.3.0"})
    assert updater.is_update_pending() is True

    monkeypatch.setattr(updater, "_cached_release", {"version": "0.9.0"})
    assert updater.is_update_pending() is False


def test_refresh_cache_async_updates_cache_via_callback(monkeypatch):
    monkeypatch.setattr(updater, "_cached_release", None)
    monkeypatch.setattr(updater, "_check_in_flight", False)

    # Run the background-thread wrapper synchronously for the test —
    # substitute check_for_update_async with an immediate callback.
    def _fake_check_async(on_result):
        on_result({"version": "9.9.9"})

    monkeypatch.setattr(updater, "check_for_update_async", _fake_check_async)
    updater.refresh_cache_async()
    assert updater.get_cached_release() == {"version": "9.9.9"}
    assert updater._check_in_flight is False


def test_refresh_cache_async_noop_while_in_flight(monkeypatch):
    monkeypatch.setattr(updater, "_check_in_flight", True)
    calls = []
    monkeypatch.setattr(updater, "check_for_update_async", lambda on_result: calls.append(1))
    updater.refresh_cache_async()
    assert calls == []  # never even started a new check
