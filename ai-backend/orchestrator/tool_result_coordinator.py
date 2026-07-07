"""
Tool-result coordination for the agentic multi-step loop.

Bridges the async gap between two parts of the same user turn:

  • streaming.py emits `tool_use` blocks to the WebSocket, then awaits the
    addon's response inside the loop before continuing the conversation
    with the model.
  • main.py receives `tool_result` WebSocket frames asynchronously from
    the addon and must signal the awaiting streaming.py without coupling
    the two modules directly.

The coordinator is **per-session**. It is owned by `main.py:websocket_endpoint`
and passed into `stream_response`. It is NOT a module-global (would race
across sessions), and NOT attached to AnthropicClient (wrong layer; the
client shouldn't know about WebSocket-level events).

Design choices (validated by the design review):

  • `asyncio.Future` per tool_use_id — one object holds both the signal
    and the result payload. Cleaner than `Event` + a separate result-stash
    dict.
  • `asyncio.gather(*futures)` for the await-all path — propagates
    cancellations and exceptions naturally; works with `asyncio.wait_for`
    for the overall timeout.
  • Race against a user-interrupt `asyncio.Event` so STOP from the panel
    bails the loop without waiting for the timeout.
  • Outcomes are plain dicts so the loop's tool_result-block builder can
    consume them without further unwrapping.

Lifecycle:
  1. Loop calls `register([id1, id2, ...])` before emitting the tool_use
     blocks to the addon.
  2. Loop calls `await_results([id1, id2, ...], timeout, cancel_event)`
     and the coroutine yields control to the event loop.
  3. As each tool_result arrives via WebSocket, `main.py` calls
     `resolve(id, outcome_dict)`. The corresponding Future is set.
  4. When all Futures resolve OR the timeout fires OR the cancel_event
     fires, `await_results` returns `dict[id → outcome]`. Missing ids
     get a synthetic timeout outcome.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("animora.coordinator")

# An "idle-aware" wait: a tool_use_id's clock resets every time the addon
# reports progress (note_progress), so a legitimately slow multi-step script
# isn't penalized for exceeding `timeout_sec` in wall-clock terms. Without
# this, a script juggling several small steps that together take >45s would
# trip the "addon didn't respond" notice even though it never stopped
# working — see streaming.py's quality_notice for the user-facing symptom
# this was causing. _HARD_CEILING_SEC is an absolute backstop independent of
# progress pings, so a script that pings forever still can't hang a turn.
_HARD_CEILING_SEC = 180.0
_POLL_SLICE_SEC = 5.0


class ToolResultCoordinator:
    """Per-session coordinator for tool_use → tool_result correlation."""

    def __init__(self, session_id: str = "unknown") -> None:
        self._session_id = session_id
        # Outstanding futures keyed by tool_use_id. A future is added by
        # register() and removed after it resolves (success, timeout, or
        # cancellation). Late tool_results for unknown ids are logged and
        # dropped (they probably belong to a previous turn).
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Stashed outcomes for ids that resolved BEFORE register was
        # called. This shouldn't happen normally — register is called
        # before tool_use is dispatched — but handles races gracefully.
        self._early_outcomes: dict[str, dict[str, Any]] = {}
        # monotonic() timestamp of the last sign of life for each pending
        # id — either registration or a note_progress() ping. Read by
        # await_results() to decide whether an id is genuinely stuck versus
        # still working.
        self._last_activity: dict[str, float] = {}

    # ── Producer-side (called from streaming.py loop) ──────────────────

    def register(self, tool_use_ids: list[str]) -> None:
        """Allocate futures for a set of tool_use ids. Call BEFORE emitting
        the tool_use blocks to the addon, so a fast response can't race
        and arrive before the future exists."""
        loop = asyncio.get_running_loop()
        for tid in tool_use_ids:
            if tid in self._futures:
                # Re-registration shouldn't happen — every tool_use id
                # from Anthropic is unique. Log and reset defensively.
                log.warning(
                    "coordinator.register.duplicate session=%s tool_use_id=%s — resetting",
                    self._session_id, tid,
                )
                if not self._futures[tid].done():
                    self._futures[tid].cancel()
            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            # Honor any outcome that landed before register
            if tid in self._early_outcomes:
                fut.set_result(self._early_outcomes.pop(tid))
            self._futures[tid] = fut
            self._last_activity[tid] = time.monotonic()

    async def await_results(
        self,
        tool_use_ids: list[str],
        *,
        timeout_sec: float = 180.0,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Wait for ALL ids to resolve. Returns dict[id → outcome].

        Outcomes for missing ids (timeout / cancellation / unknown id)
        are synthesised so the caller never sees a missing key — the
        loop can hand the model a coherent tool_result for every tool_use
        it emitted, even when the addon dropped some.
        """
        if not tool_use_ids:
            return {}

        # Build the list of futures to wait on. If any id was never
        # registered (programming error), insert a pre-resolved future
        # with a synthetic error so we don't hang forever.
        futures: list[asyncio.Future[dict[str, Any]]] = []
        for tid in tool_use_ids:
            fut = self._futures.get(tid)
            if fut is None:
                log.error(
                    "coordinator.await.unregistered session=%s tool_use_id=%s "
                    "— synthesising error",
                    self._session_id, tid,
                )
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                fut.set_result({
                    "tool_use_id": tid,
                    "is_error": True,
                    "output": "",
                    "error": "Internal: tool_use_id was not registered before await.",
                })
                self._futures[tid] = fut
            futures.append(fut)
            self._last_activity.setdefault(tid, time.monotonic())

        # Race three coroutines: the gather of futures, the cancel_event,
        # and the timeout. Whichever fires first wins.
        gather_task = asyncio.ensure_future(
            asyncio.gather(*futures, return_exceptions=True)
        )
        wait_set: set[asyncio.Future[Any]] = {gather_task}
        cancel_task: asyncio.Task | None = None
        if cancel_event is not None:
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            wait_set.add(cancel_task)

        started = time.monotonic()
        # Idle-aware wait: poll in short slices instead of one flat
        # `asyncio.wait(timeout=timeout_sec)`. After each slice with no
        # completion, only treat this as a timeout once EVERY still-pending
        # id has gone `timeout_sec` since its last activity — a note_progress()
        # ping on any one id keeps the whole await alive, up to the absolute
        # _HARD_CEILING_SEC backstop.
        hard_deadline = started + max(timeout_sec, _HARD_CEILING_SEC)
        done: set[asyncio.Future[Any]] = set()
        try:
            while True:
                remaining_hard = hard_deadline - time.monotonic()
                if remaining_hard <= 0:
                    break
                done, _pending = await asyncio.wait(
                    wait_set,
                    timeout=min(_POLL_SLICE_SEC, remaining_hard),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break  # gather_task or cancel_task completed
                now = time.monotonic()
                pending_ids = [
                    tid for tid, fut in zip(tool_use_ids, futures) if not fut.done()
                ]
                if pending_ids and all(
                    now - self._last_activity.get(tid, started) > timeout_sec
                    for tid in pending_ids
                ):
                    break  # every pending id has been idle past timeout_sec
        finally:
            # Whatever didn't win the race, cancel cleanly so it doesn't
            # leak. The gather_task cancellation propagates to the
            # individual futures only if THEY weren't already done.
            # Then await each cancelled task briefly so its CancelledError
            # is consumed — otherwise asyncio logs
            # "exception was never retrieved" noise on every timeout / cancel.
            for task in (gather_task, cancel_task):
                if task is None or task.done():
                    continue
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, BaseException):
                    pass

        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Decide why we returned and what to give back per id
        if cancel_task is not None and cancel_task in done:
            log.info(
                "coordinator.await.cancelled session=%s elapsed_ms=%d ids=%d",
                self._session_id, elapsed_ms, len(tool_use_ids),
            )
            return self._build_results_with_fallback(
                tool_use_ids, futures,
                fallback_error="User cancelled the turn before this tool completed.",
            )

        if gather_task in done:
            # Normal success path: collect results
            raw = gather_task.result()
            out: dict[str, dict[str, Any]] = {}
            for tid, item in zip(tool_use_ids, raw):
                if isinstance(item, BaseException):
                    log.warning(
                        "coordinator.future.exception session=%s tool_use_id=%s exc=%s",
                        self._session_id, tid, item,
                    )
                    out[tid] = {
                        "tool_use_id": tid,
                        "is_error": True,
                        "output": "",
                        "error": f"Internal error awaiting tool_result: {item}",
                    }
                else:
                    out[tid] = item
            log.info(
                "coordinator.await.complete session=%s elapsed_ms=%d ids=%d",
                self._session_id, elapsed_ms, len(tool_use_ids),
            )
            return out

        # Timeout
        log.warning(
            "coordinator.await.timeout session=%s timeout_sec=%.1f ids=%d",
            self._session_id, timeout_sec, len(tool_use_ids),
        )
        return self._build_results_with_fallback(
            tool_use_ids, futures,
            fallback_error=f"No tool_result from addon after {timeout_sec:.0f}s.",
        )

    # ── Consumer-side (called from main.py WebSocket handler) ──────────

    def resolve(self, tool_use_id: str, outcome: dict[str, Any]) -> bool:
        """Resolve the future for `tool_use_id`. Returns True if a future
        was waiting; False if the id wasn't registered (late or unknown
        result — outcome is stashed in case register lands shortly after,
        but typically dropped)."""
        fut = self._futures.get(tool_use_id)
        if fut is None:
            log.debug(
                "coordinator.resolve.unregistered session=%s tool_use_id=%s — stashing",
                self._session_id, tool_use_id,
            )
            self._early_outcomes[tool_use_id] = outcome
            return False
        if fut.done():
            log.warning(
                "coordinator.resolve.already_done session=%s tool_use_id=%s — dropping",
                self._session_id, tool_use_id,
            )
            return False
        fut.set_result(outcome)
        return True

    def note_progress(self, tool_use_id: str) -> None:
        """Bump the idle clock for a still-pending tool call. Called when
        the addon reports it's still working through a multi-step script
        (a `tool_progress` WS frame) — extends the effective wait past
        `timeout_sec` as long as forward progress continues, up to
        `_HARD_CEILING_SEC`. An id that never sends progress behaves
        exactly as before: a flat `timeout_sec` from registration."""
        fut = self._futures.get(tool_use_id)
        if fut is not None and not fut.done():
            self._last_activity[tool_use_id] = time.monotonic()
        else:
            log.debug(
                "coordinator.progress.unregistered session=%s tool_use_id=%s — ignoring",
                self._session_id, tool_use_id,
            )

    def clear(self) -> None:
        """Cancel any outstanding futures and reset state. Called on WS
        disconnect or before a new turn begins."""
        for tid, fut in list(self._futures.items()):
            if not fut.done():
                fut.cancel()
        self._futures.clear()
        self._early_outcomes.clear()
        self._last_activity.clear()

    # ── Internals ──────────────────────────────────────────────────────

    def _build_results_with_fallback(
        self,
        tool_use_ids: list[str],
        futures: list[asyncio.Future[dict[str, Any]]],
        *,
        fallback_error: str,
    ) -> dict[str, dict[str, Any]]:
        """For each id, return its real outcome if the future resolved,
        otherwise a synthetic error outcome with `fallback_error` as the
        explanation. Used for both timeout and cancel paths so the loop
        ALWAYS gets a complete dict[id → outcome]."""
        out: dict[str, dict[str, Any]] = {}
        for tid, fut in zip(tool_use_ids, futures):
            if fut.done() and not fut.cancelled():
                exc = fut.exception()
                if exc is None:
                    out[tid] = fut.result()
                    continue
            out[tid] = {
                "tool_use_id": tid,
                "is_error": True,
                "output": "",
                "error": fallback_error,
            }
        return out
