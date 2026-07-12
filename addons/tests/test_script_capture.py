"""
V2 Phase 2 — behavior tests for the execute_python stdout/stderr capture.

script_capture.py is bpy-free by design; these tests run everywhere
(dev machines and CI) and exercise the exact object the _ScriptRunner
wraps around each statement's exec().
"""

from __future__ import annotations

import sys

from animora_panel.script_capture import (
    _HEAD_KEEP,
    _TAIL_KEEP,
    ScriptOutputCapture,
)


def test_captures_print_across_statements() -> None:
    """Prints from multiple statements accumulate in order — the
    exec-as-one-script semantics the AST-split runner preserves."""
    cap = ScriptOutputCapture()
    with cap.capture():
        print("step one: created Crate")
    with cap.capture():
        print("step two: parented Crate to Pallet")
    assert cap.stdout_text == "step one: created Crate\nstep two: parented Crate to Pallet\n"
    assert cap.stderr_text == ""
    assert cap.has_output


def test_captures_stderr_separately() -> None:
    cap = ScriptOutputCapture()
    with cap.capture():
        print("normal", file=sys.stdout)
        print("warning: negative scale", file=sys.stderr)
    assert cap.stdout_text == "normal\n"
    assert cap.stderr_text == "warning: negative scale\n"


def test_exception_preserves_prior_output() -> None:
    """The failure case this feature exists for: a statement raises, but
    everything printed BEFORE the failure is still captured (the capture
    context folds in its finally block)."""
    cap = ScriptOutputCapture()
    with cap.capture():
        print("about to divide")
    try:
        with cap.capture():
            print("inside failing statement")
            raise ZeroDivisionError("boom")
    except ZeroDivisionError:
        pass
    assert "about to divide" in cap.stdout_text
    assert "inside failing statement" in cap.stdout_text


def test_bounded_truncation_keeps_head_and_tail() -> None:
    """A print-flood (per-vertex logging) must not grow unbounded: the
    render keeps the head, keeps the tail, and counts the dropped middle."""
    cap = ScriptOutputCapture()
    with cap.capture():
        for i in range(20_000):
            print(f"vertex {i}")
    text = cap.stdout_text
    assert text.startswith("vertex 0\n")            # head preserved
    assert text.rstrip().endswith("vertex 19999")   # tail preserved
    assert "chars truncated]" in text               # drop is explicit
    # Bounded: head + tail + marker, never the full flood.
    assert len(text) < _HEAD_KEEP + _TAIL_KEEP + 100


def test_no_output_means_empty_suffix() -> None:
    """Quiet scripts keep their exact legacy tool_result shape — the
    formatter must contribute nothing."""
    cap = ScriptOutputCapture()
    with cap.capture():
        x = 1 + 1  # noqa: F841 — deliberately silent statement
    assert cap.format_for_tool_result() == ""
    assert not cap.has_output


def test_format_for_tool_result_shape() -> None:
    cap = ScriptOutputCapture()
    with cap.capture():
        print("built 4 table legs")
        print("uv unwrap skipped", file=sys.stderr)
    block = cap.format_for_tool_result()
    assert block.startswith("\n--- script stdout ---\n")
    assert "built 4 table legs" in block
    assert "\n--- script stderr ---\n" in block
    assert "uv unwrap skipped" in block
    # stdout section always precedes stderr section.
    assert block.index("stdout") < block.index("stderr")


def test_stdout_restored_after_capture() -> None:
    """The redirect must be scoped to the statement: after capture()
    exits (success OR exception), sys.stdout is the real stream again —
    Blender's console logging depends on it."""
    before = sys.stdout
    cap = ScriptOutputCapture()
    with cap.capture():
        print("inside")
    assert sys.stdout is before
    try:
        with cap.capture():
            raise RuntimeError("fail inside redirect")
    except RuntimeError:
        pass
    assert sys.stdout is before
