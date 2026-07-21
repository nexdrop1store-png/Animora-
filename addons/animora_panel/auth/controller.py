"""Sign-in orchestration: the single place that drives auth state.

Owns the ONE in-flight browser sign-in attempt (PKCE + loopback listener),
the code exchange, WS connect/reconnect glue, and the reaction to WS auth
rejections. UI (onboarding gate, panel) only reads state.AuthS and invokes
the operators that call into here.

Threading model: everything public here runs on Blender's main thread;
network work (exchange/refresh/restore) runs on daemon threads that hop
back via one-shot bpy timers.
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from dataclasses import dataclass

import bpy

from .. import state, ws_client
from ..preferences import get_prefs
from . import loopback, pkce, session, supabase

log = logging.getLogger("animora.auth")

# One pending browser attempt at a time — a second Sign In click supersedes
# (and cancels) the first. Matches the loopback design: each attempt owns
# its own listener/port/state, so there is nothing to multiplex.
SIGNIN_TIMEOUT_SEC = 300.0  # matches the server-side 5-min code expiry
_TICK_INTERVAL = 0.5
_RESTORE_RETRY_INTERVAL = 60.0


@dataclass
class _Attempt:
    state: str
    verifier: str
    server: loopback.LoopbackServer
    started_at: float


_attempt: _Attempt | None = None

# Timestamp of the last automatic refresh-and-retry after a WS 401/403.
# Guards against a refresh/connect/reject loop when the backend keeps
# rejecting a token the auth stack keeps refreshing successfully.
_last_auth_retry_at: float = 0.0


def rejection_action(
    *,
    pending_active: bool,
    in_flight: bool,
    has_refresh_token: bool,
    last_retry_at: float,
    now: float,
    retry_interval: float = 60.0,
) -> str:
    """Decide the response to a WS auth rejection (HTTP 401/403 handshake).

    Returns one of:
      "ignore"   — a browser sign-in is in flight; the rejection is from the
                   previous session's socket and must not touch it.
      "retry"    — attempt ONE silent refresh → reconnect (access token may
                   simply have expired); throttled by `retry_interval`.
      "sign_out" — definitive: discard tokens and ask for a fresh sign-in.
    """
    if pending_active or in_flight:
        return "ignore"
    if has_refresh_token and now - last_retry_at > retry_interval:
        return "retry"
    return "sign_out"


def _run_on_main_thread(fn) -> None:
    """Schedule `fn` on Blender's main thread (bpy is not thread-safe)."""
    def _once():
        fn()
        return None  # returning None unregisters this one-shot timer
    bpy.app.timers.register(_once, first_interval=0.0)


# ---------------------------------------------------------------------------
# Sign-in attempt lifecycle
# ---------------------------------------------------------------------------

def begin_sign_in() -> str | None:
    """Start a browser sign-in. Returns a user-facing error message on
    immediate failure, else None. Main thread only."""
    cancel_sign_in()

    verifier, challenge = pkce.generate_pkce()
    signin_state = pkce.generate_state()

    server = loopback.LoopbackServer(signin_state)
    try:
        server.start()
    except OSError as exc:
        log.warning("Loopback bind failed (%s) — retrying once", exc)
        server = loopback.LoopbackServer(signin_state)
        try:
            server.start()
        except OSError as exc2:
            log.error("Loopback bind failed twice: %s", exc2)
            message = (
                "Couldn't start the local sign-in listener — a security "
                "product may be blocking loopback connections."
            )
            state.set_auth_status(state.AuthS.FAILED, message)
            return message

    global _attempt
    _attempt = _Attempt(
        state=signin_state,
        verifier=verifier,
        server=server,
        started_at=time.monotonic(),
    )
    state.set_auth_status(state.AuthS.PENDING_BROWSER, "Waiting for browser confirmation")

    url = supabase.build_signin_url(
        get_prefs().effective_website_base(),
        code_challenge=challenge,
        device_id=session.compute_device_fingerprint(),
        state=signin_state,
        device_label=supabase.device_label(),
        redirect_uri=server.redirect_uri,
    )
    log.info("Opening Animora sign-in in the browser (loopback port %d)", server.port)
    webbrowser.open(url)

    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=_TICK_INTERVAL)
    return None


