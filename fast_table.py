"""Lightweight table widget — replaces Textual DataTable for read-only lists.

Instead of Textual's DataTable which creates per-cell widgets and triggers
full layout reflow on cursor movement (12-33ms per frame), this renders
the entire table as pre-formatted Rich text in a single widget.

Cursor movement only redraws 2 lines (old + new), not the entire table.
Row strips are pre-cached so render_line() is a dict lookup + style apply.
"""
from __future__ import annotations

from rich.text import Text
from rich.style import Style
from rich.segment import Segment

from textual.binding import Binding
from textual.events import Click
from textual.geometry import Region
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget


# Pad string cache — avoids allocating " " * n repeatedly
_PAD_CACHE: dict[int, str] = {}


def _pad(n: int) -> str:
    if n <= 0:
        return ""
    s = _PAD_CACHE.get(n)
    if s is None:
        s = " " * n
        _PAD_CACHE[n] = s
    return s


class FastTable(Widget, can_focus=True):
    """A lightweight table widget optimized for read-only row-based lists.

    Features:
      - Row cursor with highlight
      - Row keys for identification
      - Emits RowHighlighted and RowSelected messages
      - Renders using render_line() for minimal overhead
      - Cursor movement only invalidates 2 lines
      - Pre-caches rendered strips per row
    """

    BINDINGS = [
        Binding("enter", "select_row", "Select", show=False),
    ]

    DEFAULT_CSS = """
    FastTable {
        height: 1fr;
        overflow-y: auto;
    }
    """

    class RowHighlighted(Message):
        """Posted when the cursor moves to a new row."""
        def __init__(self, table: FastTable, key: str | None, index: int) -> None:
            self.fast_table = table
            self.key = key
            self.index = index
            super().__init__()

        @property
        def control(self) -> FastTable:
            return self.fast_table

    class RowSelected(Message):
        """Posted when a row is activated (Enter)."""
        def __init__(self, table: FastTable, key: str | None, index: int) -> None:
            self.fast_table = table
            self.key = key
            self.index = index
            super().__init__()

        @property
        def control(self) -> FastTable:
            return self.fast_table

    def __init__(
        self,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._columns: list[str] = []
        self._rows: list[tuple[str, list[Text]]] = []  # (key, cells)
        self._cursor: int = 0
        self._scroll_offset: int = 0
        self._highlight_style = Style(bgcolor="#1a2332")
        self._col_widths: list[int] = []
        # Pre-rendered strip cache: row_index -> (width, strip)
        self._strip_cache: dict[int, tuple[int, Strip]] = {}
        self._cache_width: int = 0

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def cursor_row(self) -> int:
        return self._cursor

    def add_columns(self, *names: str) -> None:
        """Add columns by name."""
        self._columns = list(names)
        self._col_widths = [0] * len(names)

    def clear(self) -> None:
        """Remove all rows."""
        self._rows.clear()
        self._col_widths = [0] * len(self._columns)
        self._strip_cache.clear()

    def add_row(self, *cells: Text, key: str = "") -> None:
        """Add a row of Text cells with an optional key."""
        self._rows.append((key, list(cells)))
        for i, cell in enumerate(cells):
            if i < len(self._col_widths):
                w = cell.cell_len
                if w > self._col_widths[i]:
                    self._col_widths[i] = w

    def get_cursor_key(self) -> str | None:
        """Get the key for the currently highlighted row."""
        if 0 <= self._cursor < len(self._rows):
            return self._rows[self._cursor][0]
        return None

    def move_cursor(self, row: int) -> None:
        """Move cursor to a specific row."""
        old = self._cursor
        self._cursor = max(0, min(row, len(self._rows) - 1))
        if old != self._cursor:
            self._on_cursor_changed(old)

    def restore_cursor(self, old_key: str | None, old_row: int | None = None) -> None:
        """Restore cursor to the row with old_key, or clamp to old_row."""
        if old_key:
            for i, (k, _) in enumerate(self._rows):
                if k == old_key:
                    self._cursor = i
                    self._ensure_visible()
                    return
        if old_row is not None and self._rows:
            self._cursor = min(old_row, len(self._rows) - 1)
        self._ensure_visible()

    def rebuild(self, rows: list[tuple[str, list[Text]]], old_key: str | None = None, old_row: int | None = None) -> None:
        """Efficient bulk rebuild: clear and repopulate in one pass."""
        self._rows = rows
        self._strip_cache.clear()
        self._col_widths = [0] * len(self._columns)
        for _, cells in rows:
            for i, cell in enumerate(cells):
                if i < len(self._col_widths):
                    w = cell.cell_len
                    if w > self._col_widths[i]:
                        self._col_widths[i] = w
        # Restore cursor
        if old_key:
            for i, (k, _) in enumerate(self._rows):
                if k == old_key:
                    self._cursor = i
                    self._ensure_visible()
                    self.refresh()
                    return
        if old_row is not None and self._rows:
            self._cursor = min(old_row, len(self._rows) - 1)
        elif self._cursor >= len(self._rows):
            self._cursor = max(0, len(self._rows) - 1)
        self._ensure_visible()
        self.refresh()

    def _on_cursor_changed(self, old: int) -> None:
        """Handle cursor position change — only refresh affected lines."""
        self._ensure_visible()
        h = self.size.height
        if h > 0:
            old_y = old - self._scroll_offset
            new_y = self._cursor - self._scroll_offset
            if 0 <= old_y < h:
                region = Region(0, old_y, self.size.width, 1)
                self._styles_cache.set_dirty(region)
                self.refresh(region)
            if 0 <= new_y < h:
                region = Region(0, new_y, self.size.width, 1)
                self._styles_cache.set_dirty(region)
                self.refresh(region)
        self.post_message(self.RowHighlighted(self, self.get_cursor_key(), self._cursor))

    def _ensure_visible(self) -> None:
        """Ensure cursor is within the visible scroll viewport."""
        h = self.size.height
        if h <= 0:
            return
        if self._cursor < self._scroll_offset:
            self._scroll_offset = self._cursor
            self.refresh()
        elif self._cursor >= self._scroll_offset + h:
            self._scroll_offset = self._cursor - h + 1
            self.refresh()

    def get_content_height(self, container, viewport, width: int) -> int:
        return max(len(self._rows), 1)

    def action_cursor_down(self) -> None:
        if self._cursor < len(self._rows) - 1:
            old = self._cursor
            self._cursor += 1
            self._on_cursor_changed(old)

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            old = self._cursor
            self._cursor -= 1
            self._on_cursor_changed(old)

    def action_select_row(self) -> None:
        if 0 <= self._cursor < len(self._rows):
            self.post_message(self.RowSelected(self, self.get_cursor_key(), self._cursor))

    def on_click(self, event: Click) -> None:
        row = event.y + self._scroll_offset
        if 0 <= row < len(self._rows):
            old = self._cursor
            self._cursor = row
            if old != self._cursor:
                self._on_cursor_changed(old)

    def _build_strip(self, row_idx: int, width: int) -> Strip:
        """Build and cache a strip for a row at the given width."""
        cached = self._strip_cache.get(row_idx)
        if cached is not None and cached[0] == width:
            return cached[1]

        _, cells = self._rows[row_idx]
        segments: list[Segment] = []
        x = 0

        for i, cell in enumerate(cells):
            if i >= len(self._col_widths):
                break
            col_w = self._col_widths[i] + 1  # +1 gap

            if i == len(cells) - 1:
                col_w = max(col_w, width - x)

            cell_segs = list(cell.render(self.app.console))
            cell_len = sum(s.cell_length for s in cell_segs)

            segments.extend(cell_segs)
            pad = col_w - cell_len
            if pad > 0:
                segments.append(Segment(_pad(pad)))
            x += col_w

        remaining = width - x
        if remaining > 0:
            segments.append(Segment(_pad(remaining)))

        strip = Strip(segments, width)
        self._strip_cache[row_idx] = (width, strip)
        return strip

    def render_line(self, y: int) -> Strip:
        """Render a single line — hot path, mostly cache lookups."""
        row_idx = y + self._scroll_offset
        width = self.size.width

        if row_idx >= len(self._rows) or width <= 0:
            return Strip.blank(width, self.rich_style)

        strip = self._build_strip(row_idx, width)

        if row_idx == self._cursor:
            return strip.apply_style(self._highlight_style)

        return strip

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()
