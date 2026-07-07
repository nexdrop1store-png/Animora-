"""Pure text-buffer model for the multiline composer.

bpy-free and exhaustively unit-tested (addons/tests/test_composer_buffer.py)
so the risky editing logic is verified without a GUI. The modal operator in
operators.py is a thin bpy shell that feeds events into this and draws its
`wrapped()` output via blf.

Model: a single string `text` + an integer `caret` (0..len). Newlines are
real "\n" characters. `wrapped(width)` reflows for display and maps the
caret to a (row, col) cursor position so the operator can draw a blinking
caret at the right spot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextBuffer:
    text: str = ""
    caret: int = 0

    def __post_init__(self) -> None:
        self._clamp()

    # ── Mutations ────────────────────────────────────────────────────────
    def insert(self, s: str) -> None:
        """Insert a string at the caret (handles multi-char paste)."""
        if not s:
            return
        self.text = self.text[: self.caret] + s + self.text[self.caret :]
        self.caret += len(s)

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        """Delete the char before the caret."""
        if self.caret > 0:
            self.text = self.text[: self.caret - 1] + self.text[self.caret :]
            self.caret -= 1

    def delete(self) -> None:
        """Delete the char at the caret (forward delete)."""
        if self.caret < len(self.text):
            self.text = self.text[: self.caret] + self.text[self.caret + 1 :]

    def delete_word_back(self) -> None:
        """Ctrl+Backspace: delete the word (and trailing spaces) before caret."""
        if self.caret == 0:
            return
        i = self.caret
        # eat spaces
        while i > 0 and self.text[i - 1] == " ":
            i -= 1
        # eat non-spaces
        while i > 0 and self.text[i - 1] not in (" ", "\n"):
            i -= 1
        self.text = self.text[:i] + self.text[self.caret :]
        self.caret = i

    # ── Caret movement ───────────────────────────────────────────────────
    def move_left(self) -> None:
        self.caret = max(0, self.caret - 1)

    def move_right(self) -> None:
        self.caret = min(len(self.text), self.caret + 1)

    def move_home(self) -> None:
        """Start of the current visual line (after the last "\n")."""
        nl = self.text.rfind("\n", 0, self.caret)
        self.caret = nl + 1 if nl != -1 else 0

    def move_end(self) -> None:
        """End of the current line (before the next "\n")."""
        nl = self.text.find("\n", self.caret)
        self.caret = nl if nl != -1 else len(self.text)

    def clear(self) -> None:
        self.text = ""
        self.caret = 0

    def set_text(self, value: str) -> None:
        self.text = value or ""
        self.caret = len(self.text)

    # ── Display ──────────────────────────────────────────────────────────
    def _layout(self, width: int) -> list[tuple[int, str]]:
        """Single source of truth for wrapping. Returns [(start_index, text)]
        rows where start_index is the offset of the row's first char in
        `self.text` (so the caret can be mapped without re-deriving)."""
        width = max(1, width)
        rows: list[tuple[int, str]] = []
        idx = 0
        paragraphs = self.text.split("\n")
        for para in paragraphs:
            if para == "":
                rows.append((idx, ""))
            else:
                line_start = idx  # offset of the current row's first char
                line = ""
                pos = idx  # running offset into self.text
                for word in para.split(" "):
                    # Hard-break an over-long single word.
                    while len(word) > width:
                        if line:
                            rows.append((line_start, line))
                            line = ""
                        rows.append((pos, word[:width]))
                        pos += width
                        word = word[width:]
                        line_start = pos
                    if not line:
                        line_start = pos
                        line = word
                        pos += len(word)
                    elif len(line) + 1 + len(word) <= width:
                        line += " " + word
                        pos += 1 + len(word)
                    else:
                        rows.append((line_start, line))
                        pos += 1  # skip the breaking space
                        line_start = pos
                        line = word
                        pos += len(word)
                rows.append((line_start, line))
            idx += len(para) + 1  # +1 for the "\n" separator
        return rows or [(0, "")]

    def wrapped(self, width: int) -> list[str]:
        """Word-wrap to `width` columns for display."""
        return [text for _start, text in self._layout(width)]

    def caret_rowcol(self, width: int) -> tuple[int, int]:
        """Map the caret to (row, col) in the wrapped layout, using the same
        `_layout` so it can never diverge from `wrapped()`."""
        rows = self._layout(width)
        best = (0, 0)
        for r, (start, text) in enumerate(rows):
            if self.caret < start:
                break
            best = (r, min(self.caret - start, len(text)))
        return best

    # ── Internal ─────────────────────────────────────────────────────────
    def _clamp(self) -> None:
        self.caret = max(0, min(len(self.text), self.caret))
