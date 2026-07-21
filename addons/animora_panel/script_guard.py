"""
v1.1 hang-mitigation primitives for the AST-split script runner.

bpy-free by design (same pattern as composer_buffer.py and
script_capture.py): operators.py's _ScriptRunner imports and calls
these; the tests in addons/tests exercise this module directly without
Blender, except heartbeat_path()'s bpy.utils.user_resource() lookup,
which falls back cleanly to Path.home() when bpy isn't importable (so
even that stays callable in a plain-Python test process).

Why this exists (see docs plan: v1.1 "Stop the bleeding"):
_ScriptRunner._tick() (operators.py) runs each statement via a bare
exec() on Blender's MAIN THREAD with no timeout or watchdog — a single
slow statement (dense boolean, runaway pure-Python loop) freezes the
whole app. bpy is not thread-safe outside the main thread, and a
blocking C-level bpy.ops call cannot be preempted from Python once
it's in flight — so this module is deliberately scoped to what's
actually achievable, not a claim that hangs are eliminated:

  1. step_has_density_sensitive_modifier_apply() — a cheap source-text
     pre-check operators.py uses before paying the cost of a live
     bpy.context scan for a BOOLEAN/SUBSURF/ARRAY modifier_apply on an
     over-ceiling mesh (the live poly-count check itself needs bpy.data
     and stays in operators.py — this module only has the string test).
  2. install_step_deadline_trace() — a sys.settrace-compatible trace
     function that raises StepTimeout the next time a traced LINE
     executes past a wall-clock deadline. This interrupts a runaway
     PURE-PYTHON loop between bytecode steps. It cannot interrupt a
     single blocking C call inside bpy.ops.* — only Python-level
     execution between such calls ever yields control back here.
  3. Heartbeat marker read/write/clear — a small JSON breadcrumb
     persisted before each statement so a freeze neither of the above
     catches is at least DIAGNOSABLE on next launch (the backend's own
     "still working" warning can't render while the main thread is
     genuinely wedged — it's marshaled via bpy.app.timers too).
"""

from __future__ import annotations

import json
import time as _time
from contextlib import suppress
from pathlib import Path

# Above this live polygon count, applying a BOOLEAN/SUBSURF/ARRAY
# modifier is refused rather than attempted — cost scales with vertex
# density and overlap complexity, and this is the one case
# quality_enforcer.py's static AST/regex pass explicitly can't
# estimate (it logs-only for bare BOOLEAN detection). Start
# conservative; loosen based on real false-positive reports.
BOOLEAN_POLY_CEILING = 300_000

# Modifier types whose apply cost also depends on runtime mesh density
# in a way a static pass can't estimate. BOOLEAN is the named/observed
# cause; SUBSURF/ARRAY are defense-in-depth (same cost shape).
DENSITY_SENSITIVE_MODIFIER_TYPES = ("BOOLEAN", "SUBSURF", "ARRAY")

# Wall-clock deadline for the sys.settrace soft-interrupt. Only fires
# between Python bytecode steps, so it catches a runaway pure-Python
# loop (e.g. nested iteration over bm.verts) — it does NOT bound a
# single blocking bpy.ops call, which never yields control back to the
# Python interpreter until it returns.
PURE_PYTHON_STEP_DEADLINE_SEC = 15.0

_HEARTBEAT_FILENAME = "last_script_heartbeat.json"


class StepTimeout(Exception):
    """Raised by the settrace soft-interrupt when a step's pure-Python
    execution exceeds PURE_PYTHON_STEP_DEADLINE_SEC. Named distinctly
    from the builtin TimeoutError (which implies an I/O/OS timeout) so
    logs and chat messages are unambiguous about the cause."""


