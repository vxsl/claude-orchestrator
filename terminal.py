"""Embedded terminal emulator widget for Textual.

The framework-free half (PTY lifecycle, tmux-persistent sessions, byte
pipeline into libvterm/pyte, OSC 52 forwarding, key map) lives in
term_host.TerminalHost; this module adds the Textual rendering
(render_line / Strip API) and event handling on top.

Based on mitosch/textual-terminal, modernized for Textual 8.x.
"""

from __future__ import annotations

from pyte.screens import Char
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Region
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from profile_app import perf_trace
from rendering import BG_BASE
from term_host import (
    TMUX_NAV_KEYS,
    TerminalHost,
    _KEY_MAP,
    _char_style,
    _pyte_color,
)

# Opaque base painted under any cell that sets no background of its own.
# Without it, cells with bgcolor=None emit no background and a transparent
# terminal shows through the pane. Textual only pads blank/gutter area with
# the widget background — it never fills it under content segments — so the
# substitution has to happen per cell here. Reverse runs (the block cursor)
# already swap in a solid fg color as their background, so leave them alone.
_OPAQUE_BG = Style(bgcolor=BG_BASE)


def _opaque(style: Style) -> Style:
    if style.bgcolor is None and not style.reverse:
        return _OPAQUE_BG + style
    return style


