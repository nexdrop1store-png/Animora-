"""Pure-logic tests for the v1.1 hang-mitigation primitives
(animora_panel.script_guard) — bpy-free by design, same convention as
test_composer_buffer.py / test_script_capture.py."""

from __future__ import annotations

import sys
import time

import pytest

from animora_panel import script_guard

# ── step_has_density_sensitive_modifier_apply ────────────────────────────


def test_no_modifier_apply_mentioned_is_false():
    assert script_guard.step_has_density_sensitive_modifier_apply(
        "obj.location = (1, 2, 3)\n"
    ) is False


def test_modifier_apply_without_density_sensitive_type_is_false():
    # e.g. applying an ARMATURE or MIRROR modifier — not density-sensitive.
    script = (
        "mod = obj.modifiers.new('Mirror', 'MIRROR')\n"
        "bpy.ops.object.modifier_apply(modifier=mod.name)\n"
    )
    assert script_guard.step_has_density_sensitive_modifier_apply(script) is False


@pytest.mark.parametrize("mtype", ["BOOLEAN", "SUBSURF", "ARRAY"])
def test_modifier_apply_with_density_sensitive_type_is_true(mtype):
    script = (
        f"mod = obj.modifiers.new('X', '{mtype}')\n"
        "bpy.ops.object.modifier_apply(modifier=mod.name)\n"
    )
    assert script_guard.step_has_density_sensitive_modifier_apply(script) is True


def test_density_sensitive_type_without_modifier_apply_is_false():
    # Just creating the modifier (not applying it) isn't the hang risk —
    # the cost is in modifier_apply, not in adding it to the stack.
    script = "mod = obj.modifiers.new('Cut', 'BOOLEAN')\n"
    assert script_guard.step_has_density_sensitive_modifier_apply(script) is False


# ── settrace soft-interrupt ───────────────────────────────────────────────


def test_deadline_trace_interrupts_runaway_pure_python_loop():
    # sys.settrace only traces frames entered AFTER it's installed —
    # the currently-running frame is never retroactively traced. So
    # this mirrors production's actual usage (script_guard is always
    # armed immediately around an exec() call, which itself creates a
    # fresh frame for the executed code) rather than looping inline in
    # the test function's own already-running frame.
    code = compile("total = 0\nfor i in range(10_000_000):\n total += i\n", "<test>", "exec")
    deadline = time.monotonic() + 0.05
    tracer = script_guard.install_step_deadline_trace(deadline)
    try:
        sys.settrace(tracer)
        with pytest.raises(script_guard.StepTimeout):
            exec(code, {})
    finally:
        sys.settrace(None)


def test_deadline_trace_does_not_fire_before_deadline():
    code = compile("total = sum(range(1000))\n", "<test>", "exec")
    deadline = time.monotonic() + 30.0  # generous — must not trip
    tracer = script_guard.install_step_deadline_trace(deadline)
    ns: dict = {}
    try:
        sys.settrace(tracer)
        exec(code, ns)
    finally:
        sys.settrace(None)
    assert ns["total"] == sum(range(1000))


def test_settrace_cleared_after_use_does_not_leak():
    # Caller contract: sys.settrace(None) in a finally immediately after
    # exec(). Confirm that once cleared, subsequent code is untraced
    # (no lingering StepTimeout from a stale deadline).
    deadline = time.monotonic() + 0.01
    tracer = script_guard.install_step_deadline_trace(deadline)
    sys.settrace(tracer)
    sys.settrace(None)
    time.sleep(0.05)  # well past the (now-uninstalled) deadline
    # If trace were still active, this loop would raise StepTimeout.
    total = 0
    for i in range(1000):
        total += i
    assert total == sum(range(1000))


# ── heartbeat marker read/write/clear ─────────────────────────────────────


def test_write_then_check_stale_heartbeat_returns_diagnostic(tmp_path):
    p = tmp_path / "heartbeat.json"
    script_guard.write_heartbeat(
        label="Build a wooden chair", step=3, total=11,
        script_hash="abc123", path=p,
    )
    assert p.is_file()

    msg = script_guard.check_and_clear_stale_heartbeat(path=p)
    assert msg is not None
    assert "Build a wooden chair" in msg
    assert "3/11" in msg
    # Stale marker is consumed — checking again finds nothing.
    assert not p.is_file()
    assert script_guard.check_and_clear_stale_heartbeat(path=p) is None


def test_clear_heartbeat_removes_marker_with_no_stale_report(tmp_path):
    p = tmp_path / "heartbeat.json"
    script_guard.write_heartbeat(label="x", step=1, total=1, script_hash="h", path=p)
    assert p.is_file()

    script_guard.clear_heartbeat(path=p)
    assert not p.is_file()
    # A cleanly-finished script leaves nothing for next launch to misreport.
    assert script_guard.check_and_clear_stale_heartbeat(path=p) is None


def test_check_stale_heartbeat_with_no_marker_returns_none(tmp_path):
    p = tmp_path / "does_not_exist.json"
    assert script_guard.check_and_clear_stale_heartbeat(path=p) is None


def test_check_stale_heartbeat_with_corrupt_json_is_handled(tmp_path):
    p = tmp_path / "heartbeat.json"
    p.write_text("{not valid json", encoding="utf-8")
    # Must not raise — corrupt marker is treated as nothing-to-report,
    # and is still cleaned up so it doesn't linger forever.
    assert script_guard.check_and_clear_stale_heartbeat(path=p) is None
    assert not p.is_file()


def test_write_heartbeat_never_raises_on_bad_path():
    # A path under a nonexistent parent with no mkdir — write must be
    # best-effort and swallow the failure, never break a build.
    from pathlib import Path
    bogus = Path("Z:/definitely/not/a/real/path/heartbeat.json")
    script_guard.write_heartbeat(label="x", step=1, total=1, script_hash="h", path=bogus)
    # No exception means the test passes; nothing further to assert.