def cancel_sign_in() -> None:
    """Drop the in-flight attempt (listener closed, verifier discarded).
    No status change — callers decide what the UI should say next."""
    global _attempt
    attempt, _attempt = _attempt, None
    if attempt is not None:
        attempt.server.cancel()


def _tick() -> float | None:
    """Poll the loopback listener for the callback; enforce the timeout.
    Registered only while an attempt is in flight."""
    attempt = _attempt
    if attempt is None:
        return None  # unregister
    try:
        result = attempt.server.poll()
        if result is not None and result.ok:
            _complete_attempt(attempt, result.code)
            return None
        if time.monotonic() - attempt.started_at > SIGNIN_TIMEOUT_SEC:
            log.warning("Sign-in timed out waiting for the browser callback")
            cancel_sign_in()
            state.set_auth_status(
                state.AuthS.FAILED,
                "Sign-in timed out — click Sign In to try again.",
            )
            return None
    except Exception as exc:  # a timer must never raise
        log.debug("Sign-in tick error: %s", exc)
    return _TICK_INTERVAL


def _complete_attempt(attempt: _Attempt, code: str) -> None:
    """State already verified by the listener; exchange the code off-thread."""
    global _attempt
    _attempt = None  # single-use — a duplicate callback can't re-enter
    state.set_auth_status(state.AuthS.EXCHANGING_CODE, "Signing you in")

    def _exchange() -> None:
        if session.exchange_code(code, attempt.verifier):
            _run_on_main_thread(connect_ws)
        else:
            session.sign_out()
            message = session.last_auth_error() or "Sign-in failed. Please try again."
            _run_on_main_thread(
                lambda: state.set_auth_status(state.AuthS.FAILED, message)
            )

    threading.Thread(target=_exchange, daemon=True, name="animora-auth-exchange").start()


# ---------------------------------------------------------------------------
# Dev connect + sign-out
# ---------------------------------------------------------------------------

def dev_connect() -> None:
    """Local-dev / recording-bundle path: synthetic session, straight to WS."""
    session.dev_signin()
    connect_ws()


def sign_out() -> None:
    """Explicit user sign-out: drop everything, back to SIGNED_OUT."""
    ws_client.client.disconnect()
    cancel_sign_in()
    session.sign_out()
    state.set_auth_status(state.AuthS.SIGNED_OUT, "")


# ---------------------------------------------------------------------------
# WebSocket glue
# ---------------------------------------------------------------------------

def connect_ws() -> None:
    import uuid
    session_id = session.session.user_id or str(uuid.uuid4())
    state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
    ws_client.client.connect(
        url=get_prefs().effective_backend_url(),
        session_id=session_id,
        access_token=session.session.access_token,
    )


def _on_ws_connecting() -> None:
    if session.has_restorable_session() or session.session.signed_in:
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")


def _on_ws_connected() -> None:
    state.set_auth_status(state.AuthS.CONNECTED, "")


def _on_ws_transport_disconnected(message: str) -> None:
    if session.session.signed_in or session.has_restorable_session():
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
    if message:
        log.warning("WS transport disconnected: %s", message)


