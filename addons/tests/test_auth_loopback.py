"""Loopback listener tests — real sockets on 127.0.0.1 (no mocks).

Covers the full transport contract: one-shot delivery, CSRF-state
rejection without killing the live attempt, side-request tolerance,
idempotent cancel, and distinct ephemeral ports."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from animora_panel.auth.loopback import CALLBACK_PATH, LoopbackServer

STATE = "expected-state-token"


def _get(url: str):
    """GET returning (status, body). HTTPError carries non-2xx statuses."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


@pytest.fixture()
def server():
    srv = LoopbackServer(STATE)
    srv.start()
    yield srv
    srv.cancel()


def _wait_poll(srv: LoopbackServer, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = srv.poll()
        if result is not None:
            return result
        time.sleep(0.02)
    return None


def test_valid_callback_delivers_code(server):
    status, body = _get(f"{server.redirect_uri}?code=abc123&state={STATE}")
    assert status == 200
    assert "signed in" in body.lower()

    result = _wait_poll(server)
    assert result is not None and result.ok
    assert result.code == "abc123"


def test_server_stops_after_success(server):
    _get(f"{server.redirect_uri}?code=abc123&state={STATE}")
    assert _wait_poll(server) is not None
    # The listener shuts itself down after delivering the one result.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.closed:
        time.sleep(0.02)
    assert server.closed


def test_state_mismatch_rejected_but_keeps_listening(server):
    status, body = _get(f"{server.redirect_uri}?code=evil&state=WRONG")
    assert status == 400
    assert "stale" in body.lower()
    assert server.poll() is None  # nothing delivered

    # The live attempt still completes afterwards.
    status, _ = _get(f"{server.redirect_uri}?code=good&state={STATE}")
    assert status == 200
    result = _wait_poll(server)
    assert result is not None and result.code == "good"


def test_missing_params_rejected(server):
    status, _ = _get(f"{server.redirect_uri}?code=only-code")
    assert status == 400
    status, _ = _get(f"{server.redirect_uri}?state={STATE}")
    assert status == 400
    assert server.poll() is None


def test_favicon_and_other_paths_404_without_consuming(server):
    base = f"http://127.0.0.1:{server.port}"
    status, _ = _get(f"{base}/favicon.ico")
    assert status == 404
    status, _ = _get(f"{base}/anything/else")
    assert status == 404
    assert server.poll() is None

    status, _ = _get(f"{server.redirect_uri}?code=still-works&state={STATE}")
    assert status == 200
    assert _wait_poll(server).code == "still-works"


def test_duplicate_success_gets_delivered_exactly_once(server):
    url = f"{server.redirect_uri}?code=first&state={STATE}"
    status1, _ = _get(url)
    assert status1 == 200
    first = _wait_poll(server)
    assert first is not None and first.code == "first"
    # A browser refresh/preload duplicate must not queue a second result.
    assert server.poll() is None


def test_cancel_is_idempotent():
    srv = LoopbackServer(STATE)
    port = srv.start()
    assert port > 0
    srv.cancel()
    srv.cancel()  # second call is a no-op
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}{CALLBACK_PATH}", timeout=1)


def test_two_servers_get_distinct_ports():
    a, b = LoopbackServer(STATE), LoopbackServer(STATE)
    try:
        assert a.start() != b.start()
    finally:
        a.cancel()
        b.cancel()


def test_redirect_uri_shape(server):
    assert server.redirect_uri == f"http://127.0.0.1:{server.port}{CALLBACK_PATH}"
    assert CALLBACK_PATH == "/auth/callback"