class TerminalWidget(Widget, TerminalHost, can_focus=True):
    """A terminal emulator widget that runs a command in a PTY."""

    DEFAULT_CSS = f"""
    TerminalWidget {{
        height: 1fr;
        width: 1fr;
        background: {BG_BASE};
    }}
    """

    def __init__(
        self,
        command: str = "bash",
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        passthrough_keys: set[str] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        # Explicit host init: Textual's cooperative __init__ chain may have
        # default-initialized the host already; this call is authoritative.
        TerminalHost.__init__(self, command, env=env, cwd=cwd)
        self._passthrough_keys = passthrough_keys or set()

    # ── Host hook overrides ────────────────────────────────────────

    def _handle_pty_eof(self) -> None:
        self.post_message(self.Finished())

    def _clipboard_write(self, text: str) -> None:
        app = self.app if self.is_mounted else None
        if app is not None and hasattr(app, "copy_to_clipboard"):
            app.copy_to_clipboard(text)

    def _on_frame_complete(self) -> None:
        self._refresh_dirty()

    def _request_render(self) -> None:
        self.refresh()

    # ── Lifecycle ──────────────────────────────────────────────────

    def on_mount(self) -> None:
        """Start the render tick — decouples rendering from the read loop."""
        self.set_interval(1 / 20, self._render_tick)

    @perf_trace()
    def _render_tick(self) -> None:
        """Flush pending terminal output to screen, capped at 20fps.

        While orch's terminal is off screen, dirty state accumulates and the
        flush is deferred — the first tick after it becomes visible catches up.
        """
        if self._has_dirty and not self._sync_output:
            if not getattr(self.app, "_ui_visible", True):
                return
            self._has_dirty = False
            self._refresh_dirty()

    def on_unmount(self) -> None:
        # For persistent terminals, just detach (don't kill the session)
        if getattr(self, "_persistent_session", None):
            self.detach_persistent()
        else:
            self.stop()

    # ── Rendering ──────────────────────────────────────────────────

    @perf_trace()
    def _refresh_dirty(self) -> None:
        """Refresh only lines that libvterm's damage callback marked dirty.
        Falls back to full refresh for pyte backend or when scrolled up."""
        if not self._backend or self._scroll_offset > 0:
            self.refresh()
            return
        dirty = self._backend.dirty_rows
        if not dirty:
            return
        if len(dirty) >= self._nrow:
            # All lines dirty — full refresh is cheaper
            dirty.clear()
            self.refresh()
            return
        w = self._ncol
        regions = [Region(0, row, w, 1) for row in dirty]
        dirty.clear()
        self.refresh(*regions)

    def get_content_width(self, container, viewport):
        return self._ncol

    def get_content_height(self, container, viewport, width):
        return self._nrow

    def _scrollbar_char(self, y: int) -> str | None:
        """Return scrollbar character for row y, or None if not scrolled."""
        if self._scroll_offset <= 0 or not self._backend:
            return None
        sb_len = len(self._backend.scrollback)
        if sb_len == 0:
            return None
        total = sb_len + self._nrow
        # Thumb position and size mapped to screen height
        thumb_size = max(1, self._nrow * self._nrow // total)
        # viewport_top as fraction of total scrollable range
        viewport_top = sb_len - self._scroll_offset
        thumb_top = viewport_top * self._nrow // total
        thumb_top = min(thumb_top, self._nrow - thumb_size)
        if thumb_top <= y < thumb_top + thumb_size:
            return "┃"
        return "│"

    def render_line(self, y: int) -> Strip:
        """Render a single terminal line as a Textual Strip."""
        if self._backend:
            strip = self._render_line_vterm(y)
        else:
            strip = self._render_line_pyte(y)
        # Overlay scrollbar on rightmost column when scrolled
        sb_char = self._scrollbar_char(y)
        if sb_char is not None:
            return self._overlay_scrollbar(strip, sb_char)
        return strip

    def _overlay_scrollbar(self, strip: Strip, char: str) -> Strip:
        """Replace the last character of a strip with a scrollbar indicator."""
        _thumb = Style(color="#888888")
        _track = Style(color="#333333")
        style = _thumb if char == "┃" else _track
        # Rebuild segments: trim last char, append scrollbar
        segments = list(strip)
        if not segments:
            return strip
        # Walk segments to find total width, trim last char
        total_w = sum(len(s.text) for s in segments)
        if total_w < 1:
            return strip
        # Trim last character from last non-empty segment
        new_segs: list[Segment] = []
        trimmed = False
        for seg in reversed(segments):
            if not trimmed and seg.text:
                new_segs.insert(0, Segment(seg.text[:-1], seg.style))
                trimmed = True
            else:
                new_segs.insert(0, seg)
        new_segs.append(Segment(char, style))
        return Strip(new_segs, self._ncol)

    def _render_scrollback_line(self, sb_index: int) -> Strip:
        """Render a line from the scrollback buffer using raw cell data."""
        backend = self._backend
        stored_cols = backend.scrollback[sb_index][0] if sb_index < len(backend.scrollback) else 0
        segments: list[Segment] = []
        run_text: list[str] = []
        run_style: Style | None = None

        def _flush():
            nonlocal run_text, run_style
            if run_text:
                segments.append(Segment("".join(run_text), run_style or Style()))
                run_text = []

        for x in range(self._ncol):
            cell = backend.get_scrollback_cell(sb_index, x)
            if cell is not None:
                attrs = cell.attrs
                style = Style(
                    color=backend.color_to_rich(cell.fg),
                    bgcolor=backend.color_to_rich(cell.bg),
                    bold=bool(attrs & 0x01),
                    italic=bool(attrs & 0x08),
                    underline=bool((attrs >> 1) & 0x03),
                    strike=bool(attrs & 0x80),
                    reverse=bool(attrs & 0x20),
                )
                ch = backend.cell_char(cell)
            else:
                ch = " "
                style = Style()
            style = _opaque(style)

            if style != run_style:
                _flush()
                run_style = style
            run_text.append(ch)

        _flush()
        return Strip(segments, self._ncol)

    # Style cache: (fg, bg, attrs) -> Style — avoids re-creating identical Style objects
    _style_cache: dict[tuple, Style] = {}

    @classmethod
    def _get_style(cls, key: tuple) -> Style:
        """Get or create a cached Style for the given (fg, bg, attrs) key."""
        cached = cls._style_cache.get(key)
        if cached is not None:
            return cached
        if isinstance(key, tuple) and key[0] == "cursor":
            style = Style(reverse=True)
        elif isinstance(key, tuple) and key[0] == "cursor_bar":
            # Bar cursor: cell's natural style + underline to mark position
            _, fg, bg, attrs = key
            style = Style(
                color=fg, bgcolor=bg,
                bold=bool(attrs & 0x01),
                italic=bool(attrs & 0x08),
                underline=True,
                strike=bool(attrs & 0x80),
                reverse=bool(attrs & 0x20),
            )
        else:
            fg, bg, attrs = key
            style = Style(
                color=fg,
                bgcolor=bg,
                bold=bool(attrs & 0x01),
                italic=bool(attrs & 0x08),
                underline=bool((attrs >> 1) & 0x03),
                strike=bool(attrs & 0x80),
                reverse=bool(attrs & 0x20),
            )
        style = _opaque(style)
        # Limit cache size to prevent unbounded growth
        if len(cls._style_cache) > 4096:
            cls._style_cache.clear()
        cls._style_cache[key] = style
        return style

    def _render_line_vterm(self, y: int) -> Strip:
        backend = self._backend
        if y >= backend.lines:
            return Strip.blank(self._ncol)

        # When scrolled up, some lines come from scrollback
        if self._scroll_offset > 0 and backend.scrollback:
            sb_len = len(backend.scrollback)
            sb_start = sb_len - self._scroll_offset
            virtual_line = sb_start + y
            if virtual_line < 0:
                return Strip.blank(self._ncol)
            if virtual_line < sb_len:
                return self._render_scrollback_line(virtual_line)
            screen_y = virtual_line - sb_len
            if screen_y >= backend.lines:
                return Strip.blank(self._ncol)
            y = screen_y

        cursor_x = backend.cursor_x if backend.cursor_y == y else -1
        if self._scroll_offset > 0:
            cursor_x = -1
        if not self.has_focus:
            cursor_x = -1

        # Use batch row rendering to minimize per-cell overhead
        if hasattr(backend, 'render_row_segments'):
            raw_segments = backend.render_row_segments(y, backend.columns, cursor_x)
            segments = [
                Segment(text, self._get_style(key))
                for text, key in raw_segments
            ]
            return Strip(segments, self._ncol)

        # Fallback: per-cell rendering (shouldn't be reached with vterm)
        segments: list[Segment] = []
        run_text: list[str] = []
        run_style: Style | None = None

        def _flush():
            nonlocal run_text, run_style
            if run_text:
                segments.append(Segment("".join(run_text), run_style or Style()))
                run_text = []

        for x in range(backend.columns):
            cell = backend.get_cell(y, x)

            if x == cursor_x:
                if backend.cursor_shape == 1:
                    style = Style(reverse=True)
                else:
                    attrs = cell.attrs
                    style = Style(
                        color=backend.color_to_rich(cell.fg),
                        bgcolor=backend.color_to_rich(cell.bg),
                        bold=bool(attrs & 0x01),
                        italic=bool(attrs & 0x08),
                        underline=True,
                        strike=bool(attrs & 0x80),
                        reverse=bool(attrs & 0x20),
                    )
            else:
                attrs = cell.attrs
                style = Style(
                    color=backend.color_to_rich(cell.fg),
                    bgcolor=backend.color_to_rich(cell.bg),
                    bold=bool(attrs & 0x01),
                    italic=bool(attrs & 0x08),
                    underline=bool((attrs >> 1) & 0x03),
                    strike=bool(attrs & 0x80),
                    reverse=bool(attrs & 0x20),
                )
            style = _opaque(style)

            if style != run_style:
                _flush()
                run_style = style

            run_text.append(backend.cell_char(cell))

        _flush()
        return Strip(segments, self._ncol)

    def _render_line_pyte(self, y: int) -> Strip:
        if y >= self._screen.lines:
            return Strip.blank(self._ncol)

        line = self._screen.buffer[y]
        cursor_x = self._screen.cursor.x if self._screen.cursor.y == y else -1

        segments: list[Segment] = []
        run_text: list[str] = []
        run_style: Style | None = None

        def _flush():
            nonlocal run_text, run_style
            if run_text:
                segments.append(Segment("".join(run_text), run_style or Style()))
                run_text = []

        for x in range(self._screen.columns):
            char: Char = line[x]

            if x == cursor_x and self.has_focus:
                if self._cursor_shape in (1, 2):
                    style = Style(reverse=True)
                else:
                    style = Style(
                        color=_pyte_color(char.fg),
                        bgcolor=_pyte_color(char.bg),
                        bold=char.bold,
                        italic=char.italics,
                        underline=True,
                        strike=char.strikethrough,
                    )
            else:
                style = _char_style(char)

            if style != run_style:
                _flush()
                run_style = style

            run_text.append(char.data)

        _flush()
        return Strip(segments, self._ncol)

    # ── Input ──────────────────────────────────────────────────────

    # Back-compat alias: pre-split code referenced this as a class attr.
    _TMUX_NAV_KEYS = TMUX_NAV_KEYS

    async def on_key(self, event: events.Key) -> None:
        if self._pid is None:
            return

        if event.key in self._passthrough_keys:
            return  # Let it bubble to parent screen
        # Some terminals (e.g. alacritty without kitty protocol) send \x08 for
        # ctrl+h, which Textual reports as key="backspace" character="\x08".
        # Physical backspace sends \x7f (character="\x7f"). If ctrl+h is a
        # passthrough key, let the \x08-as-backspace variant bubble too.
        if event.key == "backspace" and event.character == "\x08" and "ctrl+h" in self._passthrough_keys:
            return  # ctrl+h arriving as \x08 — let it bubble
        event.stop()
        event.prevent_default()

        key = event.key

        # In a tmux-backed session, scroll keys auto-enter copy-mode so
        # scrollback and selection share one buffer.  Once in copy-mode
        # tmux's vi bindings handle further navigation natively (hjkl,
        # ctrl-u/d, g/G, /search), and `v` starts selection / `y` yanks
        # to the system clipboard / `Escape` exits back to typing.
        if self._persistent_session:
            nav = TMUX_NAV_KEYS.get(key)
            if nav is not None:
                self._tmux_copy_mode_nav(nav)
                return
            # Explicit "enter copy-mode without scrolling" trigger
            if key == "alt+v":
                self._tmux_copy_mode_nav()
                return

        # Local-scrollback fallback for non-tmux PTYs (start() rather than
        # start_persistent()).
        if key == "ctrl+u" or key == "shift+pageup":
            if self._backend and self._backend.scrollback:
                self._scroll_up(self._nrow // 2)
            elif self._mouse_tracking:
                cx, cy = self._ncol // 2 + 1, self._nrow // 2 + 1
                for _ in range(5):
                    self._write_to_pty(f"\x1b[<64;{cx};{cy}M")
            return
        if key == "ctrl+d" or key == "shift+pagedown":
            if self._scroll_offset > 0:
                self._scroll_down(self._nrow // 2)
            elif self._mouse_tracking:
                cx, cy = self._ncol // 2 + 1, self._nrow // 2 + 1
                for _ in range(5):
                    self._write_to_pty(f"\x1b[<65;{cx};{cy}M")
            return
        if key == "shift+up" or (key == "k" and self._scroll_offset > 0):
            self._scroll_up(1)
            return
        if key == "shift+down" or (key == "j" and self._scroll_offset > 0):
            self._scroll_down(1)
            return
        if key == "shift+home":
            if self._backend and self._backend.scrollback:
                self._scroll_offset = len(self._backend.scrollback)
                self.refresh()
            return
        if key == "shift+end":
            self._scroll_offset = 0
            self.refresh()
            return

        # Any other key input snaps back to bottom
        if self._scroll_offset > 0:
            self._scroll_offset = 0
            self.refresh()

        # ctrl+letter → control character
        if key.startswith("ctrl+") and len(key) == 6:
            letter = key[-1]
            if letter.isalpha():
                code = ord(letter.lower()) - ord("a") + 1
                self._write_to_pty(chr(code))
                return

        mapped = _KEY_MAP.get(key)
        if mapped:
            self._write_to_pty(mapped)
            return

        if event.character:
            self._write_to_pty(event.character)

    async def on_paste(self, event: events.Paste) -> None:
        if self._pid is None:
            return
        event.stop()
        event.prevent_default()
        # Use bracketed paste mode so the terminal app knows it's a paste
        self._write_to_pty(f"\x1b[200~{event.text}\x1b[201~")

    async def on_resize(self, event: events.Resize) -> None:
        self._ncol = self.size.width
        self._nrow = self.size.height
        if self._backend:
            self._backend.resize(self._nrow, self._ncol)
        else:
            self._screen.resize(self._nrow, self._ncol)
        self._set_pty_size(self._nrow, self._ncol)
        self.refresh()

    async def on_click(self, event: events.Click) -> None:
        if not self._mouse_tracking or self._pid is None:
            return
        x, y = event.x + 1, event.y + 1
        self._write_to_pty(f"\x1b[<0;{x};{y}M")
        self._write_to_pty(f"\x1b[<0;{x};{y}m")

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._pid is None:
            return
        if self._mouse_tracking:
            x, y = event.x + 1, event.y + 1
            self._write_to_pty(f"\x1b[<65;{x};{y}M")
        else:
            self._scroll_down(3)

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._pid is None:
            return
        if self._mouse_tracking:
            x, y = event.x + 1, event.y + 1
            self._write_to_pty(f"\x1b[<64;{x};{y}M")
        else:
            self._scroll_up(3)

    # ── Messages ───────────────────────────────────────────────────

    class Finished(Message):
        """Posted when the subprocess exits."""
        pass
