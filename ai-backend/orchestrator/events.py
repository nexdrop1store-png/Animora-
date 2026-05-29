"""
In-process event bus.

Lets quality enforcement, memory, telemetry, and any future subscribers react
to orchestrator events without the orchestrator knowing about them. Zero
external dependencies — a defaultdict of callbacks with async-aware dispatch.

Events emitted by the orchestrator (Phase 1):
    message.received       payload: {session_id, user_id, text}
    model.selected         payload: {session_id, model, plan, reason}
    llm.stream_started     payload: {session_id, model}
    llm.stream_completed   payload: {session_id, model, output_text, ...}
    tool.dispatched        payload: {session_id, tool, tool_use_id, input}
    tool.rejected          payload: {session_id, tool_use_id, reason}

Later phases extend with:
    quality.passed / quality.failed (Phase 5)
    persona.switched (Phase 4)
    session.resumed / session.idle (Phase 7)

Listener callbacks may be sync or async — both are awaited safely.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Union

log = logging.getLogger("animora.events")

Listener = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]


class EventBus:
    """Minimal pub/sub. Single instance, used as a module-level singleton."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)

    def on(self, event: str, callback: Listener) -> None:
        """Subscribe a callback (sync or async) to an event."""
        self._listeners[event].append(callback)

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Fire an event. Failing listeners are logged but never raise."""
        for cb in self._listeners[event]:
            try:
                result = cb(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log.warning("Listener for %r failed: %s", event, exc, exc_info=True)

    def emit_nowait(self, event: str, payload: dict[str, Any]) -> None:
        """Fire an event from a sync context; schedules async listeners on the
        running loop. Use sparingly — prefer `await emit(...)`."""
        loop = asyncio.get_event_loop()
        loop.create_task(self.emit(event, payload))


bus = EventBus()
