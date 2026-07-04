"""
Unit tests for the deep-link file hand-off (deep_link.py + the standalone
forwarder). Pure file I/O — no bpy, no OS scheme registration (that's
cofounder-tested on real machines).

Run:
    pytest addons/tests/test_deep_link.py -v
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "animora_panel"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _PKG / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


dl = _load("animora_deep_link", "deep_link.py")


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "auth_callback.txt"
    url = "animora://auth/callback?code=ONE&state=ST"
    dl.write_callback(url, target=target)
    assert dl.read_and_consume_callback(target=target) == url


def test_read_when_absent_returns_none(tmp_path):
    assert dl.read_and_consume_callback(target=tmp_path / "nope.txt") is None


def test_callback_is_single_use(tmp_path):
    target = tmp_path / "auth_callback.txt"
    dl.write_callback("animora://auth/callback?code=c&state=s", target=target)
    assert dl.read_and_consume_callback(target=target) is not None
    # consumed → file deleted → second read is empty
    assert dl.read_and_consume_callback(target=target) is None
    assert not target.exists()


def test_write_is_atomic_replace(tmp_path):
    # Overwriting an existing callback leaves exactly the new content (no
    # partial/merged file) and no leftover temp files.
    target = tmp_path / "auth_callback.txt"
    dl.write_callback("animora://auth/callback?code=OLD&state=1", target=target)
    dl.write_callback("animora://auth/callback?code=NEW&state=2", target=target)
    assert "NEW" in target.read_text()
    assert "OLD" not in target.read_text()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".cb_")]
    assert leftovers == []


def test_standalone_forwarder_writes_callback(tmp_path, monkeypatch):
    # Run animora_url_handler.py as the OS would: a fresh process with the
    # URL as argv[1]. It must drop the file under HOME/.animora/.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home()
    handler = _PKG / "animora_url_handler.py"
    url = "animora://auth/callback?code=FWD&state=ST"
    rc = subprocess.run([sys.executable, str(handler), url], timeout=20)
    assert rc.returncode == 0
    dropped = tmp_path / ".animora" / "auth_callback.txt"
    assert dropped.exists()
    assert dropped.read_text() == url


def test_forwarder_ignores_non_animora_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    handler = _PKG / "animora_url_handler.py"
    rc = subprocess.run([sys.executable, str(handler), "https://evil.example/x"], timeout=20)
    assert rc.returncode == 1
    assert not (tmp_path / ".animora" / "auth_callback.txt").exists()


def test_interp_prefix_prefers_animora_launcher(monkeypatch, tmp_path):
    handler = tmp_path / "animora_url_handler.py"
    handler.write_text("# noop\n", encoding="utf-8")
    launcher = tmp_path / "Animora-launcher.exe"
    launcher.write_text("", encoding="utf-8")

    monkeypatch.setattr(dl, "_handler_path", lambda: handler)
    monkeypatch.setattr(dl, "_find_animora_launcher", lambda: str(launcher))
    monkeypatch.setattr(dl, "_find_animora_binary", lambda: None)
    monkeypatch.setattr(dl, "_find_python", lambda: None)

    assert dl._interp_prefix() == [  # noqa: SLF001 - deliberate unit test of internal helper
        str(launcher),
        "--background",
        "--python",
        str(handler),
        "--",
    ]