def install_step_deadline_trace(deadline: float):
    """Return a sys.settrace-compatible global trace function that
    raises StepTimeout the next time a traced line executes past
    `deadline` (a time.monotonic() timestamp).

    Only affects frames created WHILE this trace function is active —
    caller is responsible for installing immediately before exec() and
    clearing (sys.settrace(None)) in a finally immediately after, so
    this never leaks into surrounding addon code.

    Self-disarming by design: the instant the deadline trips, the
    tracer calls sys.settrace(None) BEFORE raising. Without this, the
    deadline check would keep re-firing on every subsequent traced
    line — including the exception-unwind machinery of whatever
    catches StepTimeout, and even the caller's own `finally:
    sys.settrace(None)` line itself — because `now > deadline` stays
    true forever after it first trips. That cascades into the
    exception never cleanly propagating. Disarming first means it
    fires exactly once; the caller's own settrace(None) afterward is
    then just a harmless no-op."""
    import sys as _sys

    def _local_trace(frame, event, arg):
        if _time.monotonic() > deadline:
            _sys.settrace(None)
            raise StepTimeout(
                f"Statement exceeded the {PURE_PYTHON_STEP_DEADLINE_SEC:.0f}s "
                "pure-Python soft-interrupt deadline — likely a runaway loop."
            )
        return _local_trace

    def _global_trace(frame, event, arg):
        return _local_trace

    return _global_trace


def step_has_density_sensitive_modifier_apply(step_source: str) -> bool:
    """Cheap pre-check: does this step's source even mention a
    modifier_apply call alongside one of the density-sensitive
    modifier type names? Avoids paying the live bpy.context scan cost
    for the common case of a step that doesn't touch modifier_apply at
    all. Pure string test — the live poly-count check that actually
    resolves the target object needs bpy.data and lives in
    operators.py's _find_density_sensitive_offender()."""
    if "modifier_apply" not in step_source:
        return False
    return any(
        f"'{t}'" in step_source or f'"{t}"' in step_source
        for t in DENSITY_SENSITIVE_MODIFIER_TYPES
    )


def heartbeat_path() -> Path:
    """Where the per-statement watchdog marker lives — Blender's user
    config dir (same convention as credentials.py's keyring-fallback
    file), so it survives a force-kill and is checked on next launch.
    Falls back to ~/.animora when bpy isn't importable (plain-Python
    test process), matching credentials.py's own fallback pattern."""
    try:
        import bpy
        cfg = Path(bpy.utils.user_resource("CONFIG"))
    except Exception:
        cfg = Path.home() / ".animora"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg / _HEARTBEAT_FILENAME


def write_heartbeat(*, label: str, step: int, total: int, script_hash: str,
                     path: Path | None = None) -> None:
    """Best-effort: persist a breadcrumb before a statement executes.
    Never raises — a failed heartbeat write must not break a build."""
    try:
        p = path or heartbeat_path()
        p.write_text(
            json.dumps({
                "label": label, "step": step, "total": total,
                "script_hash": script_hash, "started_at": _time.time(),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_heartbeat(path: Path | None = None) -> None:
    """Best-effort: called on both success and failure finalize, so a
    CLEANLY-ended script leaves no stale marker for next launch to
    misreport as a crash."""
    try:
        p = path or heartbeat_path()
        if p.is_file():
            p.unlink()
    except Exception:
        pass


def check_and_clear_stale_heartbeat(path: Path | None = None) -> str | None:
    """If a heartbeat marker survived from a previous session — i.e.
    nothing cleared it, meaning that session never reached
    _finalize_success/_finalize_failure — build a human-readable
    diagnostic, delete the marker, and return the message. Returns
    None if there's nothing stale to report.

    This is the honest answer to "no feedback during a true main-
    thread freeze": real-time delivery is architecturally impossible
    while the thread is wedged (the addon's own bpy.app.timers can't
    fire), so the NEXT launch is made informative instead."""
    p = path or heartbeat_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = None
    finally:
        with suppress(Exception):
            p.unlink()
    if data is None:
        return None
    label = data.get("label", "an AI build")
    step = data.get("step", "?")
    total = data.get("total", "?")
    return (
        f"⚠ Animora's previous session appears to have been force-closed "
        f"while running: '{label}', step {step}/{total}. If this keeps "
        f"happening with similar prompts, please share this with support — "
        f"it usually means a single build step (often a dense mesh "
        f"operation) took too long and the app had to be closed."
    )
