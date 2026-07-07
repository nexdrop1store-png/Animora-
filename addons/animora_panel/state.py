"""
Animora AI panel — session state machine.

A lightweight module-level state container that the panel reads on draw
and the operators / ws_client mutate as events arrive. Lives in this
module (not as bpy properties) so the state changes don't dirty the
.blend file and don't trigger save-prompts.

States and what each means:

  IDLE             Default. Show input field + (if no history) onboarding cards.
  SUBMITTING       User clicked Send; waiting for backend ACK / first event.
                   Brief — usually <500ms.
  THINKING         Backend is classifying intent (Haiku) + planning. Lasts
                   until first stream_token arrives. Typically 1-2s.
  STREAMING        Tokens are flowing in. The latest assistant message is
                   appending live. Lasts until the LLM stops + tool calls
                   are dispatched. Typically 2-10s.
  EXECUTING        A tool_call is in flight on the addon side (bpy script
                   running, render in progress). Shows the intent_summary
                   so the user knows what's being done.
  QUALITY_CHECK    Phase 5: artist's-eye check running on the post-script
                   HD capture. Typically 2-4s.
  COMPLETE         All turn-side activity is done. Brief — auto-fades to
                   IDLE after ~1.5s so the panel returns to ready.
  ERROR            Something failed. Shown until the user sends another
                   message or hits New.

Transitions are driven by ws_client message handlers + the send operator.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("animora.state")


# String constants — using str values (not Enum) so they're trivially
# comparable from Blender's UI draw context and serializable for logs.
class S:
    IDLE = "IDLE"
    SUBMITTING = "SUBMITTING"
    THINKING = "THINKING"
    STREAMING = "STREAMING"
    EXECUTING = "EXECUTING"
    QUALITY_CHECK = "QUALITY_CHECK"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class AuthS:
    SIGNED_OUT = "signed_out"
    PENDING_BROWSER = "auth_pending_browser"
    EXCHANGING_CODE = "auth_exchanging_code"
    CONNECTING = "signed_in_connecting"
    CONNECTED = "signed_in_connected"
    FAILED = "signed_in_failed"


# Set of states where the GPU border glow + animated dots should run.
ACTIVE_STATES = frozenset({
    S.SUBMITTING, S.THINKING, S.STREAMING, S.EXECUTING, S.QUALITY_CHECK,
})


@dataclass
class _State:
    """Singleton state container. Read by panel.py:_draw_status_pill."""

    current: str = S.IDLE
    message: str = ""
    """Sub-state detail. For EXECUTING this is the intent_summary from
    the tool call ('Adding palm tree cluster…'). For ERROR it's the
    user-visible error text."""

    tool_name: str = ""
    """The active tool's name (execute_blender_script / render_preview / etc).
    Shown as a chip in the status pill during EXECUTING."""

    entered_at: float = 0.0
    """time.monotonic() when this state was entered. Used for the
    COMPLETE → IDLE auto-fade and for showing elapsed time."""

    dot_tick: int = 0
    """0/1/2 — driven by a bpy timer at 400ms. The panel renders
    `'.' * dot_tick + ' ' * (3 - dot_tick)` after the status text so the
    indicator visibly animates."""

    quality_notice: dict | None = None
    """Last quality_notice payload from the server. Rendered inline as a
    soft warning card under the most recent assistant message."""

    last_tool_use_id: str = ""
    """The most recent tool_use_id we dispatched. Used to correlate
    tool_result arrivals back to a state transition."""

    auth_status: str = AuthS.SIGNED_OUT
    auth_message: str = ""
    auth_updated_at: float = 0.0


state = _State()


def set_state(new: str, message: str = "", tool_name: str = "") -> None:
    """Update the panel state. Cheap; just sets fields and tags a redraw."""
    if new == state.current and message == state.message:
        return
    log.debug("state %s → %s (%s)", state.current, new, message)
    state.current = new
    state.message = message
    state.tool_name = tool_name
    state.entered_at = time.monotonic()
    state.dot_tick = 0
    _redraw_animora_areas()


def set_quality_notice(payload: dict | None) -> None:
    """Stash the latest quality_notice for inline rendering. None clears it."""
    state.quality_notice = payload
    _redraw_animora_areas()


def set_auth_status(status: str, message: str = "") -> None:
    if status == state.auth_status and message == state.auth_message:
        return
    log.debug("auth_state %s -> %s (%s)", state.auth_status, status, message)
    state.auth_status = status
    state.auth_message = message
    state.auth_updated_at = time.monotonic()
    _redraw_animora_areas()


def auth_can_send() -> bool:
    return state.auth_status == AuthS.CONNECTED


def reset() -> None:
    """Hard reset to IDLE (used by New Conversation).

    Deliberately leaves auth_status untouched: starting a new conversation
    doesn't change whether the user is signed in / the WS is connected, and
    clearing it here would disable the send button until the next auth
    transition (sign-out has its own explicit set_auth_status call)."""
    state.current = S.IDLE
    state.message = ""
    state.tool_name = ""
    state.entered_at = time.monotonic()
    state.dot_tick = 0
    state.quality_notice = None
    state.last_tool_use_id = ""


def elapsed_ms() -> int:
    """Time since the current state was entered, in milliseconds."""
    return int((time.monotonic() - state.entered_at) * 1000)


def is_active() -> bool:
    """True when the AI is actively working — drives the GPU border pulse."""
    return state.current in ACTIVE_STATES


# ── Animated-dots + auto-fade timer ────────────────────────────────────

# 0.15 s heartbeat: the GPU chrome (border glow, composer outline, accent
# strip) eases on time.monotonic() but only renders when the region
# redraws — at the old 0.4 s tick the "breathing" looked like stutter.
# The dot indicator advances every _DOT_DIVIDER ticks to keep its
# familiar ~0.45 s cadence.
_TIMER_INTERVAL = 0.15
_DOT_DIVIDER = 3
_tick_count = 0
_COMPLETE_FADE_SEC = 1.5
# Watchdog: if a single active state lasts longer than this, the AI is
# probably stuck (network drop, backend crash, infinite-loop script).
# Force ERROR so the user isn't trapped on a frozen panel.
#
# Sized to comfortably exceed the backend's per-request LLM timeout
# (_PER_REQUEST_TIMEOUT_SEC = 600 s in ai-backend/anthropic_client.py)
# plus margin for tool dispatch + addon-side exec + HD capture. The old
# 90 s was a leftover from when max_tokens was 4096 / 16384 and real
# generations finished in under a minute — at 32 k tokens × Opus 4.7's
# ~30 tok/s a hero-asset stream legitimately runs 3-5 minutes.
#
# Trade-off: a genuinely stuck session (network drop) now takes 11 min
# to surface instead of 90 s. The COMPLETE → IDLE auto-fade still runs
# at 1.5 s so happy paths don't pay this cost; only ERROR-via-watchdog
# is slower, and that's an inherently rare path.
_ACTIVE_STATE_TIMEOUT_SEC = 660.0
_timer_registered = False

# Per-exec hang surfacing (H2). Operators.py sets `_exec_started_at` to
# time.monotonic() right before exec(); clears it after. If _tick sees
# this set + elapsed past the threshold, it surfaces a chat warning so
# the user knows the script is hung (without trying to forcibly kill
# the exec — that's unreliable in Python on Windows).
_EXEC_HANG_WARN_SEC = 60.0
_exec_started_at: float = 0.0
_exec_warn_fired: bool = False


def _redraw_animora_areas() -> None:
    """Force-redraw the ANIMORA editor area so the new state is visible."""
    try:
        import bpy
        if bpy.context.screen is None:
            return
        for area in bpy.context.screen.areas:
            if area.type == "ANIMORA":
                area.tag_redraw()
    except Exception:
        pass  # bpy may not be ready (running outside Blender for tests)


def _tick() -> float:
    """Bpy timer callback. Runs every _TIMER_INTERVAL seconds while the
    addon is registered. Three jobs:
      1. Advance the dot tick + redraw while in an ACTIVE state
      2. Auto-fade COMPLETE → IDLE after _COMPLETE_FADE_SEC
      3. Watchdog: ACTIVE state stuck longer than
         _ACTIVE_STATE_TIMEOUT_SEC → force ERROR (network drop, backend
         crash, runaway script). Prevents the panel from being stuck
         in "thinking..." forever.
    """
    global _exec_warn_fired, _tick_count
    _tick_count += 1
    if state.current in ACTIVE_STATES:
        elapsed = time.monotonic() - state.entered_at
        if elapsed > _ACTIVE_STATE_TIMEOUT_SEC:
            state.current = S.ERROR
            state.message = f"No response after {int(elapsed)}s — connection may have dropped."
            log.warning(
                "watchdog: state %s stuck for %.0fs — forcing ERROR",
                state.current, elapsed,
            )
            _redraw_animora_areas()
        else:
            if _tick_count % _DOT_DIVIDER == 0:
                state.dot_tick = (state.dot_tick + 1) % 4
            _redraw_animora_areas()
    elif state.current == S.COMPLETE:
        if time.monotonic() - state.entered_at > _COMPLETE_FADE_SEC:
            state.current = S.IDLE
            state.message = ""
            _redraw_animora_areas()

    # H2 — exec hang surfacing. If a script has been running > 60s, post
    # a one-time warning to chat so the user knows what's happening (and
    # can choose to force-quit Blender if it really is a runaway loop).
    if _exec_started_at > 0:
        exec_elapsed = time.monotonic() - _exec_started_at
        if exec_elapsed > _EXEC_HANG_WARN_SEC and not _exec_warn_fired:
            _exec_warn_fired = True
            log.warning("exec.hang: script running %.0fs — surfacing to user", exec_elapsed)
            try:
                import bpy
                wm = bpy.context.window_manager
                if hasattr(wm, "animora_chat_history"):
                    entry = wm.animora_chat_history.add()
                    entry.role = "assistant"
                    entry.content = (
                        f"⚠ The current script has been running for over {int(exec_elapsed)}s. "
                        f"This usually means a runaway loop. If the viewport is frozen, you may "
                        f"need to force-quit Blender. (Once exec returns, the panel will recover "
                        f"normally.)"
                    )
                    _redraw_animora_areas()
            except Exception as exc:
                log.debug("exec.hang.surface_failed: %s", exc)
    return _TIMER_INTERVAL


def mark_exec_started() -> None:
    """Called by operators.py just before exec(). H2 hang surfacing."""
    global _exec_started_at, _exec_warn_fired
    _exec_started_at = time.monotonic()
    _exec_warn_fired = False


def mark_exec_finished() -> None:
    """Called by operators.py after exec() returns (success or failure)."""
    global _exec_started_at, _exec_warn_fired
    _exec_started_at = 0.0
    _exec_warn_fired = False


def start_timer() -> None:
    global _timer_registered
    if _timer_registered:
        return
    try:
        import bpy
        bpy.app.timers.register(_tick, first_interval=_TIMER_INTERVAL, persistent=True)
        _timer_registered = True
    except Exception as exc:
        log.warning("state.start_timer failed: %s", exc)


def stop_timer() -> None:
    global _timer_registered
    if not _timer_registered:
        return
    try:
        import bpy
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
    except Exception:
        pass
    _timer_registered = False


def register() -> None:
    import bpy

    if bpy.app.background:
        return
    start_timer()


def unregister() -> None:
    stop_timer()
    reset()


# ── User-facing label resolution ───────────────────────────────────────

_STATE_LABELS = {
    S.IDLE: ("", "DOT"),
    S.SUBMITTING: ("Sending", "EXPORT"),
    S.THINKING: ("Animora is thinking", "OUTLINER_OB_LIGHT"),
    S.STREAMING: ("Composing response", "OUTLINER_OB_LIGHT"),
    S.EXECUTING: ("Working in the scene", "MODIFIER"),
    S.QUALITY_CHECK: ("Checking quality", "ZOOM_IN"),
    S.COMPLETE: ("Done", "CHECKMARK"),
    S.ERROR: ("Something went wrong", "ERROR"),
}


def label() -> tuple[str, str]:
    """Returns (text, icon) for the current state's status pill."""
    base, icon = _STATE_LABELS.get(state.current, ("", "DOT"))
    if not base:
        return "", icon
    if state.current in ACTIVE_STATES:
        dots = "." * state.dot_tick
        return f"{base}{dots}", icon
    return base, icon
