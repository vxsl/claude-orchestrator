"""Tests for the tui engine's terminal pane and the vterm wide-char fix.

The wide-char tests pin the render_row_segments invariant the engine's raw
painter depends on: the summed *cell* width of a row's runs equals the
column count, even with CJK/emoji on the row (libvterm's continuation
cells must be skipped, not emitted as spacers).
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len

vterm_backend = pytest.importorskip("vterm_backend")
VTermBackend = vterm_backend.VTermBackend


def row_text(backend: VTermBackend, row: int, cursor_x: int = -1) -> str:
    return "".join(
        text for text, _ in
        backend.render_row_segments(row, backend.columns, cursor_x)
    )


class TestWideCharRows:
    def test_cjk_and_emoji_rows_sum_to_columns(self):
        b = VTermBackend(20, 4)
        b.feed("中文AB\r\nx🎉y\r\nplain\r\n".encode())
        for row in range(4):
            text = row_text(b, row)
            assert cell_len(text) == 20, f"row {row}: {text!r}"

    def test_no_column_drift_after_wide_chars(self):
        b = VTermBackend(20, 2)
        b.feed("中文AB".encode())
        # Old behavior emitted "中 文 AB" (a spacer per continuation cell),
        # shifting A right by one column per wide char.
        assert row_text(b, 0).startswith("中文AB")

    def test_narrow_only_rows_unchanged(self):
        b = VTermBackend(10, 2)
        b.feed(b"hello")
        assert row_text(b, 0) == "hello     "

    def test_block_cursor_on_wide_char(self):
        b = VTermBackend(20, 2)
        b.feed("中".encode())
        segments = b.render_row_segments(0, 20, cursor_x=0)
        assert segments[0] == ("中", ("cursor", 1))
        assert cell_len("".join(t for t, _ in segments)) == 20

    def test_cursor_on_continuation_column_highlights_wide_char(self):
        b = VTermBackend(20, 2)
        b.feed("中".encode())
        segments = b.render_row_segments(0, 20, cursor_x=1)
        assert segments[0] == ("中", ("cursor", 1))

    def test_continuation_sentinel_in_scrollback_cells(self):
        # The pane's scrollback renderer relies on the same sentinel to
        # skip continuation cells in stored lines.
        b = VTermBackend(10, 3)
        b.feed("中文AB\r\n1\r\n2\r\n3\r\n4\r\n".encode())
        assert b.scrollback
        cell = b.get_scrollback_cell(0, 1)
        assert cell.chars[0] == VTermBackend.CONTINUATION
