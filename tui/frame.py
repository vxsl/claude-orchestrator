"""Frame buffer of Rich Segments + diff-painter emitting minimal ANSI.

Frame is the immediate-mode canvas: views write markup lines or vterm-style
runs into rows of Segments. Every row always covers exactly `width` cells —
writes are cropped/padded and spliced cell-aware, so nothing can escape its
rect (wide chars cut at a boundary become spaces).

Painter renders rows to ANSI strings, diffs against the previous paint, and
emits only changed rows, wrapped in DEC 2026 synchronized output so a paint
never flickers.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console
from rich.segment import Segment
from rich.style import Style

from .layout import Rect

# One shared Console for markup parsing/style resolution. Never written to;
# fixed width so construction doesn't probe the real terminal.
_CONSOLE = Console(color_system="truecolor", force_terminal=True, width=4096)

# (markup, width) -> rendered segments, shared across frames so unchanged
# lines cost a dict lookup per repaint.
_MARKUP_CACHE: dict[tuple[str, int], list[Segment]] = {}
_MARKUP_CACHE_MAX = 4096

# vterm style key -> Style (semantics ported from terminal.py _get_style)
_STYLE_CACHE: dict[tuple, Style] = {}
_STYLE_CACHE_MAX = 4096


def _render_markup(markup: str, width: int) -> list[Segment]:
    """Render one line of Rich markup to exactly `width` cells."""
    key = (markup, width)
    cached = _MARKUP_CACHE.get(key)
    if cached is not None:
        return cached
    text = _CONSOLE.render_str(markup, emoji=False, highlight=False)
    if "\n" in text.plain:
        text = text.split("\n")[0]
    segments = list(text.render(_CONSOLE))
    segments = Segment.adjust_line_length(segments, width)
    if len(_MARKUP_CACHE) >= _MARKUP_CACHE_MAX:
        _MARKUP_CACHE.clear()
    _MARKUP_CACHE[key] = segments
    return segments


def _style_from_key(key: tuple) -> Style:
    """Style for a vterm run key: ("cursor", 1), ("cursor_bar", fg, bg, attrs),
    or (fg, bg, attrs) with attrs bits 0x01 bold / 0x08 italic /
    (attrs >> 1) & 0x03 underline / 0x80 strike / 0x20 reverse."""
    cached = _STYLE_CACHE.get(key)
    if cached is not None:
        return cached
    if key[0] == "cursor":
        style = Style(reverse=True)
    elif key[0] == "cursor_bar":
        # Bar cursor: cell's natural style + underline to mark position
        _, fg, bg, attrs = key
        style = Style(
            color=fg,
            bgcolor=bg,
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
    if len(_STYLE_CACHE) >= _STYLE_CACHE_MAX:
        _STYLE_CACHE.clear()
    _STYLE_CACHE[key] = style
    return style


def runs_to_segments(runs: list[tuple[str, tuple]]) -> tuple[list[Segment], int]:
    """Convert vterm (text, style_key) runs to (segments, cell_width) once,
    for callers that cache rendered rows across paints (Frame.write_cells)."""
    segments = [Segment(text, _style_from_key(key)) for text, key in runs if text]
    return segments, Segment.get_line_length(segments)


class Frame:
    """A width x height grid of Segment rows. Row lists are never mutated in
    place — writes replace the row — so cached segment lists stay safe."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.cursor: tuple[int, int] | None = None  # (x, y), None = hidden
        blank = Segment(" " * width)
        self.rows: list[list[Segment]] = [[blank] for _ in range(height)]

    def write_markup(self, x: int, y: int, width: int, markup: str) -> None:
        """Render markup into the row at (x, y), cropped/padded to `width`."""
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            return
        w = min(width, self.width - x)
        if w <= 0:
            return
        self._splice(y, x, w, _render_markup(markup, w))

    def write_runs(self, x: int, y: int, runs: list[tuple[str, tuple]]) -> None:
        """Write vterm (text, style_key) runs verbatim — no markup parsing."""
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            return
        segments = [Segment(text, _style_from_key(key)) for text, key in runs if text]
        if not segments:
            return
        w = Segment.get_line_length(segments)
        avail = self.width - x
        if w > avail:
            segments = Segment.adjust_line_length(segments, avail)
            w = avail
        self._splice(y, x, w, segments)

    def write_cells(self, x: int, y: int, w: int, segments: list[Segment]) -> None:
        """Splice pre-built segments the caller measured at exactly `w`
        cells (see runs_to_segments). Terminal panes cache measured rows
        and re-paint them through here — Segment construction and
        cell-width measurement happen once per row *change*, not once per
        row per paint. Overflow is still cropped, so a bad width can't
        escape the frame."""
        if y < 0 or y >= self.height or x < 0 or x >= self.width or w <= 0:
            return
        avail = self.width - x
        if w > avail:
            segments = Segment.adjust_line_length(segments, avail)
            w = avail
        self._splice(y, x, w, segments)

    def fill(self, rect: Rect, style: Style | str | None = None) -> None:
        """Fill a rect with styled spaces."""
        if isinstance(style, str):
            style = _CONSOLE.get_style(style)
        x = max(0, rect.x)
        w = min(rect.right, self.width) - x
        if w <= 0:
            return
        for y in range(max(0, rect.y), min(rect.bottom, self.height)):
            self._splice(y, x, w, [Segment(" " * w, style)])

    def plain_lines(self) -> list[str]:
        """Rows as plain text (wide chars count once) — for tests/harness."""
        return ["".join(seg.text for seg in row) for row in self.rows]

    @staticmethod
    def _cut(seg: Segment, at: int, seg_len: int) -> tuple[Segment, Segment]:
        """Split `seg` at cell offset `at`. When the (cached) cell length
        equals the character count there are no wide chars, so plain string
        slicing replaces Segment.split_cells' per-character width scan —
        the common case for every splice while a pane streams."""
        text = seg.text
        if seg_len == len(text):
            style = seg.style
            return Segment(text[:at], style), Segment(text[at:], style)
        return seg.split_cells(at)

    def _splice(self, y: int, x: int, w: int, segments: list[Segment]) -> None:
        """Replace exactly `w` cells at (x, y) with `segments` (already w cells).

        Hand-rolled single walk instead of Segment.divide: a composed frame
        splices a few hundred times per paint at 20fps while a pane streams,
        and divide's generator + repeated cell-width scans dominated the
        profile. At most two segments are split (wide chars bisected at a
        boundary become spaces, as split_cells guarantees); everything else
        is reused by reference.
        """
        row = self.rows[y]
        width = self.width
        if x == 0 and w == width:
            self.rows[y] = segments
            return
        end = x + w
        crop_right = end < width  # cells at/after `end` survive
        left: list[Segment] = []
        right: list[Segment] = []
        pos = 0
        for seg in row:
            seg_len = cell_len(seg.text)
            seg_end = pos + seg_len
            if seg_end <= x:
                left.append(seg)
            elif pos >= end:
                right.append(seg)
            else:  # overlaps the replaced range
                if pos < x:
                    left.append(self._cut(seg, x - pos, seg_len)[0])
                if crop_right and seg_end > end:
                    keep = self._cut(seg, end - pos, seg_len)[1]
                    if keep.text:
                        right.append(keep)
            pos = seg_end
        self.rows[y] = left + segments + right


