"""
Regression — per-connection event-bus subscriptions must be removable, or a
process-wide singleton bus double-fires per-session handlers after a
same-session reconnect (this inflated the usage ledger 2x).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_EVENTS = Path(__file__).resolve().parent.parent / "orchestrator" / "events.py"


def _load():
    spec = importlib.util.spec_from_file_location("animora_events", _EVENTS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.asyncio
async def test_off_removes_listener():
    ev = _load()
    bus = ev.EventBus()
    hits = []
    cb = lambda p: hits.append(p)  # noqa: E731
    bus.on("x", cb)
    await bus.emit("x", {"n": 1})
    bus.off("x", cb)
    await bus.emit("x", {"n": 2})
    assert hits == [{"n": 1}]  # second emit not delivered


@pytest.mark.asyncio
async def test_reconnect_would_double_fire_without_off():
    # Two subscriptions of the SAME session's handler (simulating a reconnect
    # that never cleaned up) fire twice; removing both restores single-fire.
    ev = _load()
    bus = ev.EventBus()
    count = {"n": 0}

    def h1(_):
        count["n"] += 1

    def h2(_):
        count["n"] += 1

    bus.on("usage.recorded", h1)
    bus.on("usage.recorded", h2)
    await bus.emit("usage.recorded", {})
    assert count["n"] == 2  # the bug: recorded twice

    bus.off("usage.recorded", h1)
    await bus.emit("usage.recorded", {})
    assert count["n"] == 3  # only the surviving handler fires


def test_off_is_safe_when_absent():
    ev = _load()
    bus = ev.EventBus()
    bus.off("never", lambda p: None)  # must not raise
