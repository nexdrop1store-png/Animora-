"""
Bounded stdout/stderr capture for the AST-split script runner.

bpy-free by design (same pattern as composer_buffer.py and auth/session.py):
operators.py's _ScriptRunner wraps each statement's exec() in capture();
the tests in addons/tests exercise this module directly without Blender.

Why capture at all (V2 Phase 2 — the execute_python contract requires
stdout/stderr capture): the model debugs its own scripts with print(),
and when a script fails mid-run the prints from the statements BEFORE
the failure are often the only clue to where the scene state diverged.
The tool_result carries them back so the next iteration's revision
prompt sees them.

Why bounded: tool_result text rides the WS frame and lands in the model's
context window — an unbounded print loop (per-vertex logging is the
classic case) would bloat both. Head+tail truncation keeps the signal:
the setup prints AND the last output before a failure.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout

# Kept per stream: first _HEAD_KEEP chars + last _TAIL_KEEP chars, with a
# marker noting how much was dropped between them. ~14 KB worst case per
# stream — comfortably inside the 8 MB WS frame cap and small enough not
# to crowd the model's context.
_HEAD_KEEP = 10_000
_TAIL_KEEP = 4_000


class _BoundedStream:
    """First-N + last-M accumulator with a dropped-char count."""

    def __init__(self, head_keep: int = _HEAD_KEEP, tail_keep: int = _TAIL_KEEP) -> None:
        self._head_keep = head_keep
        self._tail_keep = tail_keep
        self._head = ""
        self._tail = ""
        self._dropped = 0

    def append(self, text: str) -> None:
        if not text:
            return
        if len(self._head) < self._head_keep:
            take = self._head_keep - len(self._head)
            self._head += text[:take]
            text = text[take:]
        if not text:
            return
        combined = self._tail + text
        if len(combined) > self._tail_keep:
            self._dropped += len(combined) - self._tail_keep
            combined = combined[-self._tail_keep:]
        self._tail = combined

    @property
    def dropped(self) -> int:
        return self._dropped

    def render(self) -> str:
        if self._dropped:
            marker = f"\n…[{self._dropped} chars truncated]…\n"
            return self._head + marker + self._tail
        return self._head + self._tail


class ScriptOutputCapture:
    """Accumulates stdout/stderr across the statements of ONE script run.

    Usage (per statement, on Blender's main thread):

        cap = ScriptOutputCapture()
        with cap.capture():
            exec(step, namespace)
        ...
        result["output"] += cap.format_for_tool_result()

    redirect_stdout/redirect_stderr swap the process-global streams, which
    is safe here because exec() runs single-threaded on Blender's main
    thread and each redirect window is one statement long. C-level prints
    from Blender internals bypass Python's sys.stdout and are deliberately
    NOT captured — we only want the script's own print()/warnings.
    """

    def __init__(self) -> None:
        self._stdout = _BoundedStream()
        self._stderr = _BoundedStream()

    @contextmanager
    def capture(self) -> Iterator[None]:
        """Redirect stdout/stderr for one statement's exec(). Folds captured
        text into the bounded buffers even when the statement raises, so a
        failing script still reports everything it printed first."""
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                yield
        finally:
            self._stdout.append(out.getvalue())
            self._stderr.append(err.getvalue())

    @property
    def stdout_text(self) -> str:
        return self._stdout.render()

    @property
    def stderr_text(self) -> str:
        return self._stderr.render()

    @property
    def has_output(self) -> bool:
        return bool(self._stdout.render() or self._stderr.render())

    def format_for_tool_result(self) -> str:
        """Render the captured streams as a suffix for the tool_result
        `output` field. Empty string when the script printed nothing, so
        quiet scripts keep their exact legacy output shape."""
        parts: list[str] = []
        stdout = self.stdout_text
        stderr = self.stderr_text
        if stdout:
            parts.append(f"\n--- script stdout ---\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"\n--- script stderr ---\n{stderr.rstrip()}")
        return "".join(parts)
