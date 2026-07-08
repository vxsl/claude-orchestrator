"""Tests for the tui engine's terminal pane and the vterm wide-char fix.

The wide-char tests pin the render_row_segments invariant the engine's raw
painter depends on: the summed *cell* width of a row's runs equals the
column count, even with CJK/emoji on the row (libvterm's continuation
cells must be skipped, not emitted as spacers).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.cells import cell_len

from tui.frame import Frame
from tui.keys import KeyEvent, MouseEvent, PasteEvent
from tui.layout import Rect
from tui.termpane import TerminalPane

vterm_backend = pytest.importorskip("vterm_backend")
VTermBackend = vterm_backend.VTermBackend

REPO_ROOT = Path(__file__).resolve().parents[2]


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


# ── Pane helpers ─────────────────────────────────────────────────────

COLS, ROWS = 40, 10


def make_pane(cols: int = COLS, rows: int = ROWS) -> TerminalPane:
    """Pane with a live backend but no PTY (feed via _process_output_vterm)."""
    pane = TerminalPane("true")
    pane.resize(rows, cols)
    return pane


def render_frame(pane: TerminalPane, cols: int = COLS, rows: int = ROWS,
                 focused: bool = False) -> Frame:
    frame = Frame(cols, rows)
    pane.render(frame, Rect(0, 0, cols, rows), focused)
    return frame


def wired_pane(**kwargs) -> TerminalPane:
    """Pane pretending to have a live PTY, with a mock write end."""
    pane = make_pane(**kwargs)
    pane._pid = 424242
    pane._p_out = MagicMock()
    return pane


def written(pane: TerminalPane) -> bytes:
    return b"".join(
        call.args[0] for call in pane._p_out.write.call_args_list)


def key(name: str, char: str | None = None, raw: bytes = b"") -> KeyEvent:
    return KeyEvent(name, char, raw)


class TestPaneRender:
    def test_feed_and_render_shows_text(self):
        pane = make_pane()
        pane._process_output_vterm(b"hello pane\r\nsecond line")
        lines = render_frame(pane).plain_lines()
        assert "hello pane" in lines[0]
        assert "second line" in lines[1]

    def test_cjk_rows_keep_frame_cell_width(self):
        pane = make_pane()
        pane._process_output_vterm("中文テスト🎉 mixed ascii\r\n".encode())
        pane._process_output_vterm("second 行 with 中 wide".encode())
        for line in render_frame(pane).plain_lines():
            assert cell_len(line) == COLS

    def test_dirty_row_cache_rerenders_only_dirty_rows(self):
        pane = make_pane()
        backend = pane._backend
        calls: list[int] = []
        orig = backend.render_row_segments

        def counting(row, cols, cursor_x=-1):
            calls.append(row)
            return orig(row, cols, cursor_x)

        backend.render_row_segments = counting
        pane._process_output_vterm(b"\x1b[4;1Habc")  # cursor to row 3
        render_frame(pane)
        assert sorted(set(calls)) == list(range(ROWS))  # cold cache: all rows
        calls.clear()
        pane._process_output_vterm(b"x")  # touches row 3 only
        frame = render_frame(pane)
        assert calls == [3]
        assert "abcx" in frame.plain_lines()[3]

    def test_scroll_shifts_cache_instead_of_rerendering(self):
        """A one-line scroll must not re-render the whole grid: moved rows
        come from the run cache; only the new line + cursor rows re-render."""
        pane = make_pane()
        feed = "".join(f"row-{i:02d}\r\n" for i in range(ROWS - 1)).encode()
        pane._process_output_vterm(feed + b"bottom")  # full screen, no scroll
        render_frame(pane)  # warm cache
        backend = pane._backend
        calls: list[int] = []
        orig = backend.render_row_segments

        def counting(row, cols, cursor_x=-1):
            calls.append(row)
            return orig(row, cols, cursor_x)

        backend.render_row_segments = counting
        pane._process_output_vterm(b"\r\nnew-line")  # scrolls one line
        frame = render_frame(pane)
        assert len(set(calls)) <= 3, f"re-rendered rows: {sorted(set(calls))}"
        lines = frame.plain_lines()
        assert "row-01" in lines[0]  # shifted content came from the cache
        assert "new-line" in lines[ROWS - 1]

    def test_cached_render_matches_fresh_render_after_scrolls(self):
        """Oracle: the cache+moves pipeline must be invisible — a cached
        render equals a cold re-render at every step of a scrolling feed."""
        pane = make_pane()
        chunks = [
            "".join(f"line {i}\r\n" for i in range(8)).encode(),
            b"middle \x1b[1mBOLD\x1b[0m text\r\n" * 5,
            "宽字符 wide 中文\r\n".encode() * 4,
            b"\x1b[5;1H\x1b[Koverwrite row 4",
            b"\x1b[30;1H" + b"tail\r\n" * 3,
        ]
        for chunk in chunks:
            pane._process_output_vterm(chunk)
            cached = render_frame(pane).plain_lines()
            pane._invalidate_cache()
            fresh = render_frame(pane).plain_lines()
            assert cached == fresh

    def test_move_overflow_burst_still_renders_correctly(self):
        """A between-paints burst larger than the backend's MOVES_CAP trips
        the overflow latch; the pane must drop its cache and still match a
        fresh render, and tracking must resume afterwards."""
        pane = make_pane()
        render_frame(pane)  # warm
        burst = "".join(f"burst-{i:04d}\r\n" for i in range(400)).encode()
        pane._process_output_vterm(burst)
        assert pane._backend.moves_overflow
        cached = render_frame(pane).plain_lines()
        pane._invalidate_cache()
        fresh = render_frame(pane).plain_lines()
        assert cached == fresh
        assert not pane._backend.moves_overflow  # latch reset
        pane._process_output_vterm(b"after\r\n")
        assert pane._backend.pending_moves  # tracking resumed

    def test_backend_default_keeps_damage_behavior(self):
        """track_moves=False (the Textual widget): scrolls damage every
        moved row and pending_moves stays empty."""
        b = VTermBackend(20, 5)
        b.feed(b"1\r\n2\r\n3\r\n4\r\nfull")
        b.dirty_rows.clear()
        b.feed(b"\r\nscroll")
        assert b.pending_moves == []
        assert len(b.dirty_rows) == 5  # whole screen damaged, as before

    def test_render_clears_has_dirty_and_dirty_rows(self):
        pane = make_pane()
        pane._process_output_vterm(b"data")
        pane.has_dirty = True  # normally set by the read loop
        render_frame(pane)
        assert pane.has_dirty is False
        assert not pane._backend.dirty_rows

    def test_focus_change_shows_and_hides_cursor(self):
        pane = make_pane()
        pane._process_output_vterm(b"ab")

        def cursor_runs(frame):
            return [
                seg for row in frame.rows for seg in row
                if seg.style is not None and seg.style.reverse
            ]

        assert cursor_runs(render_frame(pane, focused=False)) == []
        assert cursor_runs(render_frame(pane, focused=True))
        # …and back off again (cache must invalidate both ways)
        assert cursor_runs(render_frame(pane, focused=False)) == []

    def test_resize_invalidates_cache_and_backend(self):
        pane = make_pane()
        pane._process_output_vterm(b"wide")
        render_frame(pane)
        pane.resize(5, 20)
        assert pane._backend.lines == 5
        assert pane._backend.columns == 20
        assert pane._row_cache == [None] * 5
        lines = render_frame(pane, 20, 5).plain_lines()
        assert any("wide" in line for line in lines)

    def test_render_resyncs_to_rect_size(self):
        pane = make_pane()
        frame = Frame(31, 7)
        pane.render(frame, Rect(0, 0, 31, 7), False)
        assert (pane._nrow, pane._ncol) == (7, 31)

    def test_pyte_fallback_renders(self):
        pane = TerminalPane("true")
        pane._backend = None
        import term_host
        pane._screen = term_host._Screen(COLS, ROWS)
        pane._stream = term_host._Stream(pane._screen)
        pane._seq_filter = term_host._SeqFilter()
        pane._process_output("pyte fallback text")
        lines = render_frame(pane).plain_lines()
        assert "pyte fallback text" in lines[0]
        assert all(cell_len(line) == COLS for line in lines)


class TestKeyRouting:
    def test_dead_pty_returns_false(self):
        pane = make_pane()
        assert pane.handle_key(key("a", "a", b"a")) is False

    def test_passthrough_returns_false_and_writes_nothing(self):
        pane = wired_pane()
        pane._passthrough_keys = {"ctrl+q"}
        assert pane.handle_key(key("ctrl+q", None, b"\x11")) is False
        pane._p_out.write.assert_not_called()

    def test_backspace_as_ctrl_h_passthrough_special_case(self):
        pane = wired_pane()
        pane._passthrough_keys = {"ctrl+h"}
        # \x08-as-backspace bubbles when ctrl+h is a passthrough key…
        assert pane.handle_key(key("backspace", "\x08", b"\x08")) is False
        pane._p_out.write.assert_not_called()
        # …but physical backspace (\x7f) still goes to the PTY.
        assert pane.handle_key(key("backspace", "\x7f", b"\x7f")) is True
        assert written(pane) == b"\x7f"

    def test_ctrl_letter_writes_control_byte(self):
        pane = wired_pane()
        assert pane.handle_key(key("ctrl+c", "\x03", b"\x03")) is True
        assert written(pane) == b"\x03"

    def test_ctrl_letter_synthesized_from_name_alone(self):
        pane = wired_pane()
        assert pane.handle_key(key("ctrl+a")) is True
        assert written(pane) == b"\x01"

    def test_raw_bytes_preferred_when_set(self):
        pane = wired_pane()
        pane.handle_key(key("up", None, b"\x1bOA"))  # application cursor mode
        assert written(pane) == b"\x1bOA"  # not _KEY_MAP's \x1b[A

    def test_key_map_fallback_for_synthetic_events(self):
        pane = wired_pane()
        pane.handle_key(key("up"))
        assert written(pane) == b"\x1b[A"

    def test_char_fallback_for_synthetic_events(self):
        pane = wired_pane()
        pane.handle_key(key("a", "a"))
        assert written(pane) == b"a"

    def test_tmux_nav_enters_copy_mode(self):
        pane = wired_pane()
        pane._persistent_session = "sess"
        pane._tmux_copy_mode_nav = MagicMock()
        assert pane.handle_key(key("ctrl+u", None, b"\x15")) is True
        pane._tmux_copy_mode_nav.assert_called_once_with("halfpage-up")
        pane._p_out.write.assert_not_called()

    def test_paste_is_bracketed(self):
        pane = wired_pane()
        assert pane.handle_paste(PasteEvent("hi\nthere")) is True
        assert written(pane) == b"\x1b[200~hi\nthere\x1b[201~"


def scrolled_pane(rows: int = 5, cols: int = 30) -> TerminalPane:
    """Pane with real scrollback content, scrolled halfway up."""
    pane = wired_pane(cols=cols, rows=rows)
    data = "".join(f"line-{i:03d}\r\n" for i in range(30)).encode()
    pane._process_output_vterm(data)
    assert pane._backend.scrollback
    return pane


class TestScrollback:
    def test_scroll_keys_move_offset_and_snap_back(self):
        pane = scrolled_pane()
        paints = []
        pane.request_paint = lambda: paints.append(1)
        assert pane.handle_key(key("ctrl+u", None, b"\x15")) is True
        assert pane._scroll_offset == 2  # half of 5 rows
        assert paints  # offset change requested a repaint
        pane.handle_key(key("k", "k", b"k"))  # k scrolls while offset > 0
        assert pane._scroll_offset == 3
        pane._p_out.write.assert_not_called()
        pane.handle_key(key("a", "a", b"a"))  # typing snaps to bottom
        assert pane._scroll_offset == 0
        assert written(pane) == b"a"

    def test_scrolled_render_shows_history_and_scrollbar(self):
        pane = scrolled_pane()
        pane._scroll_offset = len(pane._backend.scrollback)  # top of history
        frame = render_frame(pane, 30, 5)
        lines = frame.plain_lines()
        assert "line-000" in lines[0]
        assert all(line[-1] in "┃│" for line in lines)  # scrollbar overlay
        assert any(line[-1] == "┃" for line in lines)  # thumb present

    def test_scrolled_render_disables_cache(self):
        pane = scrolled_pane()
        pane._scroll_offset = 1
        render_frame(pane, 30, 5)
        assert pane._row_cache == [None] * 5  # untouched while scrolled

    def test_wheel_scrolls_locally_without_mouse_tracking(self):
        pane = scrolled_pane()
        ev = MouseEvent("scroll_up", 3, 3)
        assert pane.handle_mouse(ev, 3, 3) is True
        assert pane._scroll_offset == 3
        assert pane.handle_mouse(MouseEvent("scroll_down", 3, 3), 3, 3) is True
        assert pane._scroll_offset == 0
        pane._p_out.write.assert_not_called()

    def test_mouse_forwarded_as_sgr_when_tracking(self):
        pane = wired_pane()
        pane._mouse_tracking = True
        pane.handle_mouse(MouseEvent("press", 5, 2, 0), 5, 2)
        pane.handle_mouse(MouseEvent("release", 5, 2, 0), 5, 2)
        pane.handle_mouse(MouseEvent("scroll_up", 1, 1), 1, 1)
        assert written(pane) == b"\x1b[<0;6;3M\x1b[<0;6;3m\x1b[<64;2;2M"


class TestHooks:
    def test_frame_complete_and_scroll_request_paint(self):
        pane = make_pane()
        paints = []
        pane.request_paint = lambda: paints.append(1)
        pane._on_frame_complete()
        pane._request_render()
        assert len(paints) == 2

    def test_hooks_are_safe_unwired(self):
        pane = make_pane()
        pane._on_frame_complete()
        pane._request_render()
        pane._clipboard_write("ignored")

    def test_clipboard_write_forwards(self):
        pane = make_pane()
        clips = []
        pane.copy_to_clipboard = clips.append
        pane._clipboard_write("yanked")
        assert clips == ["yanked"]

    def test_sync_output_gates_ticker_then_frame_complete_paints(self):
        pane = make_pane()
        paints = []
        pane.request_paint = lambda: paints.append(1)
        pane._process_output_vterm(b"\x1b[?2026hpartial frame")
        assert pane._sync_output is True
        assert not paints
        pane._process_output_vterm(b"rest\x1b[?2026l")
        assert pane._sync_output is False
        assert len(paints) == 1


@pytest.mark.asyncio
class TestRealPty:
    async def test_echo_output_reaches_frame(self):
        pane = TerminalPane("sh -c 'echo real-pty-line'")
        pane.resize(6, 40)
        finished = asyncio.Event()
        pane.on_finished = finished.set
        pane.start()
        try:
            await asyncio.wait_for(finished.wait(), timeout=5)
            frame = render_frame(pane, 40, 6)
            assert "real-pty-line" in frame.plain_lines()[0]
        finally:
            pane.stop()

    async def test_handle_key_roundtrip_through_cat(self):
        pane = TerminalPane("cat")
        pane.resize(6, 40)
        pane.start()
        try:
            for ch in "hi":
                assert pane.handle_key(key(ch, ch, ch.encode())) is True
            for _ in range(250):  # cat's tty echo comes back asynchronously
                await asyncio.sleep(0.02)
                if "hi" in render_frame(pane, 40, 6).plain_lines()[0]:
                    break
            else:
                pytest.fail("typed bytes never echoed back through the PTY")
        finally:
            pane.stop()


def test_termpane_import_purity():
    """tui.termpane must import without Textual or any tui.views module."""
    code = (
        "import sys; import tui.termpane; "
        "bad = [m for m in sys.modules "
        " if m == 'textual' or m.startswith('textual.')"
        " or m.startswith('tui.views')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=REPO_ROOT)
