"""Exhaustive tests for the pure composer text buffer.

This is the risky editing logic behind the GUI-untestable modal composer,
so it is verified thoroughly here (insert/delete/caret/wrap/paste/newline
and caret↔wrap mapping)."""

from __future__ import annotations

from animora_panel.composer_buffer import TextBuffer, column_from_click_x


# ── Insertion ────────────────────────────────────────────────────────────

def test_insert_at_caret():
    b = TextBuffer()
    b.insert("hello")
    assert b.text == "hello" and b.caret == 5


def test_insert_mid_string():
    b = TextBuffer(text="held", caret=2)
    b.insert("llo w")
    assert b.text == "hello wld"
    assert b.caret == 7


def test_insert_empty_noop():
    b = TextBuffer(text="x", caret=1)
    b.insert("")
    assert b.text == "x" and b.caret == 1


def test_paste_is_insert():
    b = TextBuffer(text="ab", caret=1)
    b.insert("XYZ")
    assert b.text == "aXYZb" and b.caret == 4


# ── Deletion ─────────────────────────────────────────────────────────────

def test_backspace():
    b = TextBuffer(text="hello", caret=5)
    b.backspace()
    assert b.text == "hell" and b.caret == 4


def test_backspace_at_start_noop():
    b = TextBuffer(text="hi", caret=0)
    b.backspace()
    assert b.text == "hi" and b.caret == 0


def test_delete_forward():
    b = TextBuffer(text="hello", caret=0)
    b.delete()
    assert b.text == "ello" and b.caret == 0


def test_delete_at_end_noop():
    b = TextBuffer(text="hi", caret=2)
    b.delete()
    assert b.text == "hi" and b.caret == 2


def test_delete_word_back():
    b = TextBuffer(text="make a red cube", caret=15)
    b.delete_word_back()
    assert b.text == "make a red " and b.caret == 11


def test_delete_word_back_trailing_spaces():
    b = TextBuffer(text="foo bar   ", caret=10)
    b.delete_word_back()
    assert b.text == "foo " and b.caret == 4


# ── Caret movement ───────────────────────────────────────────────────────

def test_move_left_right_bounds():
    b = TextBuffer(text="ab", caret=0)
    b.move_left()
    assert b.caret == 0
    b.move_right(); b.move_right(); b.move_right()
    assert b.caret == 2


def test_home_end_within_line():
    b = TextBuffer(text="hello world", caret=6)
    b.move_home(); assert b.caret == 0
    b.move_end(); assert b.caret == 11


def test_home_end_multiline():
    b = TextBuffer(text="line one\nline two", caret=12)  # inside "line two"
    b.move_home(); assert b.caret == 9   # just after the "\n"
    b.move_end(); assert b.caret == 17


# ── Newlines ─────────────────────────────────────────────────────────────

def test_newline_inserts():
    b = TextBuffer(text="ab", caret=1)
    b.newline()
    assert b.text == "a\nb" and b.caret == 2


# ── set/clear ────────────────────────────────────────────────────────────

def test_set_text_moves_caret_to_end():
    b = TextBuffer()
    b.set_text("seeded prompt")
    assert b.caret == len("seeded prompt")


def test_clear():
    b = TextBuffer(text="stuff", caret=3)
    b.clear()
    assert b.text == "" and b.caret == 0


def test_init_clamps_caret():
    assert TextBuffer(text="ab", caret=99).caret == 2
    assert TextBuffer(text="ab", caret=-5).caret == 0


# ── Wrapping ─────────────────────────────────────────────────────────────

def test_wrap_short_fits():
    assert TextBuffer(text="hello").wrapped(20) == ["hello"]


def test_wrap_words():
    b = TextBuffer(text="the quick brown fox jumps")
    lines = b.wrapped(10)
    assert all(len(line) <= 10 for line in lines)
    assert " ".join(lines).split() == "the quick brown fox jumps".split()


def test_wrap_preserves_explicit_newlines():
    b = TextBuffer(text="a\n\nb")
    assert b.wrapped(20) == ["a", "", "b"]


def test_wrap_hard_breaks_long_word():
    b = TextBuffer(text="x" * 25)
    lines = b.wrapped(10)
    assert lines == ["x" * 10, "x" * 10, "x" * 5]


def test_wrap_empty():
    assert TextBuffer().wrapped(10) == [""]


# ── Caret ↔ wrap mapping ─────────────────────────────────────────────────

def test_caret_rowcol_start():
    b = TextBuffer(text="hello world foo", caret=0)
    assert b.caret_rowcol(20) == (0, 0)


def test_caret_rowcol_second_row():
    # width 11: "hello world" (11) on row 0, "foo" on row 1
    b = TextBuffer(text="hello world foo", caret=12)  # 'f' of foo
    row, col = b.caret_rowcol(11)
    assert row == 1 and col == 0


def test_caret_rowcol_end_of_text():
    b = TextBuffer(text="hello world foo", caret=15)
    row, col = b.caret_rowcol(11)
    assert row == 1 and col == 3


def test_caret_rowcol_multiline_newline():
    b = TextBuffer(text="ab\ncd", caret=4)  # 'd'
    row, col = b.caret_rowcol(20)
    assert row == 1 and col == 1


