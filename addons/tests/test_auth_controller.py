"""Controller orchestration tests — bpy/state/ws_client stubbed, real
loopback servers on 127.0.0.1 for the attempt lifecycle."""

from __future__ import annotations

import contextlib
import importlib
import sys
import time
import types
import urllib.request

import pytest

# ── Stub environment ─────────────────────────────────────────────────────

class _TimersStub:
    def __init__(self):
        self.registered: list = []

    def register(self, fn, first_interval=0.0):
        self.registered.append(fn)
        with contextlib.suppress(Exception):
            fn()  # run once, synchronously (one-shot main-thread hops)

    def is_registered(self, fn):
        return False

    def unregister(self, fn):
        pass


class _StateStub(types.ModuleType):
    class AuthS:
        SIGNED_OUT = "signed_out"
        PENDING_BROWSER = "auth_pending_browser"
        EXCHANGING_CODE = "auth_exchanging_code"
        CONNECTING = "signed_in_connecting"
        CONNECTED = "signed_in_connected"
        FAILED = "signed_in_failed"

    def __init__(self):
        super().__init__("animora_panel.state")
        self.state = types.SimpleNamespace(auth_status=self.AuthS.SIGNED_OUT, auth_message="")
        self.history: list[tuple[str, str]] = []

    def set_auth_status(self, status, message=""):
        self.state.auth_status = status
        self.state.auth_message = message
        self.history.append((status, message))


class _WSClientStub(types.ModuleType):
    def __init__(self):
        super().__init__("animora_panel.ws_client")
        self.client = types.SimpleNamespace(
            connect=lambda **kw: self.calls.append(("connect", kw)),
            disconnect=lambda: self.calls.append(("disconnect", {})),
        )
        self.calls: list = []


@pytest.fixture()
def env(monkeypatch):
    """Import animora_panel.auth.controller against a stubbed Blender."""
    bpy = types.ModuleType("bpy")
    timers = _TimersStub()
    bpy.app = types.SimpleNamespace(timers=timers, background=False)
    monkeypatch.setitem(sys.modules, "bpy", bpy)

    state = _StateStub()
    monkeypatch.setitem(sys.modules, "animora_panel.state", state)

    ws = _WSClientStub()
    monkeypatch.setitem(sys.modules, "animora_panel.ws_client", ws)

    prefs_mod = types.ModuleType("animora_panel.preferences")
    prefs_mod.get_prefs = lambda: types.SimpleNamespace(
        effective_website_base=lambda: "https://animora.tech",
        effective_backend_url=lambda: "wss://backend/ws",
        dev_mode=False,
    )
    monkeypatch.setitem(sys.modules, "animora_panel.preferences", prefs_mod)

    # Patch the REAL onboarding module — controller resolves
    # `from .. import onboarding` through the package attribute, so a
    # sys.modules stub wouldn't be seen once the real module is loaded.
    onboarding = importlib.import_module("animora_panel.onboarding")
    gate_opens: list[int] = []
    monkeypatch.setattr(onboarding, "open_gate", lambda slide=0: gate_opens.append(slide))
    onboarding_recorder = types.SimpleNamespace(gate_opens=gate_opens)

    opened_urls: list[str] = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened_urls.append(url))

    sys.modules.pop("animora_panel.auth.controller", None)
    controller = importlib.import_module("animora_panel.auth.controller")

    yield types.SimpleNamespace(
        controller=controller, state=state, ws=ws, timers=timers,
        urls=opened_urls, onboarding=onboarding_recorder, bpy=bpy,
    )

    controller.cancel_sign_in()
    sys.modules.pop("animora_panel.auth.controller", None)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── rejection_action truth table ─────────────────────────────────────────

@pytest.mark.parametrize(
    ("pending", "in_flight", "has_refresh", "since_retry", "expected"),
    [
        (True, False, True, 999.0, "ignore"),     # browser attempt pending
        (False, True, True, 999.0, "ignore"),     # exchange in flight
        (True, True, False, 999.0, "ignore"),     # in-flight wins over no-token
        (False, False, True, 999.0, "retry"),     # refresh token + not throttled
        (False, False, True, 10.0, "sign_out"),   # throttled (< 60s since retry)
        (False, False, False, 999.0, "sign_out"),  # nothing to retry with
    ],
)
def test_rejection_action(env, pending, in_flight, has_refresh, since_retry, expected):
    now = 1000.0
    assert env.controller.rejection_action(
        pending_active=pending,
        in_flight=in_flight,
        has_refresh_token=has_refresh,
        last_retry_at=now - since_retry,
        now=now,
    ) == expected


