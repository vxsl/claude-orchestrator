"""TerminalPane — the tui engine's embedded terminal (app code, not engine).

TerminalHost (term_host.py) owns the PTY/tmux lifecycle and the byte
pipeline into libvterm/pyte; this class adds the engine-side rendering
(dirty-row consumption + per-row run cache into Frame.write_runs) and
input routing (KeyEvent/MouseEvent/PasteEvent → PTY bytes) — the same
role terminal.py's TerminalWidget plays for Textual.

Pure engine + host: no Textual and no tui.views imports (enforced by a
subprocess purity test in tests/tui/test_termpane.py).

Wiring expected from the owning view:

    pane = TerminalPane("claude --resume ...", passthrough_keys={...})
    pane.request_paint = app.request_paint          # repaint scheduling
    pane.copy_to_clipboard = app.copy_to_clipboard  # OSC 52 forwarding
    pane.on_finished = <callable>                   # child exited

    on_show:    app.register_pane(pane)   # joins the 20fps dirty ticker
    on_hide:    app.unregister_pane(pane)
    on_resize:  pane.resize(rect.h, rect.w)
    render:     pane.render(frame, rect, focused=...)
    on_key:     return pane.handle_key(ev)     # False = owner's turn
    on_paste:   return pane.handle_paste(ev)
    on_mouse:   return pane.handle_mouse(ev, ev.x - rect.x, ev.y - rect.y)

The pane never paints on its own: the PTY read loop sets `has_dirty`,
the App's pane ticker coalesces that into request_paint at 20fps, and
synchronized-output end (2026l) short-circuits the tick for an immediate
paint. render() consumes backend.dirty_rows and clears has_dirty.
"""

from __future__ import annotations

from term_host import TMUX_NAV_KEYS, TerminalHost, _KEY_MAP, _pyte_color

from .frame import runs_to_segments

# Scrollbar overlay style keys (vterm run-key form: (fg, bg, attrs)).
_SB_THUMB = ("#888888", None, 0)
_SB_TRACK = ("#333333", None, 0)
_DEFAULT_KEY = (None, None, 0)