class Painter:
    """Diffs frames against the previous paint and emits ANSI for changed
    rows only. One Painter per real screen; `invalidate()` after resize or
    resume forces a full repaint."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or _CONSOLE
        self._prev: list[str] | None = None
        self._prev_cursor: tuple[int, int] | None = None
        self._ansi_cache: dict[Style, str] = {}

    def invalidate(self) -> None:
        self._prev = None

    def paint(self, frame: Frame) -> bytes:
        lines = [self._row_ansi(row) for row in frame.rows]
        prev = self._prev
        full = prev is None or len(prev) != len(lines)
        if full:
            changed = range(len(lines))
        else:
            changed = [y for y in range(len(lines)) if lines[y] != prev[y]]
        cursor_changed = full or frame.cursor != self._prev_cursor
        self._prev = lines
        self._prev_cursor = frame.cursor
        if not changed and not cursor_changed:
            return b""  # nothing to do: skip even the 2026 wrapper
        out = ["\x1b[?2026h"]
        for y in changed:
            out.append(f"\x1b[{y + 1};1H")
            out.append(lines[y])
            out.append("\x1b[0m")
        if frame.cursor is not None:
            cx, cy = frame.cursor
            out.append(f"\x1b[{cy + 1};{cx + 1}H\x1b[?25h")
        else:
            out.append("\x1b[?25l")
        out.append("\x1b[?2026l")
        return "".join(out).encode("utf-8")

    def _row_ansi(self, row: list[Segment]) -> str:
        parts: list[str] = []
        cache = self._ansi_cache
        for segment in row:
            style = segment.style
            if style is None:
                parts.append(segment.text)
                continue
            prefix = cache.get(style)
            if prefix is None:
                # Style.render caches its SGR codes internally; extract the
                # prefix once so each use is a dict hit + concat.
                prefix = style.render("\x00").partition("\x00")[0]
                if len(cache) >= 4096:
                    cache.clear()
                cache[style] = prefix
            if prefix:
                parts.append(prefix)
                parts.append(segment.text)
                parts.append("\x1b[0m")
            else:
                parts.append(segment.text)
        return "".join(parts)