def _on_ws_auth_rejected(message: str) -> None:
    global _last_auth_retry_at
    log.warning("WS auth rejected: %s", message)

    action = rejection_action(
        pending_active=_attempt is not None,
        in_flight=state.state.auth_status in (
            state.AuthS.PENDING_BROWSER, state.AuthS.EXCHANGING_CODE,
        ),
        has_refresh_token=bool(session.session.refresh_token),
        last_retry_at=_last_auth_retry_at,
        now=time.monotonic(),
    )

    if action == "ignore":
        # A browser sign-in is mid-flight: this rejection came from the
        # PREVIOUS session's socket and must not touch the live attempt.
        log.info("Ignoring WS auth rejection — a browser sign-in is in flight")
        return

    if action == "retry":
        # The access token may simply have expired (e.g. laptop asleep past
        # the refresh window). Try one silent refresh before discarding the
        # refresh token and forcing a full browser sign-in.
        _last_auth_retry_at = time.monotonic()
        state.set_auth_status(state.AuthS.CONNECTING, "Refreshing your session")
        if session.restore_session_async(
            on_ready=lambda: _run_on_main_thread(connect_ws),
            on_invalid=lambda: _run_on_main_thread(_on_restore_invalid),
        ):
            return

    _definitive_sign_out(message or "Session expired — please sign in again.")


def _definitive_sign_out(message: str) -> None:
    """The session is unrecoverable: clear it and reopen the gate at the
    sign-in slide. The AI panel never shows a sign-in affordance."""
    session.sign_out()
    state.set_auth_status(state.AuthS.FAILED, message)
    from .. import onboarding  # local import — onboarding imports auth
    onboarding.open_gate()  # v1.1: only the sign-in slide (index 0) remains


def _on_restore_invalid() -> None:
    """A restore attempt (startup or post-rejection) came back negative."""
    if session.last_refresh_rejected():
        _definitive_sign_out("Session expired — please sign in again.")
        return
    # Transient (offline / server hiccup): the user WAS signed in, so keep
    # the gate closed and the tokens intact; retry quietly until the network
    # returns or the server definitively rejects us.
    state.set_auth_status(
        state.AuthS.CONNECTING,
        "Couldn't reach Animora — retrying in the background",
    )
    if not bpy.app.timers.is_registered(_restore_retry_tick):
        bpy.app.timers.register(_restore_retry_tick, first_interval=_RESTORE_RETRY_INTERVAL)


def _restore_retry_tick() -> float | None:
    """Quiet retry loop after a transient restore failure."""
    try:
        if session.session.signed_in or not session.has_restorable_session():
            return None  # recovered or signed out — stop
        session.restore_session_async(
            on_ready=lambda: _run_on_main_thread(connect_ws),
            on_invalid=lambda: _run_on_main_thread(_on_restore_retry_invalid),
        )
    except Exception as exc:  # a timer must never raise
        log.debug("Restore retry tick error: %s", exc)
    return _RESTORE_RETRY_INTERVAL


def _on_restore_retry_invalid() -> None:
    if session.last_refresh_rejected():
        _definitive_sign_out("Session expired — please sign in again.")


def _configure_ws_callbacks() -> None:
    ws_client.client.on_connecting = _on_ws_connecting
    ws_client.client.on_connected = _on_ws_connected
    ws_client.client.on_auth_rejected = _on_ws_auth_rejected
    ws_client.client.on_transport_disconnected = _on_ws_transport_disconnected
    ws_client.client.token_provider = lambda: session.session.access_token


# ---------------------------------------------------------------------------
# Blender registration
# ---------------------------------------------------------------------------

def register() -> None:
    if bpy.app.background:
        # Headless runs (CI --background checks, renders) must never read
        # the keyring or refresh the session: Supabase ROTATES the refresh
        # token on every refresh and revokes the whole session family on
        # reuse, so a concurrent refresh from a short-lived background
        # process silently signs the real app out.
        log.info("Background mode — skipping session restore and token refresh")
        return

    session.load_persisted()
    session.start_refresh_thread()
    _configure_ws_callbacks()

    if session.has_restorable_session():
        state.set_auth_status(state.AuthS.CONNECTING, "Connecting to Animora")
        session.restore_session_async(
            on_ready=lambda: _run_on_main_thread(connect_ws),
            on_invalid=lambda: _run_on_main_thread(_on_restore_invalid),
        )
    else:
        state.set_auth_status(state.AuthS.SIGNED_OUT, "")


def unregister() -> None:
    cancel_sign_in()
    session.stop_refresh_thread()
    for timer in (_tick, _restore_retry_tick):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
