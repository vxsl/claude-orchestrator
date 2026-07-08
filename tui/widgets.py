"""Widget primitives for the tui engine: ListView, BlockList, LineEdit,
TextEdit, FuzzyList, FocusRing, footer_markup.

Plain classes with no View dependency — views compose them, forward
KeyEvents to `handle_key(ev) -> bool` (True = consumed), and paint the
render output into their frame rect. Imports: Rich + stdlib + state.py
+ config.py only.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from rich.markup import escape as _escape

import config
from state import fuzzy_match

from .keys import KeyEvent, _CHAR_NAMES

HIGHLIGHT_BG = "#30363d"

_MARKUP_TAG_RE = re.compile(r"\[/?[^\]]*\]")


def strip_markup(text: str) -> str:
    """Rough Rich markup stripper for fuzzy matching (ported from the
    Textual widgets.py; Rich only escapes opening brackets with \\[)."""
    text = text.replace(r"\[", "\x00")
    text = _MARKUP_TAG_RE.sub("", text)
    return text.replace("\x00", "[")


# ─── ListView ──────────────────────────────────────────────────────

_DOWN_KEYS = {"j", "down", "ctrl+n"}
_UP_KEYS = {"k", "up", "ctrl+p"}
_SELECT_KEYS = {"enter", "l"}  # DEFAULT_KEYS select_item="enter,l"


class ListView:
    """Vim-navigable list of (id, markup, disabled) rows. No wrap;
    disabled rows (separators) are never highlighted."""

    def __init__(self) -> None:
        self.rows: list[tuple[Any, str, bool]] = []
        self.highlighted: int = -1
        self.page_size: int = 10  # set by the view from its rect height
        self.on_select: Callable[[Any], None] | None = None
        self.on_highlight: Callable[[Any], None] | None = None
        self._scroll = 0

    @property
    def highlighted_id(self) -> Any | None:
        if 0 <= self.highlighted < len(self.rows):
            return self.rows[self.highlighted][0]
        return None

    def set_rows(self, rows: list[tuple[Any, str, bool]], keep_id: bool = True) -> None:
        prev_id = self.highlighted_id
        prev_idx = self.highlighted
        self.rows = list(rows)
        idx = -1
        if keep_id and prev_id is not None:
            for i, (rid, _, disabled) in enumerate(self.rows):
                if rid == prev_id and not disabled:
                    idx = i
                    break
        if idx == -1 and self.rows:
            base = min(max(prev_idx, 0), len(self.rows) - 1)
            idx = self._scan(base, 1)
            if idx == -1:
                idx = self._scan(base, -1)
        self.highlighted = idx
        if self.highlighted_id is not None and self.highlighted_id != prev_id:
            self._fire_highlight()

    def update_row(self, row_id: Any, markup: str) -> bool:
        """In-place text swap (the 1-row throbber path). No callbacks."""
        for i, (rid, _, disabled) in enumerate(self.rows):
            if rid == row_id:
                self.rows[i] = (rid, markup, disabled)
                return True
        return False

    def handle_key(self, ev: KeyEvent) -> bool:
        key = ev.key
        if key in _DOWN_KEYS:
            return self._move(self._scan(self.highlighted + 1, 1))
        if key in _UP_KEYS:
            start = self.highlighted - 1 if self.highlighted >= 0 else len(self.rows) - 1
            return self._move(self._scan(start, -1))
        if key == "g":
            return self._move(self._scan(0, 1))
        if key == "G":
            return self._move(self._scan(len(self.rows) - 1, -1))
        if key in ("ctrl+d", "ctrl+u"):
            return self._half_page(1 if key == "ctrl+d" else -1)
        if key in _SELECT_KEYS:
            if self.on_select is not None and self.highlighted >= 0:
                self.on_select(self.highlighted_id)
                return True
            return False
        return False

    def render(self, width: int, height: int) -> list[str]:
        """Exactly `height` markup lines, windowed to keep the highlight
        visible with minimal scroll. Row markup passes through unmodified
        (Frame.write_markup crops; pre-wrap stays the caller's concern)."""
        if height <= 0:
            return []
        max_scroll = max(0, len(self.rows) - height)
        self._scroll = max(0, min(self._scroll, max_scroll))
        if self.highlighted >= 0:
            if self.highlighted < self._scroll:
                self._scroll = self.highlighted
            elif self.highlighted >= self._scroll + height:
                self._scroll = self.highlighted - height + 1
        lines = []
        for i in range(self._scroll, min(len(self.rows), self._scroll + height)):
            markup = self.rows[i][1]
            lines.append(f"[on {HIGHLIGHT_BG}]{markup}[/]" if i == self.highlighted else markup)
        lines.extend([""] * (height - len(lines)))
        return lines

    # movement helpers: scan finds the first enabled row from `start`
    # in direction `step`; -1 when none (top stays top, bottom bottom).

    def _scan(self, start: int, step: int) -> int:
        i = start
        while 0 <= i < len(self.rows):
            if not self.rows[i][2]:
                return i
            i += step
        return -1

    def _move(self, idx: int) -> bool:
        if idx != -1 and idx != self.highlighted:
            self.highlighted = idx
            self._fire_highlight()
        return True  # nav keys are consumed even at the edges

    def _half_page(self, direction: int) -> bool:
        if self.highlighted < 0:
            return True
        half = max(1, self.page_size // 2)
        target = min(max(self.highlighted + direction * half, 0), len(self.rows) - 1)
        idx = self._scan(target, direction)
        if idx == -1:
            idx = self._scan(target, -direction)
        return self._move(idx)

    def _fire_highlight(self) -> None:
        if self.on_highlight is not None:
            self.on_highlight(self.highlighted_id)


# ─── BlockList ─────────────────────────────────────────────────────

SEP_ID = "__sep__"  # decorative separator row id (disabled, never a block)


class BlockList(ListView):
    """ListView where a logical item spans several rows (a block).

    Main rows carry the item id (str); continuation rows carry
    ``(item_id, n)`` and are disabled so navigation skips them. The
    highlight covers the whole block, padded to the render width."""

    @staticmethod
    def block_key(row_id: Any) -> Any:
        return row_id[0] if isinstance(row_id, tuple) else row_id

    def render(self, width: int, height: int) -> list[str]:
        if height <= 0:
            return []
        max_scroll = max(0, len(self.rows) - height)
        self._scroll = max(0, min(self._scroll, max_scroll))
        hkey = None
        if 0 <= self.highlighted < len(self.rows):
            hkey = self.block_key(self.rows[self.highlighted][0])
            # Try to bring the whole block into view (block end first, so the
            # main row wins if the block is taller than the window).
            end = self.highlighted
            while end + 1 < len(self.rows) and self.block_key(self.rows[end + 1][0]) == hkey:
                end += 1
            if end >= self._scroll + height:
                self._scroll = end - height + 1
            if self.highlighted < self._scroll:
                self._scroll = self.highlighted
            elif self.highlighted >= self._scroll + height:
                self._scroll = self.highlighted - height + 1
        lines = []
        for i in range(self._scroll, min(len(self.rows), self._scroll + height)):
            rid, markup, _disabled = self.rows[i]
            if hkey is not None and rid != SEP_ID and self.block_key(rid) == hkey:
                pad = " " * max(0, width - len(strip_markup(markup)))
                lines.append(f"[on {HIGHLIGHT_BG}]{markup}{pad}[/]")
            else:
                lines.append(markup)
        lines.extend([""] * (height - len(lines)))
        return lines


# ─── LineEdit ──────────────────────────────────────────────────────


class LineEdit:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cursor = len(text)
        self.on_submit: Callable[[str], None] | None = None
        self.on_cancel: Callable[[], None] | None = None
        self.on_change: Callable[[str], None] | None = None
        self.on_empty_backspace: Callable[[], None] | None = None
        self._hscroll = 0

    def handle_key(self, ev: KeyEvent) -> bool:
        key = ev.key
        if key == "enter":
            if self.on_submit is not None:
                self.on_submit(self.text)
                return True
            return False
        if key == "escape":
            if self.on_cancel is not None:
                self.on_cancel()
                return True
            return False
        if key == "backspace":  # \x7f and \x08 both arrive here; both delete
            if not self.text:
                if self.on_empty_backspace is not None:
                    self.on_empty_backspace()
                return True
            if self.cursor > 0:
                self._set(self.text[: self.cursor - 1] + self.text[self.cursor:], self.cursor - 1)
            return True
        if key == "delete":
            if self.cursor < len(self.text):
                self._set(self.text[: self.cursor] + self.text[self.cursor + 1:], self.cursor)
            return True
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
            return True
        if key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
            return True
        if key == "home":
            self.cursor = 0
            return True
        if key == "end":
            self.cursor = len(self.text)
            return True
        if key == "ctrl+u":
            if self.cursor:
                self._set(self.text[self.cursor:], 0)
            return True
        if key == "ctrl+w":
            i = self.cursor
            while i > 0 and self.text[i - 1] == " ":
                i -= 1
            while i > 0 and self.text[i - 1] != " ":
                i -= 1
            if i != self.cursor:
                self._set(self.text[:i] + self.text[self.cursor:], i)
            return True
        if ev.char and ev.char.isprintable() and "+" not in key:
            self._set(self.text[: self.cursor] + ev.char + self.text[self.cursor:],
                      self.cursor + len(ev.char))
            return True
        return False

    def render(self, width: int) -> str:
        """Markup for one line, horizontally scrolled to keep the cursor
        visible ("\\[" renders one cell wide, so columns stay aligned)."""
        self._clamp_hscroll(width)
        return _escape(self.text[self._hscroll: self._hscroll + width])

    def cursor_col(self, width: int) -> int:
        self._clamp_hscroll(width)
        return self.cursor - self._hscroll

    def _clamp_hscroll(self, width: int) -> None:
        if width <= 0:
            return
        if self.cursor < self._hscroll:
            self._hscroll = self.cursor
        elif self.cursor >= self._hscroll + width:
            self._hscroll = self.cursor - width + 1

    def _set(self, text: str, cursor: int) -> None:
        self.text = text
        self.cursor = cursor
        if self.on_change is not None:
            self.on_change(text)


# ─── TextEdit ──────────────────────────────────────────────────────


class TextEdit:
    """Multiline editor (quick notes, brain dumps). Model holds unwrapped
    lines; render soft-wraps for display only. ^S submits."""

    def __init__(self, text: str = "") -> None:
        self.lines: list[str] = text.split("\n")
        self.row = 0
        self.col = 0
        self.on_submit: Callable[[str], None] | None = None
        self.on_cancel: Callable[[], None] | None = None
        self._vscroll = 0

    @property
    def cursor(self) -> tuple[int, int]:
        return (self.row, self.col)

    def handle_key(self, ev: KeyEvent) -> bool:
        key = ev.key
        line = self.lines[self.row]
        if key == "ctrl+s":
            if self.on_submit is not None:
                self.on_submit("\n".join(self.lines))
                return True
            return False
        if key == "escape":
            if self.on_cancel is not None:
                self.on_cancel()
                return True
            return False
        if key == "enter":
            self.lines[self.row] = line[: self.col]
            self.lines.insert(self.row + 1, line[self.col:])
            self.row += 1
            self.col = 0
            return True
        if key == "backspace":
            if self.col > 0:
                self.lines[self.row] = line[: self.col - 1] + line[self.col:]
                self.col -= 1
            elif self.row > 0:
                self.col = len(self.lines[self.row - 1])
                self.lines[self.row - 1] += line
                del self.lines[self.row]
                self.row -= 1
            return True
        if key == "up" or key == "down":
            self.row = max(0, min(self.row + (1 if key == "down" else -1), len(self.lines) - 1))
            self.col = min(self.col, len(self.lines[self.row]))
            return True
        if key == "left":
            self.col = max(0, self.col - 1)
            return True
        if key == "right":
            self.col = min(len(line), self.col + 1)
            return True
        if key == "home":
            self.col = 0
            return True
        if key == "end":
            self.col = len(line)
            return True
        if ev.char and ev.char.isprintable() and "+" not in key:
            self.lines[self.row] = line[: self.col] + ev.char + line[self.col:]
            self.col += len(ev.char)
            return True
        return False

    def render(self, width: int, height: int) -> list[str]:
        """Exactly `height` display lines, char-wrapped at `width`, scrolled
        to keep the cursor's display row visible."""
        if width <= 0 or height <= 0:
            return [""] * max(0, height)
        display: list[str] = []
        cursor_dy = 0
        for r, line in enumerate(self.lines):
            if r == self.row:
                cursor_dy = len(display) + self.col // width
            # a line of length k*width gets a trailing empty chunk so the
            # cursor at its end lands on a real display row
            for i in range(0, len(line) // width + 1):
                display.append(line[i * width: (i + 1) * width])
        max_scroll = max(0, len(display) - height)
        self._vscroll = max(0, min(self._vscroll, max_scroll))
        if cursor_dy < self._vscroll:
            self._vscroll = cursor_dy
        elif cursor_dy >= self._vscroll + height:
            self._vscroll = cursor_dy - height + 1
        out = [_escape(t) for t in display[self._vscroll: self._vscroll + height]]
        out.extend([""] * (height - len(out)))
        return out

    def cursor_pos(self, width: int) -> tuple[int, int] | None:
        """Logical cursor mapped through the wrap, relative to the window
        last laid out by render(); None when unrepresentable."""
        if width <= 0:
            return None
        dy = sum(len(self.lines[r]) // width + 1 for r in range(self.row))
        dy += self.col // width - self._vscroll
        if dy < 0:
            return None
        return (self.col % width, dy)


# ─── FuzzyList ─────────────────────────────────────────────────────

_LIST_KEYS = {"j", "k", "down", "up", "ctrl+n", "ctrl+p", "ctrl+d", "ctrl+u"}


class FuzzyList:
    """Picker composite: a LineEdit filtering a ListView. The input keeps
    "focus" for typing; j/k & friends navigate the list (as today's
    FuzzyPicker). Physical backspace on an empty query cancels; ctrl+h
    (\\x08 or CSI-u) only ever deletes."""

    def __init__(self, items: list[tuple[Any, str]] | None = None) -> None:
        self.input = LineEdit()
        self.list = ListView()
        self.status = ""
        self.on_select: Callable[[Any], None] | None = None
        self.on_cancel: Callable[[], None] | None = None
        self._items: list[tuple[Any, str]] = []
        self.input.on_change = lambda _text: self._refilter()
        self.set_items(items or [])

    @property
    def highlighted_id(self) -> Any | None:
        return self.list.highlighted_id

    @property
    def query(self) -> str:
        return self.input.text

    def set_items(self, items: list[tuple[Any, str]]) -> None:
        self._items = list(items)
        self._refilter()

    def handle_key(self, ev: KeyEvent) -> bool:
        if ev.key in _LIST_KEYS:
            return self.list.handle_key(ev)
        if ev.key == "enter":
            if self.on_select is not None and self.highlighted_id is not None:
                self.on_select(self.highlighted_id)
            return True
        if ev.key == "escape":
            if self.on_cancel is not None:
                self.on_cancel()
            return True
        if ev.key == "ctrl+h":  # kitty-protocol ctrl+h: delete, never cancel
            return self.input.handle_key(KeyEvent("backspace", "\x08"))
        if ev.key == "backspace" and ev.char != "\x08" and not self.input.text:
            if self.on_cancel is not None:
                self.on_cancel()
            return True
        return self.input.handle_key(ev)

    def _refilter(self) -> None:
        query = self.input.text
        if query:
            scored = []
            for item_id, markup in self._items:
                s = fuzzy_match(query, strip_markup(markup))
                if s is not None:
                    scored.append((s, item_id, markup))
            scored.sort(key=lambda t: -t[0])
            rows = [(item_id, markup, False) for _, item_id, markup in scored]
        else:
            rows = [(item_id, markup, False) for item_id, markup in self._items]
        if self.list.rows:
            self.list.highlighted = 0  # reset baseline: best match goes on top
        self.list.set_rows(rows, keep_id=False)  # clamps to 0, fires on change
        self.status = f"{len(rows)} of {len(self._items)}"


# ─── FocusRing ─────────────────────────────────────────────────────


class FocusRing:
    """Ordered focus cycle over widgets exposing `handle_key(ev) -> bool`.

    tab/shift+tab rotate focus; any other key goes to the focused widget.
    """

    def __init__(self, *widgets: Any) -> None:
        self.widgets: list[Any] = list(widgets)
        self.index = 0

    @property
    def focused(self) -> Any | None:
        return self.widgets[self.index] if self.widgets else None

    def focus(self, widget: Any) -> None:
        try:
            self.index = self.widgets.index(widget)
        except ValueError:
            pass

    def focus_next(self) -> None:
        if self.widgets:
            self.index = (self.index + 1) % len(self.widgets)

    def focus_prev(self) -> None:
        if self.widgets:
            self.index = (self.index - 1) % len(self.widgets)

    def route_key(self, ev: KeyEvent) -> bool:
        if ev.key == "tab":
            self.focus_next()
            return True
        if ev.key == "shift+tab":
            self.focus_prev()
            return True
        focused = self.focused
        return focused.handle_key(ev) if focused is not None else False


# ─── footer ────────────────────────────────────────────────────────

_KEY_GLYPHS = {name: ch for ch, name in _CHAR_NAMES.items()}


def _key_display(keys: str) -> str:
    key = keys.split(",")[0].strip()
    if key.startswith("ctrl+"):
        return "^" + _key_display(key[5:])
    return _KEY_GLYPHS.get(key, key)


def footer_markup(actions: list[str] | None = None, width: int = 80) -> str:
    """Key-hint footer line, e.g. " c spawn · r resume · ? help".

    Data-driven from config.py: every DEFAULT_KEYS entry with show=True,
    plus explicitly passed extra actions; keys respect user overrides via
    get_key. Whole hints are dropped (in order) once width is exhausted.
    """
    shown = [a for a, (_, _, show, _) in config.DEFAULT_KEYS.items() if show]
    for extra in actions or []:
        if extra not in shown:
            shown.append(extra)
    parts: list[str] = []
    used = 1  # leading space
    for action in shown:
        key = _key_display(config.get_key(action))
        if not key:
            continue
        default = config.DEFAULT_KEYS.get(action)
        label = (default[1] if default else "") or action
        if label == key:
            label = action  # e.g. help's desc is "?" — echo the action name
        label = label.lower()
        sep = 3 if parts else 0  # " · "
        if used + sep + len(key) + 1 + len(label) > width:
            break
        used += sep + len(key) + 1 + len(label)
        parts.append(f"{_escape(key)} [dim]{_escape(label)}[/dim]")
    return " " + " [dim]·[/dim] ".join(parts) if parts else ""
