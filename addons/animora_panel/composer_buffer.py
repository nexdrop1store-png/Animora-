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


def column_from_click_x(click_x: float, left_margin_px: float, pixels_per_char: float) -> int:
    """v1.2 click-to-position: convert a region-relative click X pixel
    coordinate into a column index, using the same flat pixels-per-char
    rate the panel already uses for word-wrapping (panel.py's
    _BASE_PIXELS_PER_CHAR). Pulled out as a pure function (rather than
    inlined in operators.py, which imports bpy unconditionally and so
    can't be unit-tested directly) precisely so this arithmetic has
    test coverage independent of a live Blender.

    Never negative — a click left of the estimated text-start margin
    clamps to column 0, not a negative index (caret_from_rowcol would
    clamp it anyway, but doing it here keeps the contract explicit)."""
    if pixels_per_char <= 0:
        return 0
    return max(0, round((click_x - left_margin_px) / pixels_per_char))


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

    def set_caret(self, index: int) -> None:
        """Set the caret to an absolute index, clamped to [0, len(text)].
        Public counterpart to _clamp() for callers outside the class
        (the composer modal operator's v1.2 click-to-position handler)
        that shouldn't reach into the private clamp helper."""
        self.caret = index
        self._clamp()

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

    def caret_from_rowcol(self, width: int, row: int, col: int) -> int:
        """Inverse of caret_rowcol() — map a clicked (row, col) back to
        an absolute caret index (v1.2: click-to-position in the
        composer). Uses the same `_layout` so it can never diverge
        from wrapped()/caret_rowcol().

        Out-of-range row/col clamp to the nearest valid position:
        a click below the last row goes to end-of-text, above the
        first row goes to start-of-text, and a click past a short
        row's own end lands at THAT row's end (not the next row's
        start) — clicking in the empty space after a short line
        should not jump the caret onto the following line."""
        rows = self._layout(width)
        row = max(0, min(row, len(rows) - 1))
        start, text = rows[row]
        col = max(0, min(col, len(text)))
        return start + col

    # ── Internal ─────────────────────────────────────────────────────────
    def _clamp(self) -> None:
        self.caret = max(0, min(len(self.text), self.caret))
