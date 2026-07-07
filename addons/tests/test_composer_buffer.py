"""Exhaustive tests for the pure composer text buffer.

This is the risky editing logic behind the GUI-untestable modal composer,
so it is verified thoroughly here (insert/delete/caret/wrap/paste/newline
and caret↔wrap mapping)."""

from __future__ import annotations

from animora_panel.composer_buffer import TextBuffer


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
