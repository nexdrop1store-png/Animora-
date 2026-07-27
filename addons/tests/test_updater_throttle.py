"""
v1.4.2 regression guard — the update check must never storm the network, and
must never run while the user is signing in.

v1.4.1 moved the update notice into the STATUS BAR and called
refresh_cache_async() from that draw handler. Two things made that fatal:

  1. The status bar draws constantly, and unlike the AI panel (whose poll()
     is `not onboarding.gate_active()`) it ALSO draws over the fullscreen
     onboarding gate. So the check ran from the instant the app opened.
  2. refresh_cache_async() had only an IN-FLIGHT guard, no interval throttle.
     That stops concurrent checks but not sequential ones — the moment a
     check resolved, the next redraw started another.

Result: a continuous stream of requests to the SAME Supabase project the
sign-in device-handoff uses, and users got stuck on "Connecting to Animora".

These tests pin the fix: a hard interval throttle, and no checks during the
gate. updater.py imports bpy only indirectly, so it loads with light stubs.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_ADDON = Path(__file__).resolve().parent.parent / "animora_panel"


def _load_updater(gate_active: bool = False):
    """Load updater.py with the bpy/addon surface stubbed out."""
    sys.modules["bpy"] = types.ModuleType("bpy")
    pkg = types.ModuleType("animora_panel")
    pkg.__path__ = [str(_ADDON)]
    pkg.bl_info = {"version": (1, 4, 2)}
    sys.modules["animora_panel"] = pkg

    authpkg = types.ModuleType("animora_panel.auth")
    authpkg.__path__ = []
    sup = types.ModuleType("animora_panel.auth.supabase")
    sup.SUPABASE_URL = "https://example.invalid"
    sup.SUPABASE_PUBLISHABLE_KEY = "test-key"
    sys.modules["animora_panel.auth"] = authpkg
    sys.modules["animora_panel.auth.supabase"] = sup

    onboarding = types.ModuleType("animora_panel.onboarding")
    onboarding.gate_active = lambda: gate_active
    sys.modules["animora_panel.onboarding"] = onboarding

    spec = importlib.util.spec_from_file_location(
        "animora_panel.updater", _ADDON / "updater.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["animora_panel.updater"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _count_checks(mod, times: int) -> int:
    """Drive refresh_cache_async `times` times with an instantly-resolving
    network layer (the worst case for a missing throttle)."""
    calls = {"n": 0}

    def _fake(cb):
        calls["n"] += 1
        cb({"version": "9.9.9"})  # resolves immediately

    mod.check_for_update_async = _fake
    for _ in range(times):
        mod.refresh_cache_async()
    return calls["n"]


def test_repeated_calls_do_not_storm_the_network():
    """500 redraws must produce ONE check, not 500."""
    mod = _load_updater(gate_active=False)
    assert _count_checks(mod, 500) == 1


def test_no_check_while_signing_in():
    """The gate is up = the user is signing in. An update check is never
    worth competing with the one flow they cannot proceed without."""
    mod = _load_updater(gate_active=True)
    assert _count_checks(mod, 50) == 0


def test_check_resumes_once_the_interval_elapses():
    """The throttle must expire, or users would never see a new release."""
    mod = _load_updater(gate_active=False)
    assert _count_checks(mod, 5) == 1
    # Pretend the interval has passed.
    mod._last_check_at -= mod._CHECK_INTERVAL_SEC + 1
    assert _count_checks(mod, 5) == 1  # one more, not five


def test_interval_is_sane():
    mod = _load_updater()
    assert mod._CHECK_INTERVAL_SEC >= 3600, "update checks should be hourly at most"


def test_statusbar_draw_handler_does_no_io():
    """A draw handler must never trigger network I/O — that is what broke
    sign-in. The status-bar notice must only READ the cache."""
    # Checked across the WHOLE panel module, not just the status-bar function:
    # v1.4.1's outage came from ONE such call, and an orphaned copy of the old
    # in-panel banner sat in this file still containing it. Any draw code here
    # doing update I/O is the bug, wherever it lives.
    panel_src = (_ADDON / "panel.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in panel_src.splitlines()
        if not line.strip().startswith("#")
    )
    assert "refresh_cache_async(" not in code, (
        "panel.py must not kick off update checks from draw code — panels and "
        "the status bar redraw constantly, and the status bar also draws over "
        "the sign-in gate. The timer in operators.register() owns refreshing."
    )


def test_update_check_timer_is_repeating():
    """With the draw handler read-only, the timer is the ONLY refresh path;
    a one-shot at 5s would land during the gate and never retry."""
    ops_src = (_ADDON / "operators.py").read_text(encoding="utf-8")
    start = ops_src.index("def _deferred_update_check")
    body = ops_src[start:start + 700]
    assert "return None" not in body, "update-check timer must re-arm, not be one-shot"