def test_caret_rowcol_matches_wrapped_dimensions():
    # For any caret, the reported row must be a valid index into wrapped().
    b = TextBuffer(text="the quick brown fox jumps over the lazy dog")
    for c in range(len(b.text) + 1):
        b.caret = c
        row, col = b.caret_rowcol(12)
        lines = b.wrapped(12)
        assert 0 <= row < len(lines)
        assert 0 <= col <= len(lines[row])


# ── caret_from_rowcol — v1.2 click-to-position ────────────────────────────

def test_caret_from_rowcol_start():
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(20, 0, 0) == 0


def test_caret_from_rowcol_second_row():
    # width 11: "hello world" (11) on row 0, "foo" on row 1
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, 1, 0) == 12  # 'f' of foo


def test_caret_from_rowcol_mid_row():
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, 1, 2) == 14  # 'o' (second o of foo)


def test_caret_from_rowcol_click_past_short_row_end_clamps_to_that_row():
    # width 11: row 1 is "foo" (3 chars). Clicking col 50 on row 1 must
    # land at the END of "foo" (index 15), NOT bleed onto a next row —
    # there is no next row here, but this locks the clamp direction.
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, 1, 50) == 15


def test_caret_from_rowcol_click_below_last_row_goes_to_end():
    # Row clamps to the last row (1); col 99 clamps to that row's own
    # length ("foo" = 3 chars) -> absolute end of text.
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, 99, 99) == 15


def test_caret_from_rowcol_click_above_first_row_goes_to_start():
    # Row clamps to the first row (0); col 0 is the true start.
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, -5, 0) == 0


def test_caret_from_rowcol_negative_row_still_respects_col_within_clamped_row():
    # Row clamps to 0, but col within that row's own bounds is still
    # honored — clamping the ROW doesn't force col to 0 too.
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, -5, 3) == 3


def test_caret_from_rowcol_negative_col_clamps_to_row_start():
    b = TextBuffer(text="hello world foo")
    assert b.caret_from_rowcol(11, 1, -10) == 12


def test_caret_from_rowcol_multiline_newline():
    b = TextBuffer(text="ab\ncd")
    assert b.caret_from_rowcol(20, 1, 1) == 4  # 'd'


def test_caret_from_rowcol_empty_buffer():
    b = TextBuffer(text="")
    assert b.caret_from_rowcol(20, 0, 0) == 0
    assert b.caret_from_rowcol(20, 5, 5) == 0


def test_caret_from_rowcol_round_trips_with_caret_rowcol():
    # For every valid caret position, caret_rowcol -> caret_from_rowcol
    # must recover the exact original caret. This is the property that
    # actually matters for the click handler: whatever caret_rowcol
    # would report for a real caret there, clicking that same spot
    # must land back on it.
    texts = [
        "the quick brown fox jumps over the lazy dog",
        "a\n\nb\nc d e f g h i j k l m n o p",
        "x" * 37,
        "short",
        "",
        "multi\nline\ntext\nwith\nshort\nrows",
    ]
    for text in texts:
        b = TextBuffer(text=text)
        for width in (5, 11, 12, 20, 50):
            for c in range(len(text) + 1):
                b.caret = c
                row, col = b.caret_rowcol(width)
                recovered = b.caret_from_rowcol(width, row, col)
                assert recovered == c, (
                    f"round-trip failed: text={text!r} width={width} "
                    f"caret={c} -> ({row},{col}) -> {recovered}"
                )


def test_set_caret_clamps():
    b = TextBuffer(text="hello")
    b.set_caret(3)
    assert b.caret == 3
    b.set_caret(99)
    assert b.caret == 5
    b.set_caret(-10)
    assert b.caret == 0


# ── column_from_click_x — v1.2 click-to-position pixel math ──────────────

def test_column_from_click_x_at_left_margin_is_zero():
    assert column_from_click_x(8.0, left_margin_px=8.0, pixels_per_char=7.0) == 0


def test_column_from_click_x_scales_by_pixels_per_char():
    # 3 chars in at 7px/char, starting from an 8px margin.
    assert column_from_click_x(8.0 + 21.0, left_margin_px=8.0, pixels_per_char=7.0) == 3


def test_column_from_click_x_rounds_to_nearest_char():
    # 10px past the margin at 7px/char -> 1.43 chars, rounds to 1.
    assert column_from_click_x(8.0 + 10.0, left_margin_px=8.0, pixels_per_char=7.0) == 1
    # 17px past the margin -> 2.43 chars, rounds to 2.
    assert column_from_click_x(8.0 + 17.0, left_margin_px=8.0, pixels_per_char=7.0) == 2


def test_column_from_click_x_left_of_margin_clamps_to_zero():
    assert column_from_click_x(0.0, left_margin_px=8.0, pixels_per_char=7.0) == 0
    assert column_from_click_x(-50.0, left_margin_px=8.0, pixels_per_char=7.0) == 0


def test_column_from_click_x_zero_pixels_per_char_is_safe():
    # Defensive: must never divide by zero (a malformed/zero ui_scale
    # reading is the only realistic way this could happen).
    assert column_from_click_x(100.0, left_margin_px=8.0, pixels_per_char=0.0) == 0