class TerminalPane(TerminalHost):
    """Engine adapter over TerminalHost: Frame rendering + event routing."""

    def __init__(
        self,
        command: str = "bash",
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        passthrough_keys: set[str] | None = None,
    ) -> None:
        TerminalHost.__init__(self, command, env=env, cwd=cwd)
        self._passthrough_keys = passthrough_keys or set()
        # Wired by the owning view (kept as attrs so the pane never needs
        # an app reference — no tui imports, trivially testable).
        self.request_paint = None
        self.copy_to_clipboard = None
        # Per-row cache for the live vterm path: cache[y] is the last
        # rendered row as (segments, cell_width) — Segments prebuilt and
        # measured once (both dominate the paint profile otherwise) — or
        # None. Rows re-render when backend.dirty_rows says so; scrolls
        # arrive as backend pending_moves and shift the cache instead of
        # re-rendering (30 rows of ctypes cell reads per scrolled line);
        # everything else re-paints as a plain list splice.
        self._row_cache: list[tuple | None] = [None] * self._nrow
        self._focused = False       # focus state at last render
        self._was_scrolled = False  # scrollback mode at last render
        if self._backend is not None:
            self._backend.track_moves = True

    def _invalidate_cache(self) -> None:
        self._row_cache = [None] * self._nrow
        if self._backend is not None:
            self._backend.pending_moves.clear()
            self._backend.moves_overflow = False

    # ── Detach/attach: move tracking follows the pane ─────────────

    def detach(self) -> dict | None:
        # Hand the backend back in widget-compatible form: whoever attaches
        # next may not know about pending_moves.
        state = super().detach()
        if state is not None and "backend" in state:
            state["backend"].track_moves = False
            state["backend"].pending_moves.clear()
        return state

    def attach(self, state: dict) -> None:
        super().attach(state)
        if self._backend is not None:
            self._backend.track_moves = True
            self._backend.pending_moves.clear()
        self._row_cache = [None] * self._nrow

    # ── App pane-ticker contract ──────────────────────────────────

    @property
    def has_dirty(self) -> bool:
        """Unrendered PTY output pending (set by the host's read loop)."""
        return self._has_dirty

    @has_dirty.setter
    def has_dirty(self, value: bool) -> None:
        self._has_dirty = value

    # ── TerminalHost hook overrides ───────────────────────────────

    def _request_render(self) -> None:
        # Scrollback offset changed — repaint.
        if self.request_paint is not None:
            self.request_paint()

    def _on_frame_complete(self) -> None:
        # 2026l (synchronized output end): the child finished composing a
        # frame — paint on the next loop iteration instead of waiting for
        # the 20fps ticker.
        if self.request_paint is not None:
            self.request_paint()

    def _clipboard_write(self, text: str) -> None:
        if self.copy_to_clipboard is not None:
            self.copy_to_clipboard(text)

    # _handle_pty_eof: host default fires self.on_finished — sufficient.
    # _read_loop / _release_fd_reader: the add_reader loop this pane used to
    # override now lives in TerminalHost, so both engines share it.

    # ── Geometry ──────────────────────────────────────────────────

    def resize(self, rows: int, cols: int) -> None:
        """Resize backend + PTY; invalidates the row cache. Idempotent."""
        if rows <= 0 or cols <= 0 or (rows, cols) == (self._nrow, self._ncol):
            return
        self._nrow = rows
        self._ncol = cols
        if self._backend:
            self._backend.resize(rows, cols)
        else:
            self._screen.resize(rows, cols)
        self._set_pty_size(rows, cols)
        self._invalidate_cache()

    # ── Rendering ─────────────────────────────────────────────────

    def render(self, frame, rect, focused: bool = False) -> None:
        """Write the terminal grid into `frame` at `rect`.

        Live vterm path: consume backend.dirty_rows (cleared after) and
        re-render only those rows; every other row reuses its cached runs.
        Scrolled: rows come from scrollback cells + a scrollbar overlay,
        cache disabled. pyte fallback: full-row rendering, no cache.
        """
        if rect.w <= 0 or rect.h <= 0:
            return
        if (rect.h, rect.w) != (self._nrow, self._ncol):
            self.resize(rect.h, rect.w)  # keep pane in lockstep with rect
        if focused != self._focused:
            self._focused = focused
            self._invalidate_cache()  # cursor row is stale
        self._has_dirty = False  # this paint reflects all fed output
        if not self._backend:
            self._render_pyte(frame, rect, focused)
            return
        scrolled = self._scroll_offset > 0
        if scrolled != self._was_scrolled:
            self._was_scrolled = scrolled
            self._invalidate_cache()
        if scrolled:
            self._render_scrolled(frame, rect)
        else:
            self._render_live(frame, rect, focused)

    def _apply_pending_moves(self) -> None:
        """Shift cached rows along backend row moves (scrolls) so a scroll
        costs list moves, not a full-grid ctypes re-render."""
        backend = self._backend
        moves = backend.pending_moves
        if not moves:
            return
        cache = self._row_cache
        nrows = len(cache)
        if backend.moves_overflow or len(moves) >= nrows * 4:
            # Runaway backlog (firehose stream or parked in scrollback):
            # cheaper to drop the cache than replay history. Also resets
            # the backend's overflow latch so tracking resumes.
            self._invalidate_cache()
            return
        for dest_start, dest_end, delta in moves:
            if delta < 0:  # content moved up: fill top-down
                rng = range(dest_start, dest_end)
            else:          # content moved down: fill bottom-up
                rng = range(dest_end - 1, dest_start - 1, -1)
            for d in rng:
                if 0 <= d < nrows:
                    s = d - delta
                    cache[d] = cache[s] if 0 <= s < nrows else None
        moves.clear()

    def _render_live(self, frame, rect, focused: bool) -> None:
        backend = self._backend
        self._apply_pending_moves()
        dirty = backend.dirty_rows
        cache = self._row_cache
        cols = backend.columns
        cursor_y = backend.cursor_y if focused else -1
        rows = min(rect.h, backend.lines, len(cache))
        for y in range(rows):
            row = cache[y]
            if row is None or y in dirty:
                cursor_x = backend.cursor_x if y == cursor_y else -1
                runs = backend.render_row_segments(y, cols, cursor_x)
                row = runs_to_segments(runs)
                cache[y] = row
            segments, width = row
            frame.write_cells(rect.x, rect.y + y, width, segments)
        if dirty:
            dirty.clear()

    def _render_scrolled(self, frame, rect) -> None:
        """Scrollback view: virtual lines = scrollback tail + live screen."""
        backend = self._backend
        sb_len = len(backend.scrollback)
        sb_start = sb_len - self._scroll_offset
        rows = min(rect.h, self._nrow)
        last_col = rect.x + rect.w - 1
        for y in range(rows):
            virtual = sb_start + y
            if 0 <= virtual < sb_len:
                runs = self._scrollback_runs(virtual)
            elif virtual >= sb_len and virtual - sb_len < backend.lines:
                # Cursor hidden while scrolled (as in the Textual widget).
                runs = backend.render_row_segments(
                    virtual - sb_len, backend.columns, -1)
            else:
                runs = []
            if runs:
                frame.write_runs(rect.x, rect.y + y, runs)
            sb_char = self._scrollbar_char(y)
            if sb_char is not None:
                style = _SB_THUMB if sb_char == "┃" else _SB_TRACK
                frame.write_runs(last_col, rect.y + y, [(sb_char, style)])

    def _scrollbar_char(self, y: int) -> str | None:
        """Scrollbar character for row y, or None if not scrolled.
        (Ported from TerminalWidget._scrollbar_char.)"""
        if self._scroll_offset <= 0 or not self._backend:
            return None
        sb_len = len(self._backend.scrollback)
        if sb_len == 0:
            return None
        total = sb_len + self._nrow
        thumb_size = max(1, self._nrow * self._nrow // total)
        viewport_top = sb_len - self._scroll_offset
        thumb_top = viewport_top * self._nrow // total
        thumb_top = min(thumb_top, self._nrow - thumb_size)
        return "┃" if thumb_top <= y < thumb_top + thumb_size else "│"

    def _scrollback_runs(self, sb_index: int) -> list[tuple[str, tuple]]:
        """One scrollback line as (text, (fg, bg, attrs)) runs — the cell
        walk from TerminalWidget._render_scrollback_line without the Rich
        Style/Strip objects, plus the wide-char continuation skip."""
        backend = self._backend
        continuation = backend.CONTINUATION
        runs: list[tuple[str, tuple]] = []
        run_chars: list[str] = []
        run_key: tuple | None = None
        cols = self._ncol
        x = 0
        while x < cols:
            cell = backend.get_scrollback_cell(sb_index, x)
            if cell is None:
                key = _DEFAULT_KEY
                ch = " "
                step = 1
            else:
                cp = cell.chars[0]
                if cp == continuation:
                    x += 1
                    continue
                key = (
                    backend.color_to_rich(cell.fg),
                    backend.color_to_rich(cell.bg),
                    cell.attrs,
                )
                ch = backend.cell_char(cell)
                step = 2 if (cell.width == 2 and x + 1 < cols) else 1
            if key != run_key:
                if run_chars:
                    runs.append(("".join(run_chars), run_key))
                run_chars = [ch]
                run_key = key
            else:
                run_chars.append(ch)
            x += step
        if run_chars:
            runs.append(("".join(run_chars), run_key))
        return runs

    def _render_pyte(self, frame, rect, focused: bool) -> None:
        """pyte fallback (no libvterm): full-row rendering, no cache.
        Ports TerminalWidget._render_line_pyte, emitting vterm-style run
        keys — attrs bits: 0x01 bold, 0x02 underline, 0x08 italic,
        0x20 reverse, 0x80 strike (matching frame._style_from_key)."""
        screen = self._screen
        cursor = screen.cursor
        rows = min(rect.h, screen.lines)
        for y in range(rows):
            line = screen.buffer[y]
            cursor_x = cursor.x if (focused and cursor.y == y) else -1
            runs: list[tuple[str, tuple]] = []
            run_chars: list[str] = []
            run_key: tuple | None = None
            for x in range(screen.columns):
                char = line[x]
                if x == cursor_x and self._cursor_shape in (1, 2):
                    key = ("cursor", 1)
                else:
                    attrs = (
                        (0x01 if char.bold else 0)
                        | (0x02 if char.underscore else 0)
                        | (0x08 if char.italics else 0)
                        | (0x20 if char.reverse else 0)
                        | (0x80 if char.strikethrough else 0)
                    )
                    if x == cursor_x:
                        attrs |= 0x02  # bar/underline cursor
                    key = (_pyte_color(char.fg), _pyte_color(char.bg), attrs)
                if key != run_key:
                    if run_chars:
                        runs.append(("".join(run_chars), run_key))
                    run_chars = [char.data]
                    run_key = key
                else:
                    run_chars.append(char.data)
            if run_chars:
                runs.append(("".join(run_chars), run_key))
            frame.write_runs(rect.x, rect.y + y, runs)

    # ── Input ─────────────────────────────────────────────────────

    def handle_key(self, ev) -> bool:
        """Route a KeyEvent. Returns False when the owner should handle it
        (passthrough keys, dead PTY); True when consumed.

        Ported from TerminalWidget.on_key, with the plan's input-fidelity
        adjustment: after the special cases, `ev.raw` (the exact bytes the
        outer terminal sent) is written verbatim when present; _KEY_MAP /
        ev.char re-encoding only serves synthetic events (raw=b"").
        """
        if self._pid is None:
            return False
        key = ev.key
        if key in self._passthrough_keys:
            return False
        # Terminals without the kitty protocol send \x08 for ctrl+h, which
        # decodes as key="backspace" char="\x08" (physical backspace is
        # \x7f). If the owner wants ctrl+h, let that variant through too.
        if (
            key == "backspace"
            and ev.char == "\x08"
            and "ctrl+h" in self._passthrough_keys
        ):
            return False

        # tmux-backed sessions: scroll keys enter copy-mode so scrollback
        # and selection share one buffer; tmux vi bindings take over there.
        if self._persistent_session:
            nav = TMUX_NAV_KEYS.get(key)
            if nav is not None:
                self._tmux_copy_mode_nav(nav)
                return True
            if key == "alt+v":
                self._tmux_copy_mode_nav()
                return True

        # Local-scrollback fallback for plain PTYs (start(), not tmux).
        if key == "ctrl+u" or key == "shift+pageup":
            if self._backend and self._backend.scrollback:
                self._scroll_up(self._nrow // 2)
            elif self._mouse_tracking:
                self._synthetic_scroll(64)
            return True
        if key == "ctrl+d" or key == "shift+pagedown":
            if self._scroll_offset > 0:
                self._scroll_down(self._nrow // 2)
            elif self._mouse_tracking:
                self._synthetic_scroll(65)
            return True
        if key == "shift+up" or (key == "k" and self._scroll_offset > 0):
            self._scroll_up(1)
            return True
        if key == "shift+down" or (key == "j" and self._scroll_offset > 0):
            self._scroll_down(1)
            return True
        if key == "shift+home":
            if self._backend and self._backend.scrollback:
                self._scroll_offset = len(self._backend.scrollback)
                self._request_render()
            return True
        if key == "shift+end":
            self._scroll_offset = 0
            self._request_render()
            return True

        # Any other key snaps back to the live screen.
        if self._scroll_offset > 0:
            self._scroll_offset = 0
            self._request_render()

        # ctrl+letter → control byte (kitty CSI-u encodings from the outer
        # terminal must not be forwarded raw — the child may not speak it).
        if key.startswith("ctrl+") and len(key) == 6:
            letter = key[5]
            if letter.isalpha():
                self._write_to_pty(chr(ord(letter.lower()) - ord("a") + 1))
                return True

        # Verbatim byte fidelity for everything the outer terminal sent.
        if ev.raw:
            self._write_bytes(ev.raw)
            return True

        # Synthetic events (tests / programmatic dispatch): re-encode.
        mapped = _KEY_MAP.get(key)
        if mapped:
            self._write_to_pty(mapped)
            return True
        if ev.char:
            self._write_to_pty(ev.char)
        return True

    def handle_paste(self, ev) -> bool:
        if self._pid is None:
            return False
        self._write_to_pty(f"\x1b[200~{ev.text}\x1b[201~")
        return True

    def handle_mouse(self, ev, rel_x: int, rel_y: int) -> bool:
        """Route a MouseEvent with pane-relative 0-based coordinates:
        SGR forwarding when the child tracks the mouse, local scrollback
        for wheel events otherwise."""
        if self._pid is None:
            return False
        kind = ev.kind
        if kind == "scroll_up" or kind == "scroll_down":
            if self._mouse_tracking:
                b = (64 if kind == "scroll_up" else 65) | self._sgr_mods(ev)
                self._write_to_pty(f"\x1b[<{b};{rel_x + 1};{rel_y + 1}M")
            elif kind == "scroll_up":
                self._scroll_up(3)
            else:
                self._scroll_down(3)
            return True
        if not self._mouse_tracking:
            return False
        button = ev.button if ev.button >= 0 else 3
        if kind == "press" or kind == "release":
            b = button | self._sgr_mods(ev)
            final = "M" if kind == "press" else "m"
            self._write_to_pty(f"\x1b[<{b};{rel_x + 1};{rel_y + 1}{final}")
            return True
        if kind == "move":
            b = (button + 32) | self._sgr_mods(ev)  # motion flag (1002/1003)
            self._write_to_pty(f"\x1b[<{b};{rel_x + 1};{rel_y + 1}M")
            return True
        return False

    @staticmethod
    def _sgr_mods(ev) -> int:
        return (4 if ev.shift else 0) | (8 if ev.alt else 0) | (16 if ev.ctrl else 0)

    def _synthetic_scroll(self, code: int) -> None:
        """Fake wheel events at pane center — scrolls mouse-tracking apps
        that have no scrollback of their own (e.g. tig)."""
        cx, cy = self._ncol // 2 + 1, self._nrow // 2 + 1
        seq = f"\x1b[<{code};{cx};{cy}M" * 5
        self._write_to_pty(seq)

    def _write_bytes(self, data: bytes) -> None:
        """Write raw bytes to the PTY (ev.raw passthrough)."""
        if self._p_out is not None:
            try:
                self._p_out.write(data)
            except OSError:
                pass