# ── Attempt lifecycle ────────────────────────────────────────────────────

def test_begin_sign_in_opens_browser_with_loopback_redirect(env):
    assert env.controller.begin_sign_in() is None
    assert env.state.state.auth_status == env.state.AuthS.PENDING_BROWSER

    attempt = env.controller._attempt
    assert attempt is not None
    assert len(env.urls) == 1
    # The loopback redirect (double-encoded inside `next`) reaches the URL.
    from urllib.parse import quote
    expected = quote(quote(attempt.server.redirect_uri, safe=""), safe="")
    assert expected in env.urls[0]
    assert env.urls[0].startswith("https://animora.tech/signin?next=")


def test_second_sign_in_supersedes_first(env):
    env.controller.begin_sign_in()
    first = env.controller._attempt
    env.controller.begin_sign_in()
    second = env.controller._attempt
    assert first is not second
    assert first.server.closed
    assert not second.server.closed
    assert len(env.urls) == 2


def test_tick_timeout_fails_attempt(env):
    env.controller.begin_sign_in()
    env.controller._attempt.started_at = time.monotonic() - 9999.0
    assert env.controller._tick() is None  # unregisters itself
    assert env.controller._attempt is None
    assert env.state.state.auth_status == env.state.AuthS.FAILED
    assert "timed out" in env.state.state.auth_message.lower()


def test_callback_completes_exchange_and_connects(env, monkeypatch):
    from animora_panel.auth import session as session_mod
    monkeypatch.setattr(env.controller.session, "exchange_code", lambda c, v: True)
    session_mod.session.user_id = "user-1"
    session_mod.session.access_token = "tok"

    env.controller.begin_sign_in()
    attempt = env.controller._attempt
    urllib.request.urlopen(
        f"{attempt.server.redirect_uri}?code=the-code&state={attempt.state}", timeout=5
    )
    assert _wait_for(lambda: env.controller._tick() is None or env.controller._attempt is None)
    assert _wait_for(
        lambda: any(c[0] == "connect" for c in env.ws.calls), timeout=3.0
    ), env.state.history
    connect_kwargs = next(kw for name, kw in env.ws.calls if name == "connect")
    assert connect_kwargs["access_token"] == "tok"
    assert env.state.state.auth_status == env.state.AuthS.CONNECTING


def test_failed_exchange_signs_out_and_fails(env, monkeypatch):
    monkeypatch.setattr(env.controller.session, "exchange_code", lambda c, v: False)
    monkeypatch.setattr(env.controller.session, "last_auth_error", lambda: "HTTP 400: bad code")

    env.controller.begin_sign_in()
    attempt = env.controller._attempt
    urllib.request.urlopen(
        f"{attempt.server.redirect_uri}?code=bad&state={attempt.state}", timeout=5
    )
    _wait_for(lambda: env.controller._tick() is None or env.controller._attempt is None)
    assert _wait_for(
        lambda: env.state.state.auth_status == env.state.AuthS.FAILED, timeout=3.0
    ), env.state.history
    assert "400" in env.state.state.auth_message


def test_definitive_rejection_reopens_gate_at_signin_slide(env, monkeypatch):
    monkeypatch.setattr(env.controller.session, "restore_session_async", lambda **kw: False)
    from animora_panel.auth import session as session_mod
    session_mod.session.refresh_token = ""  # nothing to retry with

    env.controller._on_ws_auth_rejected("Session expired")
    assert env.state.state.auth_status == env.state.AuthS.FAILED
    assert env.onboarding.gate_opens == [2]


def test_rejection_ignored_while_attempt_pending(env):
    env.controller.begin_sign_in()
    before = env.state.state.auth_status
    env.controller._on_ws_auth_rejected("stale socket rejection")
    assert env.state.state.auth_status == before  # untouched
    assert env.onboarding.gate_opens == []
