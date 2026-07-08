"""Modal view bases for the tui engine (P4).

Ports of the Textual modal trio — screens.py's _VimOptionListMixin
list screens, widgets.py's FuzzyPickerScreen and ModalForm — as View
subclasses: a centered bordered box drawn over the view below, with
list / fuzzy-picker / form specializations.

Dismiss contract (same as Textual's ModalScreen): `dismiss(result)`
delivers `result` to the `on_result` callback given to App.push;
escape / ctrl+h / backspace cancel with `cancel_result` (None unless a
subclass overrides — Confirm uses False, BrainPreview uses "").
"""

from __future__ import annotations

from typing import Any

from rendering import (
    BG_RAISED, BG_SURFACE,
    C_BLUE, C_DIM, C_FAINT, C_PURPLE,
)

from ..layout import Rect, center
from ..view import View
from ..widgets import Cycler, FocusRing, FuzzyList, LineEdit, ListView, TextEdit, strip_markup

_HINT_SELECT = f"[{C_DIM}]Enter[/{C_DIM}] select  [{C_DIM}]^H[/{C_DIM}] back"
_HINT_SUBMIT = f"[{C_DIM}]Enter[/{C_DIM}] submit  [{C_DIM}]^H[/{C_DIM}] back"


class ModalView(View):
    """Centered bordered modal box; the view below renders underneath.

    Subclasses set `box_size` (clamped to the screen), `title`, `hint`,
    override `_dispatch_key` to give hosted widgets first crack at keys,
    and draw content in `render_body(frame, body_rect)`. Unconsumed
    escape / ctrl+h / backspace dismiss with `cancel_result`.
    """

    opaque = False
    fullscreen = False  # True = whole screen instead of a centered box
    box_size: tuple[int, int] = (60, 12)
    border_color: str = C_FAINT
    cancel_result: Any = None

    def __init__(self, title: str = "", hint: str = "") -> None:
        super().__init__()
        self.title = title
        self.hint = hint
        self._box = Rect(0, 0, 0, 0)

    # ── keys ──────────────────────────────────────────────────────

    def on_key(self, ev) -> bool:
        if self._dispatch_key(ev):
            self.request_paint()
            return True
        if ev.key in ("escape", "ctrl+h", "backspace"):
            self._cancel()
            return True
        return False

    def _dispatch_key(self, ev) -> bool:
        """Subclass hook: route ev to hosted widgets. True = consumed."""
        return False

    def _cancel(self) -> None:
        self.dismiss(self.cancel_result)

    # ── geometry ──────────────────────────────────────────────────

    def _compute_box(self, rect: Rect) -> Rect:
        if self.fullscreen:
            return rect
        w, h = self.box_size
        return center(rect, w, h)  # center() clamps to the rect

    @property
    def body_rect(self) -> Rect:
        """Content area inside the border + 1x2 padding (the Textual
        containers' `padding: 1 2`), minus title and hint lines."""
        box = self._box
        x = box.x + 3
        w = max(0, box.w - 6)
        top = box.y + 2
        bottom = box.bottom - 3  # inclusive last interior row
        if self.title:
            top += 2  # title line + blank (padding-bottom: 1)
        if self.hint:
            bottom -= 2  # hint line + blank above (padding-top: 1)
        return Rect(x, top, w, max(0, bottom - top + 1))

    # ── rendering ─────────────────────────────────────────────────

    def render(self, frame, rect) -> None:
        box = self._compute_box(rect)
        self._box = box
        if box.w < 8 or box.h < 4:
            return  # too small for a box worth drawing
        frame.fill(box, f"on {BG_SURFACE}")
        self._draw_border(frame, box)
        ix, iw = box.x + 3, max(0, box.w - 6)
        if self.title:
            self._write_line(
                frame, ix, box.y + 2, iw,
                f"[bold {C_PURPLE}]{self.title}[/bold {C_PURPLE}]",
            )
        if self.hint:
            self._write_centered(frame, ix, box.bottom - 3, iw, self.hint)
        self.render_body(frame, self.body_rect)

    def render_body(self, frame, body: Rect) -> None:
        """Subclass hook: draw content into the body rect."""

    def _draw_border(self, frame, box: Rect) -> None:
        style = f"{self.border_color} on {BG_SURFACE}"
        horiz = "─" * (box.w - 2)
        frame.write_markup(box.x, box.y, box.w, f"[{style}]╭{horiz}╮[/{style}]")
        for y in range(box.y + 1, box.bottom - 1):
            frame.write_markup(box.x, y, 1, f"[{style}]│[/{style}]")
            frame.write_markup(box.right - 1, y, 1, f"[{style}]│[/{style}]")
        frame.write_markup(box.x, box.bottom - 1, box.w, f"[{style}]╰{horiz}╯[/{style}]")

    def _write_line(self, frame, x: int, y: int, width: int, markup: str,
                    bg: str = BG_SURFACE) -> None:
        """One body line, padded to `width` on the modal background."""
        pad = " " * max(0, width - len(strip_markup(markup)))
        frame.write_markup(x, y, width, f"[on {bg}]{markup}{pad}[/on {bg}]")

    def _write_centered(self, frame, x: int, y: int, width: int, markup: str) -> None:
        pad = max(0, (width - len(strip_markup(markup))) // 2)
        self._write_line(frame, x, y, width, " " * pad + markup)


class ListModalView(ModalView):
    """Modal hosting a vim-navigable ListView (the _VimOptionListMixin
    screens). enter/l → `_on_selected(id)`; rows via `self.list.set_rows`.

    `fullscreen = True` makes the box the whole screen (Trash,
    CurrentSessions). The list's page_size is synced from the body rect
    on every render — the P2 gotcha, centralized here.
    """

    box_size = (80, 24)
    list_cls: type[ListView] = ListView

    def __init__(self, title: str = "", hint: str = "") -> None:
        super().__init__(title, hint or _HINT_SELECT)
        self.list = self.list_cls()
        self.list.on_select = lambda item_id: self._on_selected(item_id)

    def _on_selected(self, item_id) -> None:
        """Override: handle selection. Default dismisses with the id."""
        self.dismiss(item_id)

    def _dispatch_key(self, ev) -> bool:
        return self.list.handle_key(ev)

    def render_body(self, frame, body: Rect) -> None:
        self.list.page_size = max(1, body.h)
        for i, line in enumerate(self.list.render(body.w, body.h)):
            self._write_line(frame, body.x, body.y + i, body.w, line)


class FuzzyModalView(ModalView):
    """Fuzzy-picker modal (port of FuzzyPickerScreen): type to filter,
    j/k & friends navigate, enter → `_on_selected(id)` (default:
    dismiss with the id); escape or physical backspace on an empty
    query cancel (ctrl+h / \\x08 only ever deletes — FuzzyList
    semantics). Status line shows "N of M".

    `_get_items` (→ list of (id, markup)) and `_on_selected` may be
    overridden in a subclass or assigned on an instance — the app.py
    injection pattern. Items load lazily on first show so instance
    assignment after construction works.
    """

    box_size = (80, 28)

    def __init__(self, title: str = "Select", hint: str = "") -> None:
        super().__init__(title, hint or _HINT_SELECT)
        self.picker = FuzzyList()
        self.picker.on_select = lambda item_id: self._on_selected(item_id)
        self.picker.on_cancel = self._cancel
        self._items_loaded = False

    def _get_items(self) -> list[tuple[Any, str]]:
        """Override or instance-assign: (id, display_markup) items."""
        return []

    def _on_selected(self, item_id) -> None:
        """Override or instance-assign. Default dismisses with the id."""
        self.dismiss(item_id)

    def on_show(self) -> None:
        super().on_show()
        if not self._items_loaded:
            self._items_loaded = True
            self.picker.set_items(list(self._get_items()))

    def refresh_items(self) -> None:
        self.picker.set_items(list(self._get_items()))
        self.request_paint()

    def _dispatch_key(self, ev) -> bool:
        return self.picker.handle_key(ev)

    def render_body(self, frame, body: Rect) -> None:
        if body.h < 1 or body.w < 1:
            return
        query_markup = (
            self.picker.input.render(body.w)
            if self.picker.query
            else f"[{C_DIM}]Type to filter…[/{C_DIM}]"
        )
        self._write_line(frame, body.x, body.y, body.w, query_markup, bg=BG_RAISED)
        frame.cursor = (body.x + self.picker.input.cursor_col(body.w), body.y)
        list_h = max(0, body.h - 2)
        self.picker.list.page_size = max(1, list_h)
        for i, line in enumerate(self.picker.list.render(body.w, list_h)):
            self._write_line(frame, body.x, body.y + 1 + i, body.w, line)
        if body.h >= 2:
            self._write_line(
                frame, body.x, body.bottom - 1, body.w,
                f"[{C_DIM}]{self.picker.status}[/{C_DIM}]",
            )


class FormModalView(ModalView):
    """Labeled-field form modal (port of ModalForm). tab/shift+tab cycle
    fields via a FocusRing; enter submits from any LineEdit; ctrl+s
    submits from anywhere (the TextEdit path); escape / ctrl+h cancel.

    Subclasses register fields with `add_field(label, editor)` and
    override `_on_submit()` to build the dismiss value — return None to
    keep the form open (failed validation).
    """

    box_size = (70, 12)
    textedit_height = 6

    def __init__(self, title: str = "", hint: str = "") -> None:
        super().__init__(title, hint or _HINT_SUBMIT)
        self.fields: list[tuple[str, Any]] = []
        self.ring = FocusRing()

    def add_field(self, label: str, editor):
        """Register a (label, LineEdit/TextEdit/Cycler) field, in tab order."""
        if isinstance(editor, (LineEdit, Cycler)):
            editor.on_submit = lambda _value: self._submit()
        self.fields.append((label, editor))
        self.ring.widgets.append(editor)
        return editor

    def _on_submit(self):
        """Override: return the dismiss value (None = stay open)."""
        return None

    def _submit(self) -> None:
        result = self._on_submit()
        if result is not None:
            self.dismiss(result)

    def _dispatch_key(self, ev) -> bool:
        if ev.key == "ctrl+s":
            self._submit()
            return True
        return self.ring.route_key(ev)

    def render_body(self, frame, body: Rect) -> None:
        y = body.y
        for label, editor in self.fields:
            if y >= body.bottom:
                break
            focused = editor is self.ring.focused
            if label:
                color = f"bold {C_BLUE}" if focused else C_DIM
                self._write_line(frame, body.x, y, body.w, f"[{color}]{label}[/{color}]")
                y += 1
            if isinstance(editor, TextEdit):
                h = min(self.textedit_height, body.bottom - y)
                if h <= 0:
                    break
                for i, line in enumerate(editor.render(body.w, h)):
                    self._write_line(frame, body.x, y + i, body.w, line, bg=BG_RAISED)
                if focused:
                    pos = editor.cursor_pos(body.w)
                    if pos is not None and pos[1] < h:
                        frame.cursor = (body.x + pos[0], y + pos[1])
                y += h + 1  # margin below the field
            else:
                self._write_line(frame, body.x, y, body.w,
                                 editor.render(body.w), bg=BG_RAISED)
                if focused:
                    frame.cursor = (body.x + editor.cursor_col(body.w), y)
                y += 2  # field line + margin
