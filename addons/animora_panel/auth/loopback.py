"""Loopback HTTP callback listener for browser sign-in (RFC 8252 §7.3).

The running app binds an ephemeral port on 127.0.0.1 and hands the website
`redirect_uri=http://127.0.0.1:{port}/auth/callback`. After the user signs
in, the browser navigates straight to this listener — no OS URL-scheme
registration, no second process, no drop files, no polling. This is the
same pattern VS Code, gcloud, and GitHub CLI use for desktop auth.

bpy-free and stdlib-only so the whole transport is unit-testable with real
sockets (addons/tests/test_auth_loopback.py). Threading contract:
- start() spawns a daemon serving thread; handlers run on worker threads.
- poll() is the ONLY read API and is safe to call from Blender's main
  thread (non-blocking queue read).
- cancel() may be called from any thread, any number of times.

SECURITY: the listener binds strictly to 127.0.0.1 (never 0.0.0.0, never a
hostname), accepts only the exact /auth/callback path, requires the CSRF
`state` to match in constant time, and delivers at most ONE result per
server instance. The one-time code it receives is useless without the PKCE
verifier, which never leaves the app.
"""

from __future__ import annotations

import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from .pkce import verify_state

log = logging.getLogger("animora.auth.loopback")

CALLBACK_PATH = "/auth/callback"

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Animora</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
        justify-content: center; font-family: 'Segoe UI', system-ui, sans-serif;
        background: linear-gradient(160deg, #0d0b1a 0%, #14102a 55%, #1a1533 100%);
        color: #e4e4f4; }}
  .card {{ text-align: center; padding: 48px 56px; border-radius: 16px;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(140,107,255,0.25);
          box-shadow: 0 24px 80px rgba(0,0,0,0.45); }}
  .mark {{ font-size: 15px; font-weight: 600; letter-spacing: 0.35em;
          text-transform: uppercase; color: #8c6bff; margin-bottom: 28px; }}
  h1 {{ font-size: 28px; font-weight: 650; margin: 0 0 12px; }}
  p  {{ font-size: 15px; line-height: 1.6; color: #a5a3c2; margin: 0; }}
</style></head><body>
<div class="card"><div class="mark">&#9670; Animora</div>
<h1>{heading}</h1><p>{body}</p></div>
</body></html>"""

SUCCESS_HTML = _PAGE_TEMPLATE.format(
    title="Signed in",
    heading="You're signed in",
    body="You can close this tab and return to Animora.",
)

STALE_HTML = _PAGE_TEMPLATE.format(
    title="Link expired",
    heading="This sign-in link is stale",
    body="Return to Animora and click Sign In to start a fresh attempt.",
)

INVALID_HTML = _PAGE_TEMPLATE.format(
    title="Invalid request",
    heading="Invalid sign-in callback",
    body="The sign-in confirmation was incomplete. Return to Animora and try again.",
)


class CallbackResult(NamedTuple):
    ok: bool
    code: str = ""
    error: str = ""  # "state_mismatch" | "bad_request"


class _CallbackHandler(BaseHTTPRequestHandler):
    # The owning LoopbackServer is attached to the server object (see start()).
    server: _OwnedHTTPServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug("loopback: " + format, *args)

    def _reply(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        owner = self.server.owner
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            # Browsers also request /favicon.ico after landing — never let a
            # side request consume the attempt or stop the server.
            self._reply(404, "")
            return

        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        got_state = (params.get("state") or [""])[0]

        if not code or not got_state:
            log.warning("Loopback callback missing code/state — ignored")
            self._reply(400, INVALID_HTML)
            return

        if not verify_state(owner.expected_state, got_state):
            # A stale tab from a previous attempt (or a CSRF probe). The live
            # attempt must keep listening — never fatal.
            log.warning("Loopback callback state mismatch — ignored")
            self._reply(400, STALE_HTML)
            return

        if owner.deliver(CallbackResult(ok=True, code=code)):
            self._reply(200, SUCCESS_HTML)
            # serve_forever deadlocks if shutdown() is called from a handler
            # thread — always hand it to a helper thread.
            threading.Thread(target=owner.cancel, daemon=True).start()
        else:
            # Duplicate GET (browser refresh/preload) after the one result
            # was already delivered.
            self._reply(200, SUCCESS_HTML)


class _OwnedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    owner: LoopbackServer


class LoopbackServer:
    """One-shot localhost callback receiver for a single sign-in attempt."""

    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self._server: _OwnedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._results: queue.Queue[CallbackResult] = queue.Queue(maxsize=1)
        self._delivered = threading.Event()
        self._closed = threading.Event()
        self._port = 0

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start(self) -> int:
        """Bind 127.0.0.1 on an ephemeral port and start serving. Returns
        the port. Raises OSError if the bind fails (caller surfaces it)."""
        server = _OwnedHTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.owner = self
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            daemon=True,
            name="animora-auth-loopback",
        )
        self._thread.start()
        log.info("Sign-in loopback listening on 127.0.0.1:%d", self._port)
        return self._port

    def cancel(self) -> None:
        """Stop the listener. Idempotent; callable from any thread."""
        if self._closed.is_set():
            return
        self._closed.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                log.debug("Loopback shutdown noise: %s", exc)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    # ── Data plane ───────────────────────────────────────────────────────
    def deliver(self, result: CallbackResult) -> bool:
        """Queue the single result. Returns False if one was already
        delivered (duplicate browser request)."""
        if self._delivered.is_set():
            return False
        self._delivered.set()
        try:
            self._results.put_nowait(result)
        except queue.Full:
            return False
        return True

    def poll(self) -> CallbackResult | None:
        """Non-blocking read of the callback result; None while waiting.
        Main-thread only (by convention — the queue itself is thread-safe)."""
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    # ── Introspection ────────────────────────────────────────────────────
    @property
    def port(self) -> int:
        return self._port

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._port}{CALLBACK_PATH}"

    @property
    def closed(self) -> bool:
        return self._closed.is_set()
