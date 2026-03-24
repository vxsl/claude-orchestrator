"""Reusable widgets — FuzzyPicker, ModalForm, TabBar, InlineInput.

Building blocks for all screens. No business logic — just composable UI patterns.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_PURPLE, C_YELLOW,
    BG_BASE, BG_RAISED, BG_SURFACE,
    _rich_escape,
)


# ─── FuzzyPicker ───────────────────────────────────────────────────

class FuzzyPicker(Vertical):
    """Composable fuzzy-search picker: Input + OptionList with filtering.

    Usage:
        picker = FuzzyPicker(items=[("id1", "Display 1"), ("id2", "Display 2")])
        # Listen for FuzzyPicker.ItemSelected messages

    Items are (id, display_markup) tuples.  The display_markup is shown in
    the OptionList and the plain text version is used for fuzzy matching.
    """

    class ItemSelected(Message):
        """Posted when an item is selected."""
        def __init__(self, item_id: str, **kwargs) -> None:
            super().__init__(**kwargs)
            self.item_id = item_id

    class ItemHighlighted(Message):
        """Posted when an item is highlighted."""
        def __init__(self, item_id: str, index: int, **kwargs) -> None:
            super().__init__(**kwargs)
            self.item_id = item_id
            self.index = index

    DEFAULT_CSS = f"""
    FuzzyPicker {{
        height: auto;
    }}
    FuzzyPicker > #fp-input {{
        dock: top;
        margin-bottom: 0;
        border: none;
        background: {BG_RAISED};
    }}
    FuzzyPicker > #fp-input:focus {{
        border: none;
        background: {BG_RAISED};
    }}
    FuzzyPicker > #fp-list {{
        height: auto;
        max-height: 24;
    }}
    FuzzyPicker > #fp-list > .option-list--option-highlighted {{
        background: $primary 15%;
    }}
    FuzzyPicker > #fp-status {{
        height: 1;
        color: {C_DIM};
        padding: 0 1;
    }}
    """

    def __init__(
        self,
        items: list[tuple[str, str]] | None = None,
        placeholder: str = "Type to filter...",
        show_input: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._all_items: list[tuple[str, str]] = items or []
        self._filtered_ids: list[str] = [item_id for item_id, _ in self._all_items]
        self._placeholder = placeholder
        self._show_input = show_input

    def compose(self) -> ComposeResult:
        if self._show_input:
            yield Input(placeholder=self._placeholder, id="fp-input")
        yield OptionList(id="fp-list")
        yield Static("", id="fp-status")

    def on_mount(self) -> None:
        self._rebuild_list("")
        if self._show_input:
            self.query_one("#fp-input", Input).focus()

    def set_items(self, items: list[tuple[str, str]]) -> None:
        """Replace the item list and re-filter."""
        self._all_items = items
        query = ""
        if self._show_input:
            try:
                query = self.query_one("#fp-input", Input).value
            except Exception:
                pass
        self._rebuild_list(query)

    def _rebuild_list(self, query: str) -> None:
        from state import fuzzy_match

        ol = self.query_one("#fp-list", OptionList)
        ol.clear_options()

        if query:
            scored: list[tuple[int, str, str]] = []
            for item_id, display in self._all_items:
                # Match against plain text (strip Rich markup for scoring)
                plain = _strip_markup(display)
                s = fuzzy_match(query, plain)
                if s is not None:
                    scored.append((s, item_id, display))
            scored.sort(key=lambda t: -t[0])
            self._filtered_ids = [item_id for _, item_id, _ in scored]
            for _, item_id, display in scored:
                ol.add_option(Option(display, id=item_id))
        else:
            self._filtered_ids = [item_id for item_id, _ in self._all_items]
            for item_id, display in self._all_items:
                ol.add_option(Option(display, id=item_id))

        if not self._filtered_ids:
            ol.add_option(Option(f"[{C_DIM}](no matches)[/{C_DIM}]", id="__none__", disabled=True))

        if self._filtered_ids:
            ol.highlighted = 0

        status = self.query_one("#fp-status", Static)
        status.update(f"[{C_DIM}]{len(self._filtered_ids)} of {len(self._all_items)}[/{C_DIM}]")

    @on(Input.Changed, "#fp-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._rebuild_list(event.value)

    @on(Input.Submitted, "#fp-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        self._select_highlighted()

    @on(OptionList.OptionSelected, "#fp-list")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if str(event.option_id) != "__none__":
            self.post_message(self.ItemSelected(str(event.option_id)))

    @on(OptionList.OptionHighlighted, "#fp-list")
    def _on_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index is not None and str(event.option_id) != "__none__":
            self.post_message(self.ItemHighlighted(str(event.option_id), event.option_index))

    def _select_highlighted(self) -> None:
        ol = self.query_one("#fp-list", OptionList)
        idx = ol.highlighted
        if idx is not None and idx < len(self._filtered_ids):
            item_id = self._filtered_ids[idx]
            if item_id != "__none__":
                self.post_message(self.ItemSelected(item_id))

    def on_key(self, event) -> None:
        """Route navigation keys to the OptionList while input stays focused."""
        ol = self.query_one("#fp-list", OptionList)
        key = event.key

        if key in ("down", "ctrl+n", "j"):
            if ol.option_count > 0:
                if ol.highlighted is None:
                    ol.highlighted = 0
                elif ol.highlighted < ol.option_count - 1:
                    ol.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif key in ("up", "ctrl+p", "k"):
            if ol.option_count > 0:
                if ol.highlighted is None:
                    ol.highlighted = 0
                elif ol.highlighted > 0:
                    ol.action_cursor_up()
            event.prevent_default()
            event.stop()
        elif key == "ctrl+d":
            if ol.option_count > 0:
                ol.action_page_down()
            event.prevent_default()
            event.stop()
        elif key == "ctrl+u":
            if ol.option_count > 0:
                ol.action_page_up()
            event.prevent_default()
            event.stop()

    @property
    def highlighted_id(self) -> str | None:
        """Return the item_id of the currently highlighted item."""
        ol = self.query_one("#fp-list", OptionList)
        idx = ol.highlighted
        if idx is not None and idx < len(self._filtered_ids):
            return self._filtered_ids[idx]
        return None


def _strip_markup(text: str) -> str:
    """Rough Rich markup stripper for fuzzy matching."""
    import re
    # First, replace escaped brackets with a placeholder
    text = text.replace(r'\[', '\x00LBRACKET\x00')
    # Remove Rich tags like [bold], [/bold], [#aabbcc], [dim], etc.
    text = re.sub(r'\[/?[^\]]*\]', '', text)
    # Restore escaped brackets as literal [
    text = text.replace('\x00LBRACKET\x00', '[')
    return text


# ─── FuzzyPickerScreen ─────────────────────────────────────────────

class FuzzyPickerScreen(ModalScreen):
    """Modal screen wrapping a FuzzyPicker. Use as base for picker screens.

    Subclasses should override `_get_items()` and optionally `_on_selected(item_id)`.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        # ctrl+h handled in on_key (backspace must reach Input for deletion)
    ]

    DEFAULT_CSS = f"""
    FuzzyPickerScreen {{ align: center middle; }}
    .fpscreen-container {{
        width: 80; height: auto; max-height: 85%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    .fpscreen-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    .fpscreen-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, title: str = "Select", hint: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._hint = hint or f"[{C_DIM}]Enter[/{C_DIM}] select  [{C_DIM}]^H[/{C_DIM}] back"

    def compose(self) -> ComposeResult:
        with Vertical(classes="fpscreen-container"):
            yield Label(self._title, classes="fpscreen-title")
            yield FuzzyPicker(items=self._get_items(), id="fpscreen-picker")
            yield Static(self._hint, classes="fpscreen-hint")

    def _get_items(self) -> list[tuple[str, str]]:
        """Override: return list of (id, display_markup) tuples."""
        return []

    def _on_selected(self, item_id: str) -> None:
        """Override: handle selection. Default dismisses with item_id."""
        self.dismiss(item_id)

    @on(FuzzyPicker.ItemSelected)
    def _handle_selection(self, event: FuzzyPicker.ItemSelected) -> None:
        self._on_selected(event.item_id)

    def action_go_back(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        """Handle backspace/ctrl+h: ctrl+h always goes back, backspace only when input empty."""
        if event.key == "backspace":
            # ctrl+h (char \x08) — always go back
            if event.character == "\x08":
                self.dismiss(None)
                event.stop()
                event.prevent_default()
                return
            # Physical backspace (char \x7f) — go back only if search is empty
            try:
                inp = self.query_one("#fp-input", Input)
                if not inp.value:
                    self.dismiss(None)
                    event.prevent_default()
                    event.stop()
            except Exception:
                pass


# ─── ModalForm ─────────────────────────────────────────────────────

class ModalForm(ModalScreen):
    """Base class for modal form screens.

    Provides standard container CSS, dismiss bindings, and a compose pattern.
    Subclasses override `compose_form()` to yield their form fields.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
    ]

    DEFAULT_CSS = f"""
    ModalForm {{ align: center middle; }}
    .form-container {{
        width: 70; height: auto; max-height: 85%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    .form-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    .form-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    .form-container Input {{ margin: 0 0 1 0; }}
    .form-container Select {{ margin: 0 0 1 0; }}
    .form-container TextArea {{ margin: 0 0 1 0; }}
    """

    def __init__(self, title: str = "", hint: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._form_title = title
        self._form_hint = hint or f"[{C_DIM}]Enter[/{C_DIM}] submit  [{C_DIM}]^H[/{C_DIM}] back"

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-container"):
            if self._form_title:
                yield Label(self._form_title, classes="form-title")
            yield from self.compose_form()
            yield Static(self._form_hint, classes="form-hint")

    def compose_form(self) -> ComposeResult:
        """Override: yield form field widgets."""
        return
        yield  # make it a generator

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_go_back(self) -> None:
        self.dismiss(None)


# ─── TabBar ────────────────────────────────────────────────────────

class TabBar(Static):
    """Horizontal tab bar with active tab highlighting.

    Each tab is (id, label, icon). The first tab (index 0) is always "Home".
    """

    class TabSelected(Message):
        def __init__(self, tab_id: str, index: int, **kwargs) -> None:
            super().__init__(**kwargs)
            self.tab_id = tab_id
            self.index = index

    class TabClosed(Message):
        def __init__(self, tab_id: str, index: int, **kwargs) -> None:
            super().__init__(**kwargs)
            self.tab_id = tab_id
            self.index = index

    DEFAULT_CSS = f"""
    TabBar {{
        height: 1;
        dock: top;
        background: {BG_BASE};
        padding: 0 1;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._tabs: list[tuple[str, str, str]] = [("home", "Home", "\u2302")]  # ⌂
        self._active_idx: int = 0

    @property
    def active_idx(self) -> int:
        return self._active_idx

    @property
    def active_tab_id(self) -> str:
        if self._active_idx < len(self._tabs):
            return self._tabs[self._active_idx][0]
        return "home"

    @property
    def tab_count(self) -> int:
        return len(self._tabs)

    def add_tab(self, tab_id: str, label: str, icon: str = "") -> int:
        """Add a tab. Returns the index. If tab already exists, returns its index."""
        for i, (tid, _, _) in enumerate(self._tabs):
            if tid == tab_id:
                return i
        self._tabs.append((tab_id, label, icon))
        self._render_tabs()
        return len(self._tabs) - 1

    def remove_tab(self, index: int) -> None:
        """Remove tab at index. Cannot remove index 0 (Home)."""
        if index <= 0 or index >= len(self._tabs):
            return
        tab_id = self._tabs[index][0]
        self._tabs.pop(index)
        if self._active_idx >= len(self._tabs):
            self._active_idx = len(self._tabs) - 1
        elif self._active_idx > index:
            self._active_idx -= 1
        elif self._active_idx == index:
            self._active_idx = max(0, index - 1)
        self._render_tabs()
        self.post_message(self.TabClosed(tab_id, index))

    def activate(self, index: int) -> None:
        """Switch to tab at index."""
        if 0 <= index < len(self._tabs):
            if index != self._active_idx:
                self._active_idx = index
                self._render_tabs()
                tab_id = self._tabs[index][0]
                self.post_message(self.TabSelected(tab_id, index))

    def activate_by_id(self, tab_id: str) -> None:
        for i, (tid, _, _) in enumerate(self._tabs):
            if tid == tab_id:
                self.activate(i)
                return

    def _render_tabs(self) -> None:
        parts = []
        for i, (tab_id, label, icon) in enumerate(self._tabs):
            prefix = f"{icon} " if icon else ""
            # Truncate long labels
            display = label[:20] + "\u2026" if len(label) > 20 else label
            if i == self._active_idx:
                parts.append(f"[bold {C_CYAN}] {prefix}{_rich_escape(display)} [/bold {C_CYAN}]")
            else:
                parts.append(f"[{C_DIM}] {prefix}{_rich_escape(display)} [/{C_DIM}]")
            if i < len(self._tabs) - 1:
                parts.append(f"[{C_FAINT}]\u2502[/{C_FAINT}]")
        self.update("".join(parts))

    def on_mount(self) -> None:
        self._render_tabs()


# ─── InlineInput ───────────────────────────────────────────────────

class InlineMode(str, Enum):
    SEARCH = "search"
    COMMAND = "command"
    NOTE = "note"
    RENAME = "rename"


class InlineInput(Input):
    """Consolidated inline input for search, command, note, rename modes."""

    BINDINGS = [Binding("escape", "cancel_inline", "Cancel", priority=True)]

    def __init__(self, mode: InlineMode = InlineMode.SEARCH, **kwargs) -> None:
        placeholders = {
            InlineMode.SEARCH: "Search...",
            InlineMode.COMMAND: ":",
            InlineMode.NOTE: "note: ",
            InlineMode.RENAME: "rename: ",
        }
        super().__init__(placeholder=placeholders.get(mode, ""), **kwargs)
        self.mode = mode

    def action_cancel_inline(self) -> None:
        self.value = ""
        self.display = False
        app = self.app
        if hasattr(app, '_focus_main_list'):
            app._focus_main_list()
