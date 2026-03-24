"""Modal screens — all ModalScreen subclasses for the orchestrator TUI.

Each screen is self-contained. They receive data via constructor params
and return results via dismiss(). No direct access to app state.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.table import Table as RichTable

log = logging.getLogger("orch.screens")

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Input,
    Label,
    OptionList,
    Rule,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from models import (
    Category, Link, Store, TodoItem, Workstream,
    _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, load_last_seen, mark_thread_seen
from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_GOLD, C_GREEN, C_LIGHT, C_ORANGE, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE, BG_CHROME, BG_RAISED, BG_SURFACE,
    CATEGORY_THEME,
    LINK_TYPE_ICONS, LINK_ORDER, LINK_KINDS,
    THROBBER_FRAMES,
    _category_markup, _link_icon,
    _activity_icon, _activity_badge, _is_session_seen, _is_today, _parse_iso,
    _colored_tokens, _token_color_markup,
    _short_model, _short_project,
    _render_session_option, _session_title,
    _render_todo_option, _render_notification_option,
    _render_notified_session_option, QUIET_SEPARATOR_LABEL, DEFERRED_SEPARATOR_LABEL, THINKING_SEPARATOR_LABEL,
    _render_content_search_result, tool_bar_legend,
    TODO_UNDONE_ICON, TODO_DONE_ICON,
    _rich_escape,
)
from actions import (
    ws_directories, ws_working_dir, open_file_picker, open_link, generate_tig_tigrc,
)
from terminal import TerminalWidget
from notifications import Notification, dismiss_notification, dismiss_all_for_dirs
from state import fuzzy_match, content_search, SessionSearchResult
from widgets import FuzzyPicker, FuzzyPickerScreen


def _label_with_legend(left: str, legend: str) -> RichTable:
    """Return a Rich grid with left text and right-aligned legend."""
    t = RichTable.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right")
    t.add_row(left, legend)
    return t


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


# ─── Messages ────────────────────────────────────────────────────────

class SessionsChanged(Message):
    """Posted on the app when the session list has been updated."""
    pass


class _SearchInput(Input):
    """Search input that cancels on backspace-when-empty or Escape."""

    BINDINGS = [
        Binding("escape", "cancel_search", "Cancel", priority=True),
        Binding("backspace,ctrl+h", "backspace_or_cancel", "Back", priority=True),
    ]

    def action_cancel_search(self):
        screen = self.screen
        if hasattr(screen, '_cancel_search'):
            screen._cancel_search()

    def action_backspace_or_cancel(self):
        if not self.value:
            self.action_cancel_search()
        else:
            self.action_delete_left()


# ─── Help Screen ────────────────────────────────────────────────────

class HelpScreen(FuzzyPickerScreen):
    """Searchable keyboard reference — built on FuzzyPickerScreen."""

    # Organized keybinding reference as (id, display) tuples
    _HELP_ITEMS = [
        # Navigation
        ("nav-down", f"[{C_YELLOW}]j / \u2193[/{C_YELLOW}]  Move down"),
        ("nav-up", f"[{C_YELLOW}]k / \u2191[/{C_YELLOW}]  Move up"),
        ("nav-pgdn", f"[{C_YELLOW}]Ctrl+D[/{C_YELLOW}]  Half-page down"),
        ("nav-pgup", f"[{C_YELLOW}]Ctrl+U[/{C_YELLOW}]  Half-page up"),
        ("nav-top", f"[{C_YELLOW}]g[/{C_YELLOW}]  Jump to top"),
        ("nav-bottom", f"[{C_YELLOW}]G[/{C_YELLOW}]  Jump to bottom"),
        ("nav-drill", f"[{C_YELLOW}]Ctrl+L[/{C_YELLOW}]  Drill in / resume"),
        ("nav-back", f"[{C_YELLOW}]Ctrl+H[/{C_YELLOW}]  Back / close"),
        ("nav-enter", f"[{C_YELLOW}]Enter[/{C_YELLOW}]  Confirm / open"),
        ("nav-tab", f"[{C_YELLOW}]H / L[/{C_YELLOW}]  Prev / next tab"),
        ("nav-closetab", f"[{C_YELLOW}]x[/{C_YELLOW}]  Close tab"),
        # Workstream actions
        ("ws-add", f"[{C_YELLOW}]a[/{C_YELLOW}]  Add new workstream"),
        ("ws-brain", f"[{C_YELLOW}]b[/{C_YELLOW}]  Brain dump (multi-line)"),
        ("ws-note", f"[{C_YELLOW}]n[/{C_YELLOW}]  Quick todo"),
        ("ws-spawn", f"[{C_YELLOW}]c[/{C_YELLOW}]  New Claude session"),
        ("ws-repo", f"[{C_YELLOW}]C[/{C_YELLOW}]  Spawn in repo"),
        ("ws-resume", f"[{C_YELLOW}]r[/{C_YELLOW}]  Resume session"),
        ("ws-link", f"[{C_YELLOW}]W[/{C_YELLOW}]  Add link"),
        ("ws-todos", f"[{C_YELLOW}]e[/{C_YELLOW}]  Todo list"),
        ("ws-rename", f"[{C_YELLOW}]E[/{C_YELLOW}]  Rename"),
        ("ws-open", f"[{C_YELLOW}]o[/{C_YELLOW}]  Open links"),
        ("ws-archive", f"[{C_YELLOW}]u[/{C_YELLOW}]  Archive / unarchive"),
        ("ws-delete", f"[{C_YELLOW}]d[/{C_YELLOW}]  Delete"),
        # Session
        ("sess-exit", f"[{C_YELLOW}]Ctrl+D[/{C_YELLOW}]  Exit Claude session"),
        ("sess-extract", f"[{C_YELLOW}]Ctrl+E[/{C_YELLOW}]  Extract todo from session"),
        ("sess-panels", f"[{C_YELLOW}]Ctrl+J/K[/{C_YELLOW}]  Cycle panels"),
        # Filters
        ("flt-all", f"[{C_YELLOW}]1[/{C_YELLOW}]  Filter: All"),
        ("flt-work", f"[{C_YELLOW}]2[/{C_YELLOW}]  Filter: Work"),
        ("flt-personal", f"[{C_YELLOW}]3[/{C_YELLOW}]  Filter: Personal"),
        ("flt-active", f"[{C_YELLOW}]4[/{C_YELLOW}]  Filter: Active"),
        ("flt-stale", f"[{C_YELLOW}]5[/{C_YELLOW}]  Filter: Stale"),
        ("flt-archived", f"[{C_YELLOW}]6[/{C_YELLOW}]  Filter: Archived"),
        ("flt-search", f"[{C_YELLOW}]/[/{C_YELLOW}]  Search workstreams"),
        # Sort
        ("srt-activity", f"[{C_YELLOW}]F1[/{C_YELLOW}]  Sort: Activity"),
        ("srt-updated", f"[{C_YELLOW}]F2[/{C_YELLOW}]  Sort: Updated"),
        ("srt-created", f"[{C_YELLOW}]F3[/{C_YELLOW}]  Sort: Created"),
        ("srt-category", f"[{C_YELLOW}]F4[/{C_YELLOW}]  Sort: Category"),
        ("srt-name", f"[{C_YELLOW}]F5[/{C_YELLOW}]  Sort: Name"),
        # Other
        ("cmd-palette", f"[{C_YELLOW}]:[/{C_YELLOW}]  Command palette"),
        ("preview", f"[{C_YELLOW}]p[/{C_YELLOW}]  Toggle preview"),
        ("refresh", f"[{C_YELLOW}]R[/{C_YELLOW}]  Refresh"),
        ("help", f"[{C_YELLOW}]?[/{C_YELLOW}]  This help"),
        ("quit", f"[{C_YELLOW}]q[/{C_YELLOW}]  Quit"),
    ]

    def __init__(self):
        super().__init__(
            title=f"[bold {C_PURPLE}]Keyboard Reference[/bold {C_PURPLE}]",
            hint=f"[{C_DIM}]Type to search  ^H back  Esc close[/{C_DIM}]",
        )

    def _get_items(self) -> list[tuple[str, str]]:
        return list(self._HELP_ITEMS)

    def _on_selected(self, item_id: str) -> None:
        # Selecting a help item just dismisses
        self.dismiss(None)


# ─── Quick Note Screen ───────────────────────────────────────────────

class QuickNoteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "^S save", priority=True),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back", priority=True),
    ]

    def action_go_back(self):
        self.dismiss(None)

    def action_save(self):
        text = self.query_one("#qnote-area", TextArea).text.strip()
        self.dismiss(text or None)

    DEFAULT_CSS = f"""
    QuickNoteScreen {{ align: center middle; }}
    #qnote-container {{
        width: 72; height: 14;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #qnote-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #qnote-area {{ height: 7; }}
    #qnote-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, ws: Workstream):
        super().__init__()
        self.ws = ws

    def compose(self) -> ComposeResult:
        with Vertical(id="qnote-container"):
            yield Label(f"Todo: {self.ws.name}", id="qnote-title")
            yield TextArea(id="qnote-area")
            yield Static(f"[{C_DIM}]^S[/{C_DIM}] save  [{C_DIM}]Esc[/{C_DIM}] cancel", id="qnote-hint")

    def on_mount(self):
        self.query_one("#qnote-area", TextArea).focus()



# ─── Vim OptionList Navigation Mixin ─────────────────────────────────

class _VimOptionListMixin:
    """Adds j/k, Ctrl+D/U, gg/G navigation to any screen with an OptionList."""

    _option_list_id: str = ""

    VIM_BINDINGS = [
        Binding("j,down", "cursor_down", show=False),
        Binding("k,up", "cursor_up", show=False),
        Binding("ctrl+d", "half_page_down", show=False),
        Binding("ctrl+u", "half_page_up", show=False),
        Binding("g", "jump_top", show=False),
        Binding("G", "jump_bottom", show=False),
    ]

    def _olist(self) -> OptionList:
        return self.query_one(f"#{self._option_list_id}", OptionList)  # type: ignore[attr-defined]

    def action_cursor_down(self):
        ol = self._olist()
        if ol.option_count == 0:
            return
        if ol.highlighted is None:
            ol.highlighted = 0
        elif ol.highlighted < ol.option_count - 1:
            ol.action_cursor_down()

    def action_cursor_up(self):
        ol = self._olist()
        if ol.option_count == 0:
            return
        if ol.highlighted is None:
            ol.highlighted = 0
        elif ol.highlighted > 0:
            ol.action_cursor_up()

    def action_half_page_down(self):
        self._olist().action_page_down()

    def action_half_page_up(self):
        self._olist().action_page_up()

    def action_jump_top(self):
        self._olist().action_first()

    def action_jump_bottom(self):
        self._olist().action_last()


# ─── Todo Screen ────────────────────────────────────────────────────

class TodoScreen(_VimOptionListMixin, ModalScreen[None]):
    """Interactive todo list — each item is a potential pending Claude session."""

    _option_list_id = "todo-list"

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("a", "add_todo", "Add"),
        Binding("enter", "spawn_todo", "Spawn", priority=True),
        Binding("space", "toggle_done", "Toggle done", priority=True),
        Binding("e", "edit_todo", "Edit"),
        Binding("c", "spawn_todo", "Spawn"),
        Binding("d", "delete_todo", "Delete"),
        Binding("E", "edit_context", "Edit context"),
        Binding("K", "move_up", "Move \u2191", show=False),
        Binding("J", "move_down", "Move \u2193", show=False),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss()

    DEFAULT_CSS = f"""
    TodoScreen {{ align: center middle; }}
    #todo-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}
    #todo-header {{
        height: 2;
        padding: 0 2;
        background: {BG_RAISED};
    }}
    #todo-header-name {{
        color: {C_PURPLE};
        text-style: bold;
    }}
    #todo-header-stats {{
        color: {C_DIM};
    }}
    #todo-list {{
        height: 1fr;
        margin: 0 1; padding: 0;
        border: none;
        background: {BG_BASE};
    }}
    #todo-list > .option-list--option-highlighted {{
        background: {BG_SURFACE};
    }}
    #todo-no-items {{
        padding: 2 3;
        color: {C_DIM};
    }}
    #todo-context {{
        height: auto;
        max-height: 5;
        padding: 1 3;
        background: {BG_RAISED};
        color: {C_DIM};
    }}
    #todo-help {{
        height: 1;
        padding: 0 2;
        background: {BG_CHROME};
        color: {C_DIM};
        dock: bottom;
    }}
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store
        self._active_items: list[TodoItem] = []
        self._rebuilding: bool = False

    @property
    def _app_state(self):
        return self.app.state

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-container"):
            with Vertical(id="todo-header"):
                yield Static("", id="todo-header-name")
                yield Static("", id="todo-header-stats")
            yield OptionList(id="todo-list")
            yield Static(f"[{C_DIM}]No todos \u2014 press [bold]a[/bold] to add[/{C_DIM}]", id="todo-no-items")
            yield Static("", id="todo-context")
            yield Static(self._render_help(), id="todo-help")

    def on_mount(self):
        self._rebuild()
        self.query_one("#todo-list", OptionList).focus()

    def _focused_olist(self) -> OptionList:
        return self.query_one("#todo-list", OptionList)

    def _olist(self) -> OptionList:
        return self._focused_olist()

    def _rebuild(self):
        """Reload data and rebuild the list."""
        self._rebuilding = True
        try:
            self._rebuild_inner()
        finally:
            self._rebuilding = False

    def _rebuild_inner(self):
        from state import AppState
        self.ws = self.store.get(self.ws.id) or self.ws
        self._active_items = AppState.active_todos(self.ws)

        olist = self.query_one("#todo-list", OptionList)
        no_items = self.query_one("#todo-no-items", Static)
        old_id = self._highlighted_item_id(olist, self._active_items)
        old_idx = olist.highlighted

        if self._active_items:
            olist.display = True
            no_items.display = False
            options = [Option(_render_todo_option(item), id=item.id) for item in self._active_items]
            olist.clear_options()
            olist.add_options(options)
            self._restore_highlight(olist, self._active_items, old_id, old_idx)
        else:
            olist.clear_options()
            olist.display = False
            no_items.display = True

        self._update_header()
        self._update_context_preview()
        # Restore focus — clear_options() can lose focus; defer until after refresh.
        self.call_after_refresh(olist.focus)

    @staticmethod
    def _highlighted_item_id(olist: OptionList, items: list[TodoItem]) -> str | None:
        if olist.highlighted is not None and olist.option_count > 0:
            try:
                return olist.get_option_at_index(olist.highlighted).id
            except Exception:
                pass
        return None

    @staticmethod
    def _restore_highlight(olist: OptionList, items: list[TodoItem], item_id: str | None, old_idx: int | None = None):
        if not olist.option_count:
            return
        if item_id:
            for i, t in enumerate(items):
                if t.id == item_id:
                    olist.highlighted = i
                    return
        # Item was removed — keep cursor at same position, clamped.
        if old_idx is not None:
            olist.highlighted = min(old_idx, olist.option_count - 1)
        else:
            olist.highlighted = 0

    def _update_header(self):
        na = len(self._active_items)
        done = sum(1 for t in self._active_items if t.done)
        crystal = sum(1 for t in self._active_items if getattr(t, "origin", "manual") == "crystallized")
        name = _rich_escape(self.ws.name)
        self.query_one("#todo-header-name", Static).update(
            f"[bold {C_PURPLE}]\u25c6 Todos[/bold {C_PURPLE}]  [{C_DIM}]{name}[/{C_DIM}]"
        )
        parts = []
        pending = na - done
        if pending:
            parts.append(f"[{C_LIGHT}]{pending}[/{C_LIGHT}] pending")
        if done:
            parts.append(f"[{C_GREEN}]{done}[/{C_GREEN}] done")
        if crystal:
            parts.append(f"[{C_GOLD}]{crystal}[/{C_GOLD}] crystallized")
        sep = f" [{C_FAINT}]\u00b7[/{C_FAINT}] "
        stats = sep.join(parts) if parts else f"[{C_DIM}]empty[/{C_DIM}]"
        self.query_one("#todo-header-stats", Static).update(stats)

    def _update_context_preview(self):
        item = self._highlighted_item()
        ctx_widget = self.query_one("#todo-context", Static)
        if item and item.context:
            is_crystal = getattr(item, "origin", "manual") == "crystallized"
            all_lines = item.context.strip().split("\n")
            preview = _rich_escape("\n".join(all_lines[:4]))
            if len(all_lines) > 4:
                preview += f"\n[{C_DIM}]...[/{C_DIM}]"
            label_color = C_GOLD if is_crystal else C_BLUE
            ctx_widget.update(f"[{label_color}]Context:[/{label_color}] {preview}")
        elif item:
            ctx_widget.update(f"[{C_DIM}]No context \u2014 E to add[/{C_DIM}]")
        else:
            ctx_widget.update("")

    def _highlighted_item(self) -> TodoItem | None:
        olist = self.query_one("#todo-list", OptionList)
        if olist.highlighted is not None and olist.highlighted < len(self._active_items):
            return self._active_items[olist.highlighted]
        return None

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if not self._rebuilding:
            self._update_context_preview()

    def _render_help(self) -> str:
        pairs = [
            ("a", "add"), ("Enter", "spawn"), ("Space", "done"), ("e", "edit"),
            ("d", "del"), ("E", "ctx"), ("J/K", "reorder"), ("q", "back"),
        ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    # ── Actions ────────────────────────────────────────────────────

    def action_add_todo(self):
        def on_text(text: str | None):
            if text and text.strip():
                self._app_state.add_todo(self.ws.id, text.strip())
                self._rebuild()
                olist = self.query_one("#todo-list", OptionList)
                olist.focus()
                if olist.option_count > 0:
                    olist.highlighted = olist.option_count - 1
                self.app.notify("Todo added", timeout=1)
        self.app.push_screen(QuickNoteScreen(self.ws), callback=on_text)

    def action_toggle_done(self):
        item = self._highlighted_item()
        if not item:
            return
        self._app_state.toggle_todo(self.ws.id, item.id)
        # Replace prompts in place — avoids clear_options() focus loss.
        from state import AppState
        self.ws = self.store.get(self.ws.id) or self.ws
        new_items = AppState.active_todos(self.ws)
        olist = self.query_one("#todo-list", OptionList)
        for i, t in enumerate(new_items):
            olist.replace_option_prompt_at_index(i, _render_todo_option(t))
        new_idx = next((i for i, t in enumerate(new_items) if t.id == item.id), 0)
        olist.highlighted = new_idx
        self._active_items = new_items
        self._update_header()
        self._update_context_preview()

    def action_edit_todo(self):
        item = self._highlighted_item()
        if not item:
            return
        def on_text(text: str | None):
            if text and text.strip():
                self._app_state.edit_todo(self.ws.id, item.id, text=text.strip())
                self._rebuild()
        self.app.push_screen(_TodoEditScreen(item.text), callback=on_text)

    def action_delete_todo(self):
        item = self._highlighted_item()
        if not item:
            return
        self._app_state.delete_todo(self.ws.id, item.id)
        self._rebuild()
        self.app.notify("Todo deleted", timeout=1)

    def action_spawn_todo(self):
        item = self._highlighted_item()
        if not item:
            return
        prompt = item.text
        if item.context:
            prompt = f"{item.text}\n\n{item.context}"
        self._app_state.toggle_todo(self.ws.id, item.id)  # mark done
        self._rebuild()
        self.app.launch_claude_session(self.ws, prompt=prompt)

    def action_edit_context(self):
        item = self._highlighted_item()
        if not item:
            return
        def on_close(_):
            self.ws = self.store.get(self.ws.id) or self.ws
            self._rebuild()
        self.app.push_screen(_TodoContextScreen(self.ws, self.store, item.id), callback=on_close)

    def action_move_up(self):
        item = self._highlighted_item()
        if item:
            self._app_state.reorder_todo(self.ws.id, item.id, -1)
            self._rebuild()

    def action_move_down(self):
        item = self._highlighted_item()
        if item:
            self._app_state.reorder_todo(self.ws.id, item.id, 1)
            self._rebuild()

class _TodoEditScreen(ModalScreen[str | None]):
    """Single-line input pre-filled with existing text."""
    BINDINGS = [
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back", priority=True),
    ]

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    _TodoEditScreen {{ align: center middle; }}
    #todo-edit-container {{
        width: 70; height: 9;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #todo-edit-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #todo-edit-input {{ height: 3; }}
    #todo-edit-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, initial_text: str):
        super().__init__()
        self._initial = initial_text

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-edit-container"):
            yield Label("Edit Todo", id="todo-edit-title")
            yield Input(value=self._initial, id="todo-edit-input")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] save  [{C_DIM}]^H[/{C_DIM}] back", id="todo-edit-hint")

    def on_mount(self):
        self.query_one("#todo-edit-input", Input).focus()

    @on(Input.Submitted, "#todo-edit-input")
    def on_submit(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class _TodoContextScreen(ModalScreen[None]):
    """TextArea editor for a todo item's context field."""
    BINDINGS = [
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back", priority=True),
    ]

    def action_go_back(self):
        self.action_save_and_close()

    DEFAULT_CSS = f"""
    _TodoContextScreen {{ align: center middle; }}
    #todo-ctx-container {{
        width: 80; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #todo-ctx-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #todo-ctx-editor {{ height: 15; margin: 0 0 1 0; }}
    #todo-ctx-hint {{ text-align: center; color: {C_DIM}; }}
    """

    def __init__(self, ws: Workstream, store: Store, todo_id: str):
        super().__init__()
        self.ws = ws
        self.store = store
        self.todo_id = todo_id
        self._item = next((t for t in ws.todos if t.id == todo_id), None)

    def compose(self) -> ComposeResult:
        text = self._item.context if self._item else ""
        label = self._item.text[:40] if self._item else "?"
        with Vertical(id="todo-ctx-container"):
            yield Label(f"Context: {label}", id="todo-ctx-title")
            yield TextArea(text, id="todo-ctx-editor")
            yield Static(f"[{C_DIM}]^H[/{C_DIM}] save & back", id="todo-ctx-hint")

    def action_save_and_close(self):
        if self._item:
            editor = self.query_one("#todo-ctx-editor", TextArea)
            new_ctx = editor.text.strip()
            from state import AppState
            state = self.app.state
            state.edit_todo(self.ws.id, self.todo_id, context=new_ctx)
        self.dismiss()


# ─── Links Screen ───────────────────────────────────────────────────

class LinksScreen(_VimOptionListMixin, ModalScreen[None]):
    _option_list_id = "links-list"
    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("enter", "open_link", "Open"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss()

    DEFAULT_CSS = f"""
    LinksScreen {{ align: center middle; }}
    #links-container {{
        width: 80; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #links-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #links-list {{ height: auto; max-height: 20; }}
    #links-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="links-container"):
            yield Label(f"Links: {self.ws.name}", id="links-title")
            options = []
            for i, lnk in enumerate(self.ws.links):
                icon = _link_icon(lnk.kind)
                options.append(Option(f"{icon}  [{_rich_escape(lnk.kind)}] {_rich_escape(lnk.value)}", id=str(i)))
            if not options:
                options.append(Option("(no links)", id="none", disabled=True))
            yield OptionList(*options, id="links-list")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] open  [{C_DIM}]^H[/{C_DIM}] back", id="links-hint")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_open_link()

    def action_open_link(self):
        option_list = self.query_one("#links-list", OptionList)
        idx = option_list.highlighted
        if idx is not None and idx < len(self.ws.links):
            link = self.ws.links[idx]
            open_link(link, ws=self.ws, app=self.app)
            self.app.notify(f"Opening {link.label}...", timeout=2)


# ─── Add Screen ─────────────────────────────────────────────────────

from widgets import ModalForm


class AddScreen(ModalForm):
    """Create a new workstream. Built on ModalForm base."""

    def __init__(self):
        super().__init__(
            title="New Workstream",
            hint=f"[{C_DIM}]Enter[/{C_DIM}] create  [{C_DIM}]Tab[/{C_DIM}] next field  [{C_DIM}]^H[/{C_DIM}] back",
        )

    def compose_form(self) -> ComposeResult:
        yield Input(placeholder="Name", id="add-name")
        yield Input(placeholder="Description (optional)", id="add-desc")
        yield Select(
            [(c.value, c) for c in Category],
            value=Category.PERSONAL,
            id="add-category",
        )

    def on_mount(self):
        self.query_one("#add-name", Input).focus()

    @on(Input.Submitted, "#add-name")
    def on_name_submitted(self):
        self.query_one("#add-desc", Input).focus()

    @on(Input.Submitted, "#add-desc")
    def on_desc_submitted(self):
        self._create()

    def _create(self):
        name = self.query_one("#add-name", Input).value.strip()
        if not name:
            self.app.notify("Name cannot be empty", severity="error", timeout=2)
            return
        desc = self.query_one("#add-desc", Input).value.strip()
        cat = self.query_one("#add-category", Select).value
        self.dismiss(Workstream(name=name, description=desc, category=cat))


# ─── Detail Screen ──────────────────────────────────────────────────

class DetailScreen(_VimOptionListMixin, ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Back", priority=True),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("ctrl+l", "go_forward", "^L resume"),
        # s/S freed (was: cycle status — removed, status is auto-derived)
        Binding("c", "spawn", "Spawn"),
        Binding("r", "resume", "Resume"),
        Binding("n", "quick_note", "+todo"),
        Binding("W", "add_link", "Link+"),
        Binding("e", "open_todos", "Todos"),
        Binding("o", "open_links", "Open links"),
        Binding("f", "file_picker", "Files"),
        Binding("x", "archive", "Archive"),
        Binding("p", "peek_session", "Peek", priority=True),
        Binding("h", "go_back", show=False),
        Binding("enter,l", "select_session", show=False),
        Binding("y", "yank_resume_cmd", "Yank cmd"),
        Binding("z", "defer_session", "Defer", show=False),
        Binding("d", "dismiss_notification", "Dismiss", show=False),
        Binding("D", "dismiss_all_notifications", "Dismiss all", show=False),
        Binding("/", "search", "Search", show=False, priority=True),
        # Delegate to app — OptionList type-ahead consumes these before
        # they reach app-level bindings in a ModalScreen.
        Binding("colon", "command_palette", ":", show=False),
        Binding("question_mark", "help", "?", show=False),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    DetailScreen {{ align: center middle; }}
    #detail-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}
    #detail-tab-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_CHROME};
    }}
    #detail-title {{
        text-style: bold;
        background: {BG_RAISED};
        padding: 0 2;
    }}
    #detail-desc {{
        color: {C_DIM};
        background: {BG_RAISED};
        padding: 0 2;
    }}
    #detail-lists {{
        height: auto; max-height: 70%;
    }}
    .detail-list-pane {{
        width: 1fr;
        border: blank;
    }}
    #detail-sessions-pane {{
        width: 2fr;
    }}
    .detail-list-pane.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
    }}
    .detail-list-label {{
        padding: 0 3;
        color: {C_BLUE};
        text-style: bold;
    }}
    #detail-sessions, #detail-archived {{
        height: auto;
        margin: 0 1; padding: 0;
        border: none;
        background: {BG_BASE};
    }}
    #detail-sessions > .option-list--option-highlighted,
    #detail-archived > .option-list--option-highlighted {{
        background: #101010;
    }}
    #detail-search-input {{
        display: none;
        margin: 0 1;
        height: auto;
        background: {BG_BASE};
        border: none;
        padding: 0 2;
    }}
    #detail-search-input.visible {{
        display: block;
    }}
    #detail-search-input:focus {{
        border: none;
    }}
    #detail-no-sessions, #detail-no-archived {{
        padding: 1 3;
        color: {C_DIM};
    }}
    #detail-archived-pane {{
        display: none;
    }}
    #detail-lower {{
        height: 1fr;
        background: {BG_RAISED};
    }}
    #detail-scroll {{
        width: 3fr;
        border: blank;
    }}
    #detail-scroll.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
    }}
    .detail-tig-wrap {{
        height: 1fr;
        border: blank;
        background: {BG_RAISED};
    }}
    .detail-tig-wrap.pane-focused {{
        border: round {C_BLUE};
        background: {BG_RAISED};
    }}
    #detail-tig-status, #detail-tig-log {{
        height: 1fr;
    }}
    #detail-body {{
        padding: 1 3;
    }}
    #detail-feed-pane {{
        display: none;
    }}
    #detail-feed-pane.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
    }}
    .detail-feed-label {{
        padding: 0 3;
        color: {C_BLUE};
        text-style: bold;
    }}
    #detail-feed {{
        height: auto;
        margin: 0 1; padding: 0;
        border: none;
        background: {BG_BASE};
    }}
    #detail-feed > .option-list--option-highlighted {{
        background: #101010;
    }}
    #detail-no-feed {{
        padding: 1 3;
        color: {C_DIM};
    }}
    #detail-help {{
        height: 1;
        padding: 0 2;
        background: {BG_CHROME};
        color: {C_DIM};
        dock: bottom;
    }}
    """

    _option_list_id = "detail-sessions"

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store
        self._detail_sessions: list[ClaudeSession] = []
        self._archived_sessions: list[ClaudeSession] = []
        self._deferred_set: set[str] = set(ws.deferred_sessions)
        self._feed_notifications: list[Notification] = []
        self._session_notifications: dict[str, Notification] = {}  # session_id -> latest notif
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}
        self._active_pane: str = "sessions"
        self._animating_sessions: list[tuple[int, ThreadActivity]] = []
        self._animating_archived: list[tuple[int, ThreadActivity]] = []
        self._notified_count: int = 0
        self._content_cache: dict[str, list] = {}  # session_id -> list[SessionMessage]
        self._content_ready: bool = False
        self._content_results: list[SessionSearchResult] = []
        self._content_search_active: bool = False
        # Full unfiltered lists (set once sessions are loaded)
        self._all_sessions: list[ClaudeSession] = []
        self._all_archived: list[ClaudeSession] = []
        self._peek_mode: bool = False
        self._peek_session_id: str | None = None
        self._loading_frame: int = 0
        self._loading_timer = None
        self._throbber_timer = None
        self._refresh_timer = None
        self._mounted_once: bool = False  # skip first on_screen_resume (on_mount handles it)
        # Tig sidebar
        self._cwd = ws_working_dir(ws)
        self._tigrc_path: str | None = None
        self._tig_env: dict[str, str] = {}
        self._sidebar_enabled = self._detect_git_sidebar()
        if self._sidebar_enabled:
            self._tigrc_path = generate_tig_tigrc(subtle=True)
            self._tig_env = {"TIGRC_USER": self._tigrc_path, "GIT_OPTIONAL_LOCKS": "0"}

    def _detect_git_sidebar(self) -> bool:
        """Return True if the workstream has a git repo we can show tig for."""
        import shutil
        if not shutil.which("tig"):
            return False
        if not self.ws.repo_path and not ws_directories(self.ws):
            return False
        try:
            result = subprocess.run(
                ["git", "-C", self._cwd, "rev-parse", "--git-dir"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-container"):
            yield Static("", id="detail-tab-bar")
            yield Static(
                self._render_title() + "  " + self._render_meta(),
                id="detail-title",
            )
            if self.ws.description:
                yield Static(self.ws.description, id="detail-desc")

            with Horizontal(id="detail-lists"):
                with Vertical(id="detail-sessions-pane", classes="detail-list-pane"):
                    yield Static(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]", id="detail-sessions-label", classes="detail-list-label")
                    yield _SearchInput(placeholder="search...", id="detail-search-input")
                    yield OptionList(id="detail-sessions")
                    yield Static(f"[{C_DIM}]No sessions[/{C_DIM}]", id="detail-no-sessions")
                with Vertical(id="detail-archived-pane", classes="detail-list-pane"):
                    yield Static(f"[{C_DIM}]Archived[/{C_DIM}]", id="detail-archived-label", classes="detail-list-label")
                    yield OptionList(id="detail-archived")
                    yield Static(f"[{C_DIM}]Empty[/{C_DIM}]", id="detail-no-archived")

            with Horizontal(id="detail-lower"):
                if self._sidebar_enabled:
                    with Vertical(id="detail-tig-status-wrap", classes="detail-tig-wrap"):
                        yield TerminalWidget(
                            command="tig status",
                            env=self._tig_env,
                            cwd=self._cwd,
                            passthrough_keys={"ctrl+j", "ctrl+k", "ctrl+h", "backspace"},
                            id="detail-tig-status",
                        )
                    with Vertical(id="detail-tig-log-wrap", classes="detail-tig-wrap"):
                        yield TerminalWidget(
                            command="tig",
                            env=self._tig_env,
                            cwd=self._cwd,
                            passthrough_keys={"ctrl+j", "ctrl+k", "ctrl+h", "backspace"},
                            id="detail-tig-log",
                        )
                else:
                    with VerticalScroll(id="detail-scroll"):
                        yield Static(self._render_body(), id="detail-body")
                # Feed pane kept in DOM but hidden — notifications now inline in session list
                with Vertical(id="detail-feed-pane"):
                    yield Static(self._render_feed_label(), id="detail-feed-label", classes="detail-feed-label")
                    yield OptionList(id="detail-feed")
                    yield Static(f"[{C_DIM}]No notifications[/{C_DIM}]", id="detail-no-feed")

            yield Static(self._render_help(), id="detail-help")

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._mark_all_seen()
        self._load_feed()  # must run before _load_detail_sessions so notification map is ready
        self._load_detail_sessions()
        # Update header with session stats now that _detail_sessions is populated
        self.query_one("#detail-title", Static).update(self._render_title() + "  " + self._render_meta())
        try:
            self.query_one("#detail-body", Static).update(self._render_body())
        except Exception:
            pass  # body not present when tig sidebar is active
        if self._sidebar_enabled:
            for tw in self.query(TerminalWidget):
                try:
                    tw.start()
                except Exception as e:
                    log.error("DetailScreen: failed to start tig terminal %s: %s", tw.id, e)
        self.query_one("#detail-sessions", OptionList).focus()
        self._update_pane_labels()
        self._refresh_timer = self.set_interval(30, self._periodic_refresh)
        self._throbber_timer = self.set_interval(0.15, self._tick_throbber)
        self._mounted_once = True
        if self._sessions_loading():
            self._loading_timer = self.set_interval(0.12, self._tick_loading)

    def on_screen_resume(self):
        """Lightweight refresh when returning to a cached screen."""
        if not self._mounted_once:
            return  # on_mount handles first activation
        # Refresh workstream data (may have changed while screen was suspended)
        self.ws = self.store.get(self.ws.id) or self.ws
        self._last_seen_cache = load_last_seen()
        self._mark_all_seen()
        self._load_feed()
        self._load_detail_sessions()
        # Sync-refresh live sessions so activity badges are correct immediately
        # (avoids brief stale "your turn" flash when returning from session view)
        self._sync_refresh_live()
        self.query_one("#detail-title", Static).update(self._render_title() + "  " + self._render_meta())
        try:
            self.query_one("#detail-body", Static).update(self._render_body())
        except Exception:
            pass
        self._update_pane_labels()
        self.query_one("#detail-sessions", OptionList).focus()
        # Restart periodic refresh
        self._refresh_timer = self.set_interval(30, self._periodic_refresh)

    def on_screen_suspend(self):
        """Pause timers when screen is covered or dismissed."""
        if hasattr(self, '_refresh_timer') and self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def on_unmount(self):
        if self._sidebar_enabled:
            for tw in self.query(TerminalWidget):
                try:
                    tw.stop()
                except Exception:
                    pass
        if self._tigrc_path:
            try:
                os.unlink(self._tigrc_path)
            except OSError:
                pass

    def _mark_all_seen(self):
        """Mark non-pending sessions in this workstream as seen right now — batched.

        Sessions that are waiting for a response (AWAITING_INPUT / RESPONSE_READY)
        are intentionally skipped so their green badge stays visible until the
        user actually opens/responds to them.
        """
        from threads import session_activity, ThreadActivity, save_last_seen
        app = self.app
        if hasattr(app, 'state'):
            sessions = app.state.sessions_for_ws(self.ws, include_archived_sessions=True)
        else:
            sessions = self._detail_sessions + self._archived_sessions
        if not sessions:
            return
        # Batch: single read + single write instead of N reads + N writes
        data = load_last_seen()
        now = datetime.now(timezone.utc).isoformat()
        pending = {ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY}
        for s in sessions:
            if session_activity(s) not in pending:
                data[s.session_id] = now
        save_last_seen(data)
        self._last_seen_cache = data

    def _sync_refresh_live(self):
        """Tail-read live sessions synchronously to prevent stale activity badges.

        Only reads the last 8KB of each live session's JSONL — fast enough
        for the main thread (~µs per file) and avoids the 1-2 second gap
        before the next background liveness refresh corrects the display.
        """
        from sessions import refresh_session_tail
        for s in self._detail_sessions:
            if s.is_live:
                refresh_session_tail(s)
        for s in self._archived_sessions:
            if s.is_live:
                refresh_session_tail(s)

    def _periodic_refresh(self):
        """Single merged timer: liveness check + feed poll."""
        self._do_liveness_refresh()

    @work(thread=True, exclusive=True, group="detail_liveness")
    def _do_liveness_refresh(self):
        from actions import refresh_liveness
        from sessions import refresh_session_tail
        refresh_liveness(self._detail_sessions)
        refresh_liveness(self._archived_sessions)
        for s in self._detail_sessions:
            if s.is_live:
                refresh_session_tail(s)
        for s in self._archived_sessions:
            if s.is_live:
                refresh_session_tail(s)
        # Build fingerprint to check if anything changed
        fp = self._session_fingerprint()
        self.app.call_from_thread(self._apply_liveness_result, fp)

    def _session_fingerprint(self) -> frozenset:
        """Quick fingerprint of session state to detect changes."""
        return frozenset(
            (s.session_id, s.is_live, s.last_message_role, s.message_count)
            for s in self._detail_sessions
        )

    def _apply_liveness_result(self, new_fp: frozenset):
        """Apply liveness changes only if session state actually changed."""
        old_fp = getattr(self, '_last_session_fp', None)
        self._last_session_fp = new_fp
        if old_fp != new_fp:
            with self.app.batch_update():
                self._rebuild_all_options()
        # Also poll feed
        old_notif_sids = set(self._session_notifications.keys())
        self._load_feed()
        new_notif_sids = set(self._session_notifications.keys())
        if old_notif_sids != new_notif_sids:
            self._load_detail_sessions()

    def _session_line_width(self, olist_id: str = "#detail-sessions") -> int:
        """Return usable character width for session rows, or 0 for default."""
        try:
            olist = self.query_one(olist_id, OptionList)
            w = olist.size.width
            return w - 2 if w > 20 else 0  # subtract padding/scrollbar; 0 = use default
        except Exception:
            return 0

    def _focused_olist(self) -> OptionList:
        if self._active_pane == "archived":
            return self.query_one("#detail-archived", OptionList)
        return self.query_one("#detail-sessions", OptionList)

    def _olist(self) -> OptionList:
        return self._focused_olist()

    def action_focus_sessions(self):
        self._active_pane = "sessions"
        olist = self.query_one("#detail-sessions", OptionList)
        olist.focus()
        self._update_pane_labels()

    def action_focus_archived(self):
        if not self._archived_sessions:
            return
        self._active_pane = "archived"
        olist = self.query_one("#detail-archived", OptionList)
        olist.focus()
        self._update_pane_labels()

    # Map pane names to the container widget that should get the focus border
    _PANE_BORDER_CONTAINERS = {
        "sessions": "#detail-sessions-pane",
        "archived": "#detail-archived-pane",
        "body": "#detail-scroll",
        "feed": "#detail-feed-pane",
        "tig-status": "#detail-tig-status-wrap",
        "tig-log": "#detail-tig-log-wrap",
    }

    def _sessions_loading(self) -> bool:
        """True if the initial session discovery hasn't completed yet."""
        app = self.app
        return hasattr(app, 'state') and not app.state.sessions_loaded

    def _tick_throbber(self):
        """Animate thinking-session icons at ~10fps."""
        if self._animating_sessions or self._animating_archived:
            self._throbber_frame += 1
            self._rebuild_all_options()

    def _tick_loading(self):
        """Animate the loading spinner while sessions are being discovered."""
        if not self._sessions_loading():
            if self._loading_timer:
                self._loading_timer.stop()
                self._loading_timer = None
            return
        self._loading_frame += 1
        from rendering import THROBBER_FRAMES
        frame = THROBBER_FRAMES[self._loading_frame % len(THROBBER_FRAMES)]
        no_sess = self.query_one("#detail-no-sessions", Static)
        no_sess.update(f"[{C_CYAN}]{frame}[/{C_CYAN}] [{C_DIM}]Discovering sessions...[/{C_DIM}]")
        self._update_pane_labels()

    def _update_pane_labels(self):
        sess_label = self.query_one("#detail-sessions-label", Static)
        arch_label = self.query_one("#detail-archived-label", Static)
        n_active = len(self._detail_sessions)
        n_archived = len(self._archived_sessions)
        active_sids = {s.session_id for s in self._detail_sessions}
        n_notified = sum(1 for sid in self._session_notifications if sid in active_sids)

        legend = tool_bar_legend()
        notif_badge = f" [{C_GREEN}]({n_notified} new)[/{C_GREEN}]" if n_notified else ""
        loading = self._sessions_loading() and n_active == 0
        if loading:
            from rendering import THROBBER_FRAMES
            frame = THROBBER_FRAMES[self._loading_frame % len(THROBBER_FRAMES)]
            spinner = f" [{C_CYAN}]{frame}[/{C_CYAN}]"
        else:
            spinner = ""
        count_str = f"({n_active})" if not loading else ""
        if self._active_pane == "sessions":
            left = f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]{spinner} [{C_DIM}]{count_str}[/{C_DIM}]{notif_badge}"
        else:
            left = f"[{C_DIM}]Sessions {count_str}[/{C_DIM}]{spinner}{notif_badge}"
        sess_label.update(_label_with_legend(left, legend))

        if self._active_pane == "archived":
            arch_label.update(f"[bold {C_BLUE}]Archived[/bold {C_BLUE}] [{C_DIM}]({n_archived})[/{C_DIM}]")
        else:
            arch_label.update(f"[{C_DIM}]Archived ({n_archived})[/{C_DIM}]")

        # Toggle focus border on pane containers
        for pane_name, selector in self._PANE_BORDER_CONTAINERS.items():
            try:
                container = self.query_one(selector)
                if pane_name == self._active_pane:
                    container.add_class("pane-focused")
                else:
                    container.remove_class("pane-focused")
            except Exception:
                pass

    # _refresh_session_liveness replaced by _schedule_liveness_refresh + _do_liveness_refresh above

    def on_sessions_changed(self, event: SessionsChanged):
        if self._loading_timer and not self._sessions_loading():
            self._loading_timer.stop()
            self._loading_timer = None
        # Debounce: coalesce rapid-fire SessionsChanged into one refresh
        if hasattr(self, '_sessions_changed_timer') and self._sessions_changed_timer:
            self._sessions_changed_timer.stop()
        self._sessions_changed_timer = self.set_timer(0.1, self._refresh)

    def _update_animating_cache(self):
        anim_types = (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT)
        self._animating_sessions = []
        for i, s in enumerate(self._detail_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in anim_types:
                self._animating_sessions.append((i, act))
        self._animating_archived = []
        for i, s in enumerate(self._archived_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in anim_types:
                self._animating_archived.append((i, act))

    def _rebuild_all_options(self):
        """Re-render every session option in place (preserves highlight)."""
        if self._content_search_active or self._peek_mode:
            return  # Don't overwrite search results or peek content

        # Re-categorize sessions
        notified = []
        elevated = []
        quiet = []
        for s in self._detail_sessions:
            if s.session_id in self._session_notifications:
                notified.append(s)
            else:
                act = session_activity(s, self._last_seen_cache)
                seen = _is_session_seen(s, self._last_seen_cache)
                if not seen and act in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
                    elevated.append(s)
                else:
                    quiet.append(s)
        notified.sort(key=lambda s: self._session_notifications[s.session_id].dt, reverse=True)
        elevated.sort(key=lambda s: s.last_activity or "", reverse=True)
        quiet.sort(key=lambda s: s.last_activity or "", reverse=True)
        quiet_active = [s for s in quiet if s.session_id not in self._deferred_set]
        quiet_deferred = [s for s in quiet if s.session_id in self._deferred_set]
        quiet_thinking = [s for s in quiet_active if session_activity(s, self._last_seen_cache) == ThreadActivity.THINKING]
        quiet_other = [s for s in quiet_active if s not in quiet_thinking]
        all_elevated = notified + elevated

        # If the notified/elevated/quiet structure changed, the in-place index
        # mapping is invalid (separator may have moved or appeared/disappeared).
        # Fall back to a full rebuild to avoid stale/duplicate entries.
        has_separator = bool(all_elevated and (quiet_active or quiet_deferred))
        has_thinking_sep = bool(quiet_thinking)
        has_deferred_sep = bool(quiet_deferred)
        expected_count = (
            len(self._detail_sessions)
            + (1 if has_separator else 0)
            + (1 if has_thinking_sep else 0)
            + (1 if has_deferred_sep else 0)
        )
        olist = self.query_one("#detail-sessions", OptionList)
        if len(all_elevated) != self._notified_count or olist.option_count != expected_count:
            old_sid = self._highlighted_session_id(olist)
            old_idx = olist.highlighted
            self._build_session_list()
            self._restore_highlight_by_sid(olist, self._detail_sessions, old_sid, old_idx)
        else:
            # Structure unchanged — safe to update in place.
            # Wrap in batch_update to coalesce N cache clears into one render.
            with self.app.batch_update():
                lw = self._session_line_width("#detail-sessions")
                idx = 0
                for s in notified:
                    if idx < olist.option_count:
                        act = session_activity(s, self._last_seen_cache)
                        seen = _is_session_seen(s, self._last_seen_cache)
                        notif = self._session_notifications[s.session_id]
                        prompt = _render_notified_session_option(
                            s, act, notif, self._throbber_frame,
                            ws_repo_path=self.ws.repo_path, seen=seen,
                            line_width=lw,
                        )
                        olist.replace_option_prompt_at_index(idx, prompt)
                    idx += 1

                for s in elevated:
                    if idx < olist.option_count:
                        act = session_activity(s, self._last_seen_cache)
                        seen = _is_session_seen(s, self._last_seen_cache)
                        prompt = _render_notified_session_option(
                            s, act, None, self._throbber_frame,
                            ws_repo_path=self.ws.repo_path, seen=seen,
                            line_width=lw,
                        )
                        olist.replace_option_prompt_at_index(idx, prompt)
                    idx += 1

                if has_separator:
                    idx += 1  # skip quiet separator

                for s in quiet_other:
                    if idx < olist.option_count:
                        act = session_activity(s, self._last_seen_cache)
                        seen = _is_session_seen(s, self._last_seen_cache)
                        prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
                        olist.replace_option_prompt_at_index(idx, prompt)
                    idx += 1

                if has_thinking_sep:
                    idx += 1  # skip thinking separator
                    for s in quiet_thinking:
                        if idx < olist.option_count:
                            act = session_activity(s, self._last_seen_cache)
                            seen = _is_session_seen(s, self._last_seen_cache)
                            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
                            olist.replace_option_prompt_at_index(idx, prompt)
                        idx += 1

                if has_deferred_sep:
                    idx += 1  # skip deferred separator
                    for s in quiet_deferred:
                        if idx < olist.option_count:
                            act = session_activity(s, self._last_seen_cache)
                            seen = _is_session_seen(s, self._last_seen_cache)
                            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw, deferred=True)
                            olist.replace_option_prompt_at_index(idx, prompt)
                        idx += 1

                alw = self._session_line_width("#detail-archived")
                arch_olist = self.query_one("#detail-archived", OptionList)
                limit = getattr(self, '_archived_show_count', self._ARCHIVED_PAGE_SIZE)
                for i, s in enumerate(self._archived_sessions[:limit]):
                    if i < arch_olist.option_count:
                        act = session_activity(s, self._last_seen_cache)
                        seen = _is_session_seen(s, self._last_seen_cache)
                        prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=alw, archived=True)
                        arch_olist.replace_option_prompt_at_index(i, prompt)


    @staticmethod
    def _parse_ts(ts: str) -> datetime:
        """Parse a timestamp string, returning a UTC-aware datetime."""
        ts = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    def _load_detail_sessions(self):
        app = self.app
        if hasattr(app, 'state'):
            all_sessions = app.state.sessions_for_ws(self.ws, include_archived_sessions=True)
            archived = self.ws.archived_sessions
            log.debug("load_detail: ws=%s all=%d archived_map=%s",
                      self.ws.name, len(all_sessions),
                      {k: v for k, v in archived.items()})
            revived = set()
            for s in all_sessions:
                if s.session_id in archived:
                    archived_at = archived[s.session_id]
                    last_act = s.last_activity or ""
                    if last_act and archived_at and self._parse_ts(last_act) > self._parse_ts(archived_at):
                        revived.add(s.session_id)
            if revived:
                log.debug("load_detail: reviving %s", revived)
                for sid in revived:
                    del self.ws.archived_sessions[sid]
                self.store.update(self.ws)
            # Auto-undefer: if a new user message arrived after deferral, wake the session
            undeferred = set()
            for s in all_sessions:
                if s.session_id in self.ws.deferred_sessions:
                    deferred_at = self.ws.deferred_sessions[s.session_id]
                    last_msg = s.last_user_message_at or ""
                    if last_msg and deferred_at and self._parse_ts(last_msg) > self._parse_ts(deferred_at):
                        undeferred.add(s.session_id)
            if undeferred:
                log.debug("load_detail: auto-undeferring %s", undeferred)
                for sid in undeferred:
                    del self.ws.deferred_sessions[sid]
                self.store.update(self.ws)
            hidden = set(self.ws.archived_sessions)
            # Hide the pending new session (from "c") until it has actual messages
            pending_sid = getattr(self.app, '_ws_pending_session', {}).get(self.ws.id)
            self._all_sessions = [
                s for s in all_sessions
                if s.session_id not in hidden
                and not (s.session_id == pending_sid and s.message_count == 0)
            ]
            self._all_archived = [s for s in all_sessions if s.session_id in hidden]
            self._deferred_set = set(self.ws.deferred_sessions)
            log.debug("load_detail: active=%d archived=%d deferred=%d",
                      len(self._all_sessions), len(self._all_archived), len(self._deferred_set))
        else:
            from actions import find_sessions_for_ws
            self._all_sessions = find_sessions_for_ws(self.ws, getattr(app, 'sessions', []))
            self._all_archived = []
            self._deferred_set = set(self.ws.deferred_sessions)

        # If search is active, just update backing data silently — don't
        # rebuild the results list mid-search (it resets scroll/highlight and
        # causes visual disruption).  The next keystroke will re-search with
        # the freshly updated _all_sessions/_all_archived.
        if self._search_text:
            return

        # Always use fresh sorted order so most-recent sessions are at top.
        # The notified/elevated/quiet grouping in _build_session_list handles
        # keeping important sessions visible without needing stable merge.
        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)

        # Don't rebuild OptionLists while peek is open — backing data is updated
        # but the visible list stays showing the conversation.
        if self._peek_mode:
            return

        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        if self._detail_sessions:
            olist.display = True
            no_sess.display = False
            old_sid = self._highlighted_session_id(olist)
            old_idx = olist.highlighted
            self._build_session_list()
            self._restore_highlight_by_sid(olist, self._detail_sessions, old_sid, old_idx)
        else:
            olist.display = False
            no_sess.display = True
            if self._sessions_loading():
                from rendering import THROBBER_FRAMES
                frame = THROBBER_FRAMES[self._loading_frame % len(THROBBER_FRAMES)]
                no_sess.update(f"[{C_CYAN}]{frame}[/{C_CYAN}] [{C_DIM}]Discovering sessions...[/{C_DIM}]")
            else:
                no_sess.update(f"[{C_DIM}]No sessions[/{C_DIM}]")

        arch_olist = self.query_one("#detail-archived", OptionList)
        no_arch = self.query_one("#detail-no-archived", Static)
        arch_pane = self.query_one("#detail-archived-pane")
        arch_pane.display = True
        if self._archived_sessions:
            arch_olist.display = True
            no_arch.display = False
            # Skip rebuild if archived set hasn't changed
            arch_fp = tuple(
                (s.session_id, s.is_live, s.last_message_role)
                for s in self._archived_sessions[:getattr(self, '_archived_show_count', self._ARCHIVED_PAGE_SIZE)]
            )
            if arch_fp != getattr(self, '_last_arch_fp', None):
                self._last_arch_fp = arch_fp
                old_sid = self._highlighted_session_id(arch_olist)
                old_arch_idx = arch_olist.highlighted
                self._build_archived_list()
                self._restore_highlight_by_sid(arch_olist, self._archived_sessions, old_sid, old_arch_idx)
        else:
            arch_olist.display = False
            no_arch.display = True
            if self._active_pane == "archived":
                self._active_pane = "sessions"
                self.query_one("#detail-sessions", OptionList).focus()

        self._update_pane_labels()

    @staticmethod
    def _highlighted_session_id(olist: OptionList) -> str | None:
        """Get the session_id of the currently highlighted option via its option ID."""
        if olist.highlighted is not None and olist.option_count > 0:
            try:
                oid = olist.get_option_at_index(olist.highlighted).id
                if oid:
                    return oid.removeprefix("a:")
            except Exception:
                pass
        return None

    @staticmethod
    def _restore_highlight_by_sid(olist: OptionList, sessions: list, sid: str | None, old_idx: int | None = None):
        """Restore highlight to the session with the given ID, or clamp to old position."""
        if not olist.option_count:
            return
        if sid:
            # Search by option ID since option list may have separators
            for i in range(olist.option_count):
                try:
                    oid = olist.get_option_at_index(i).id
                    if oid and oid.removeprefix("a:") == sid:
                        olist.highlighted = i
                        return
                except Exception:
                    continue
        # Session was removed — keep cursor at same position, clamped.
        if old_idx is not None:
            olist.highlighted = min(old_idx, olist.option_count - 1)
        else:
            olist.highlighted = 0

    @staticmethod
    def _stable_merge(existing: list[ClaudeSession], fresh: list[ClaudeSession]) -> list[ClaudeSession]:
        """Merge fresh session data into existing order.

        Preserves the position of sessions already in the list (updating their
        data), prepends genuinely new sessions at the top, and drops sessions
        no longer present in fresh.  This prevents the list from re-sorting
        every time active sessions receive new responses.
        """
        fresh_by_id = {s.session_id: s for s in fresh}
        fresh_ids = set(fresh_by_id)

        # Keep existing order for sessions still present, with updated objects
        merged = []
        seen = set()
        for s in existing:
            if s.session_id in fresh_ids:
                merged.append(fresh_by_id[s.session_id])
                seen.add(s.session_id)

        # Prepend any new sessions (not previously in the list) at the top
        new_sessions = [s for s in fresh if s.session_id not in seen]
        return new_sessions + merged

    def _build_session_list(self):
        olist = self.query_one("#detail-sessions", OptionList)
        animating = []

        # Split into notified, elevated (unseen your-turn without notification), and quiet
        notified = []
        elevated = []
        quiet = []
        for s in self._detail_sessions:
            if s.session_id in self._session_notifications:
                notified.append(s)
            else:
                act = session_activity(s, self._last_seen_cache)
                seen = _is_session_seen(s, self._last_seen_cache)
                if not seen and act in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
                    elevated.append(s)
                else:
                    quiet.append(s)

        # Sort each group by recency (newest first)
        notified.sort(key=lambda s: self._session_notifications[s.session_id].dt, reverse=True)
        elevated.sort(key=lambda s: s.last_activity or "", reverse=True)
        quiet.sort(key=lambda s: s.last_activity or "", reverse=True)

        # Split quiet into other, thinking, and deferred sections
        # Order: other (top) → thinking → deferred (bottom)
        quiet_active = [s for s in quiet if s.session_id not in self._deferred_set]
        quiet_deferred = [s for s in quiet if s.session_id in self._deferred_set]
        quiet_thinking = [s for s in quiet_active if session_activity(s, self._last_seen_cache) == ThreadActivity.THINKING]
        quiet_other = [s for s in quiet_active if s not in quiet_thinking]

        # Build all options before touching widget tree
        all_elevated = notified + elevated
        self._notified_count = len(all_elevated)
        lw = self._session_line_width("#detail-sessions")
        options = []
        idx = 0
        for s in notified:
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((idx, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            notif = self._session_notifications[s.session_id]
            prompt = _render_notified_session_option(
                s, act, notif, self._throbber_frame,
                ws_repo_path=self.ws.repo_path, seen=seen,
                line_width=lw,
            )
            options.append(Option(prompt, id=s.session_id))
            idx += 1

        for s in elevated:
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((idx, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            prompt = _render_notified_session_option(
                s, act, None, self._throbber_frame,
                ws_repo_path=self.ws.repo_path, seen=seen,
                line_width=lw,
            )
            options.append(Option(prompt, id=s.session_id))
            idx += 1

        if all_elevated and (quiet_active or quiet_deferred):
            options.append(Option(QUIET_SEPARATOR_LABEL(lw), id="__separator__", disabled=True))
            idx += 1

        earlier_sep_inserted = False
        for qi, s in enumerate(quiet_other):
            act = session_activity(s, self._last_seen_cache)
            if act == ThreadActivity.AWAITING_INPUT:
                animating.append((idx, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            if not earlier_sep_inserted and qi > 0 and not _is_today(s.last_activity or s.started_at or ""):
                pad = max(1, (lw - 10) // 2)
                earlier_label = f"[{C_FAINT}]{'─' * pad} earlier {'─' * pad}[/{C_FAINT}]"
                options.append(Option(earlier_label, id="__sep_earlier__", disabled=True))
                idx += 1
                earlier_sep_inserted = True
            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
            options.append(Option(prompt, id=s.session_id))
            idx += 1

        if quiet_thinking:
            options.append(Option(THINKING_SEPARATOR_LABEL(lw), id="__sep_thinking__", disabled=True))
            idx += 1
            for s in quiet_thinking:
                act = session_activity(s, self._last_seen_cache)
                animating.append((idx, act))
                seen = _is_session_seen(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
                options.append(Option(prompt, id=s.session_id))
                idx += 1

        if quiet_deferred:
            options.append(Option(DEFERRED_SEPARATOR_LABEL(lw), id="__sep_deferred__", disabled=True))
            idx += 1
            for s in quiet_deferred:
                act = session_activity(s, self._last_seen_cache)
                seen = _is_session_seen(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw, deferred=True)
                options.append(Option(prompt, id=s.session_id))
                idx += 1

        olist.clear_options()
        olist.add_options(options)
        self._animating_sessions = animating
        log.debug("build_session_list: %d notified + %d elevated + %d quiet (%d deferred), option_count=%d",
                  len(notified), len(elevated), len(quiet), len(quiet_deferred), olist.option_count)

    _ARCHIVED_PAGE_SIZE = 30

    def _build_archived_list(self):
        olist = self.query_one("#detail-archived", OptionList)
        animating = []
        alw = self._session_line_width("#detail-archived")
        options = []
        limit = getattr(self, '_archived_show_count', self._ARCHIVED_PAGE_SIZE)
        display = self._archived_sessions[:limit]
        for i, s in enumerate(display):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((i, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=alw, archived=True)
            options.append(Option(prompt, id=f"a:{s.session_id}"))
        remaining = len(self._archived_sessions) - limit
        if remaining > 0:
            options.append(Option(
                f"[{C_DIM}]  ↓ {remaining} more archived sessions — press Enter to load[/{C_DIM}]",
                id="__load_more_archived__",
            ))
        olist.clear_options()
        olist.add_options(options)
        self._animating_archived = animating

    # ── Feed (notification) panel ──

    def _render_feed_label(self) -> str:
        n = len([n for n in self._feed_notifications if not n.dismissed])
        if n:
            return f"[bold {C_BLUE}]Feed[/bold {C_BLUE}] [{C_DIM}]({n})[/{C_DIM}]"
        return f"[bold {C_BLUE}]Feed[/bold {C_BLUE}]"

    def _load_feed(self):
        app = self.app
        if hasattr(app, 'state'):
            self._feed_notifications = app.state.notifications_for_ws(self.ws)
        else:
            self._feed_notifications = []

        # Build session_id -> latest (non-dismissed) notification map.
        # Match by session_id when available, otherwise by cwd proximity.
        notif_map: dict[str, Notification] = {}
        unmatched: list[Notification] = []
        for n in self._feed_notifications:
            if n.dismissed:
                continue
            if n.session_id:
                existing = notif_map.get(n.session_id)
                if not existing or n.dt > existing.dt:
                    notif_map[n.session_id] = n
            else:
                unmatched.append(n)

        # Fallback: match unmatched notifications to sessions by cwd + timestamp
        if unmatched and self._detail_sessions:
            for n in unmatched:
                if not n.cwd:
                    continue
                cwd_norm = n.cwd.rstrip("/")
                best_sid = None
                best_gap = None
                for s in self._detail_sessions:
                    if not s.project_path or s.project_path.rstrip("/") != cwd_norm:
                        continue
                    if s.last_activity:
                        s_dt = _parse_iso(s.last_activity)
                        if s_dt:
                            gap = abs((n.dt - s_dt).total_seconds())
                            if best_gap is None or gap < best_gap:
                                best_gap = gap
                                best_sid = s.session_id
                    elif best_sid is None:
                        best_sid = s.session_id
                if best_sid:
                    existing = notif_map.get(best_sid)
                    if not existing or n.dt > existing.dt:
                        notif_map[best_sid] = n

        self._session_notifications = notif_map

        # Feed pane is now hidden — notifications are inline with sessions
        try:
            olist = self.query_one("#detail-feed", OptionList)
            olist.display = False
            self.query_one("#detail-no-feed", Static).display = False
        except Exception:
            pass

    def _build_feed_list(self):
        olist = self.query_one("#detail-feed", OptionList)
        options = [Option(_render_notification_option(notif), id=f"notif:{notif.id}")
                   for notif in self._feed_notifications]
        olist.clear_options()
        olist.add_options(options)

    # _poll_feed merged into _apply_liveness_result (single timer)

    def action_dismiss_notification(self):
        """Dismiss the notification on the currently highlighted session."""
        if self._active_pane != "sessions":
            return
        olist = self.query_one("#detail-sessions", OptionList)
        idx = olist.highlighted
        if idx is None:
            return
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid == "__separator__":
            return
        sid = oid.removeprefix("a:")
        notif = self._session_notifications.get(sid)
        if not notif:
            return
        dismiss_notification(notif.id)
        notif.dismissed = True
        del self._session_notifications[sid]
        self._load_detail_sessions()

    def action_dismiss_all_notifications(self):
        """Dismiss all notifications for this workstream."""
        dirs = set()
        if hasattr(self.app, 'state'):
            dirs = self.app.state._ws_dirs(self.ws)
        if dirs:
            dismiss_all_for_dirs(self._feed_notifications, dirs)
        for n in self._feed_notifications:
            n.dismissed = True
        self._session_notifications.clear()
        self._load_detail_sessions()

    # ── Panel navigation (Ctrl+j/k) ──

    def _panel_ids(self) -> list[str]:
        """Focusable panel widget IDs, skipping empty ones."""
        panels = ["detail-sessions"]
        if self._archived_sessions:
            panels.append("detail-archived")
        if self._sidebar_enabled:
            panels.extend(["detail-tig-status", "detail-tig-log"])
        else:
            panels.append("detail-scroll")
        # Feed pane no longer in cycle — notifications are inline in sessions
        return panels

    _PANEL_ID_TO_NAME = {
        "detail-sessions": "sessions",
        "detail-archived": "archived",
        "detail-scroll": "body",
        "detail-feed": "feed",
        "detail-tig-status": "tig-status",
        "detail-tig-log": "tig-log",
    }
    _PANEL_NAME_TO_ID = {v: k for k, v in _PANEL_ID_TO_NAME.items()}

    def action_next_panel(self):
        panels = self._panel_ids()
        current_id = self._PANEL_NAME_TO_ID.get(self._active_pane, "detail-sessions")
        idx = panels.index(current_id) if current_id in panels else 0
        next_id = panels[(idx + 1) % len(panels)]
        self._active_pane = self._PANEL_ID_TO_NAME.get(next_id, "sessions")
        try:
            self.query_one(f"#{next_id}").focus()
        except Exception:
            pass
        self._update_pane_labels()

    def action_prev_panel(self):
        panels = self._panel_ids()
        current_id = self._PANEL_NAME_TO_ID.get(self._active_pane, "detail-sessions")
        idx = panels.index(current_id) if current_id in panels else 0
        prev_id = panels[(idx - 1) % len(panels)]
        self._active_pane = self._PANEL_ID_TO_NAME.get(prev_id, "sessions")
        try:
            self.query_one(f"#{prev_id}").focus()
        except Exception:
            pass
        self._update_pane_labels()

    def _find_session_by_id(self, sid: str) -> ClaudeSession | None:
        for s in self._detail_sessions:
            if s.session_id == sid:
                return s
        for s in self._archived_sessions:
            if s.session_id == sid:
                return s
        return None

    def action_select_session(self):
        """Vim-style l to select the highlighted session (same as enter)."""
        if self._peek_mode:
            return
        olist = self._focused_olist()
        if olist.highlighted is not None and olist.option_count > 0:
            olist.action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if self._peek_mode:
            return
        oid = event.option_id
        log.debug("option_selected: option_id=%r option_index=%r widget_id=%s",
                  oid, event.option_index, event.option_list.id if hasattr(event, 'option_list') else "?")

        # Feed notification selected — jump to the matching session
        if oid and oid.startswith("notif:"):
            notif_id = oid.removeprefix("notif:")
            notif = next((n for n in self._feed_notifications if n.id == notif_id), None)
            if notif and notif.session_id:
                session = self._find_session_by_id(notif.session_id)
                if session:
                    mark_thread_seen(session.session_id)
                    self._last_seen_cache = load_last_seen()
                    self.app.launch_claude_session(
                        self.ws, session_id=session.session_id,
                        cwd=session.project_path,
                    )
                    return
            # No session to jump to — dismiss instead
            if notif and not notif.dismissed:
                dismiss_notification(notif.id)
                notif.dismissed = True
                self._load_feed()
            return

        if oid == "__load_more_archived__":
            self._archived_show_count = getattr(self, '_archived_show_count', self._ARCHIVED_PAGE_SIZE) + self._ARCHIVED_PAGE_SIZE
            self._build_archived_list()
            return
        if oid is None or oid == "__separator__":
            if oid is None:
                log.warning("option_selected: option_id is None!")
            return
        sid = oid.removeprefix("a:")  # archived IDs are "a:<session_id>"
        session = self._find_session_by_id(sid)
        log.debug("option_selected: sid=%s found=%s", sid, session is not None)
        if session:
            mark_thread_seen(session.session_id)
            self._last_seen_cache = load_last_seen()
            # Auto-dismiss all notifications that could match this session.
            # Dismiss by session_id match AND by cwd match, so the cwd fallback
            # doesn't resurface older notifications on the next poll.
            session_cwd = (session.project_path or "").rstrip("/")
            for n in self._feed_notifications:
                if n.dismissed:
                    continue
                if (n.session_id and n.session_id == sid) or \
                   (not n.session_id and n.cwd and n.cwd.rstrip("/") == session_cwd):
                    dismiss_notification(n.id)
                    n.dismissed = True
            self._session_notifications.pop(sid, None)
            self.app.launch_claude_session(
                self.ws, session_id=session.session_id,
                cwd=session.project_path,
            )
        else:
            log.warning("option_selected: session not found for sid=%s, detail_sids=%s, archived_sids=%s",
                        sid, [s.session_id for s in self._detail_sessions],
                        [s.session_id for s in self._archived_sessions])

    def _render_title(self) -> str:
        return f"[bold {C_PURPLE}]{_rich_escape(self.ws.name)}[/bold {C_PURPLE}]"

    def _render_meta(self) -> str:
        parts = [_category_markup(self.ws.category)]

        # Enrichment badges (ticket, MR, solve)
        ticket_key = getattr(self.ws, "ticket_key", "")
        if ticket_key:
            ticket_status = getattr(self.ws, "ticket_status", "")
            if ticket_status:
                ts_lower = ticket_status.lower()
                if "progress" in ts_lower or "review" in ts_lower:
                    ts_color = C_CYAN
                elif "done" in ts_lower or "closed" in ts_lower or "resolved" in ts_lower:
                    ts_color = C_GREEN
                else:
                    ts_color = C_DIM
                parts.append(f"[bold]{_rich_escape(ticket_key)}[/bold] [{ts_color}]{_rich_escape(ticket_status)}[/{ts_color}]")
            else:
                parts.append(f"[bold]{_rich_escape(ticket_key)}[/bold]")
        mr_url = getattr(self.ws, "mr_url", "")
        if mr_url:
            parts.append(f"[{C_PURPLE}]MR[/{C_PURPLE}]")
        solve_status = getattr(self.ws, "ticket_solve_status", "")
        if solve_status:
            if solve_status.lower() in ("running", "active"):
                parts.append(f"[{C_YELLOW}]solving[/{C_YELLOW}]")
            elif solve_status.lower() in ("done", "complete"):
                parts.append(f"[{C_GREEN}]solved[/{C_GREEN}]")
            else:
                parts.append(f"[{C_DIM}]solve:{_rich_escape(solve_status)}[/{C_DIM}]")

        if self._detail_sessions:
            n = len(self._detail_sessions)
            total_tok = sum(s.total_input_tokens + s.total_output_tokens for s in self._detail_sessions)
            total_msgs = sum(s.message_count for s in self._detail_sessions)
            _tk = f"{total_tok / 1_000_000:.1f}M" if total_tok > 1_000_000 else f"{total_tok / 1_000:.0f}k" if total_tok > 1_000 else str(total_tok)
            parts.append(
                f"[{C_DIM}]{n} sessions \u00b7 {total_msgs} msgs \u00b7 "
                f"{_token_color_markup(_tk, total_tok)}[/{C_DIM}]"
            )
        return "  ".join(parts)

    def _render_body(self) -> str:
        lines = []
        ext_links = [lnk for lnk in self.ws.links
                     if lnk.kind not in ("worktree", "file", "claude-session")]
        if ext_links:
            lines.append(f"[bold {C_BLUE}]Links[/bold {C_BLUE}]")
            for lnk in ext_links:
                icon = _link_icon(lnk.kind)
                lines.append(f"  {icon} {_rich_escape(lnk.value)}")
            lines.append("")
        # Todo summary
        from state import AppState
        active_todos = AppState.active_todos(self.ws)
        if active_todos:
            undone = [t for t in active_todos if not t.done]
            done = [t for t in active_todos if t.done]
            lines.append(f"[bold {C_BLUE}]Todos[/bold {C_BLUE}] [{C_DIM}]({len(undone)} pending, {len(done)} done)[/{C_DIM}]")
            for t in active_todos[:6]:
                icon = TODO_DONE_ICON if t.done else TODO_UNDONE_ICON
                color = C_GREEN if t.done else ""
                if color:
                    lines.append(f"  [{color}]{icon} {t.text}[/{color}]")
                else:
                    lines.append(f"  {icon} {t.text}")
            if len(active_todos) > 6:
                lines.append(f"  [{C_DIM}]... +{len(active_todos) - 6} more[/{C_DIM}]")
            lines.append("")
        lines.append(
            f"[{C_DIM}]Created {_relative_time(self.ws.created_at)} \u00b7 "
            f"Updated {_relative_time(self.ws.updated_at)}[/{C_DIM}]"
        )
        return "\n".join(lines)

    def _render_help(self) -> str:
        pairs = [
            ("^L", "resume"), ("c", "spawn"),
            ("n", "+todo"), ("e", "todos"), ("L", "+link"),
            ("o", "open"), ("x", "archive ws"),
            ("space", "archive/restore"),
            ("z", "defer/undefer"),
            ("p", "peek"), ("y", "yank cmd"),
            ("^j/^k", "panels"), ("/", "search"),
            ("^H", "back"),
        ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    # ── Content search ──

    def _search_is_active(self) -> bool:
        """Return True if the search input is visible (search mode)."""
        try:
            return self.query_one("#detail-search-input", _SearchInput).has_class("visible")
        except Exception:
            return False

    async def action_dismiss(self) -> None:
        """Override dismiss: close peek, then search, then screen."""
        if self._peek_mode:
            self._close_peek()
        elif self._search_is_active():
            self._cancel_search()
        else:
            await super().action_dismiss(None)

    def action_go_back(self):
        """Ctrl+H/Backspace: close peek, then search, then dismiss."""
        if self._peek_mode:
            self._close_peek()
        elif self._search_is_active():
            self._cancel_search()
        else:
            self.dismiss(None)

    def action_go_forward(self):
        """Ctrl+L: resume the highlighted session."""
        self.action_resume()

    def _update_help_bar(self):
        help_bar = self.query_one("#detail-help", Static)
        search_input = self.query_one("#detail-search-input", _SearchInput)
        if search_input.has_class("visible") and not search_input.has_focus:
            # Viewing search results — show navigation hints
            pairs = [
                ("j/k", "navigate"), ("^L", "resume"),
                ("/", "refine"), ("^H", "clear/back"),
            ]
            help_bar.update("  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs))
        else:
            help_bar.update(self._render_help())

    @property
    def _search_text(self) -> str:
        """Current search query from the Input widget."""
        try:
            return self.query_one("#detail-search-input", _SearchInput).value
        except Exception:
            return ""

    def action_search(self):
        """Show the search input and focus it."""
        search_input = self.query_one("#detail-search-input", _SearchInput)
        search_input.add_class("visible")
        search_input.focus()
        # Hide archived pane so search results get full width
        self.query_one("#detail-archived-pane").display = False
        # Start warming the content cache in background
        if not self._content_ready:
            self._warm_content_cache()

    def _cancel_search(self):
        """Hide search input and restore normal session lists."""
        search_input = self.query_one("#detail-search-input", _SearchInput)
        search_input.value = ""
        search_input.remove_class("visible")
        self._content_search_active = False
        self._content_results = []
        # Restore normal session lists and archived pane
        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)
        if self._archived_sessions:
            self.query_one("#detail-archived-pane").display = True
        self._rebuild_session_lists()
        self._update_help_bar()
        # Return focus to the sessions list
        self.query_one("#detail-sessions", OptionList).focus()

    def _focus_search_results(self):
        """Shift focus from search input to results list."""
        olist = self.query_one("#detail-sessions", OptionList)
        if olist.option_count > 0:
            olist.highlighted = 0
            olist.focus()
            self._active_pane = "sessions"
            self._update_pane_labels()
            self._update_help_bar()

    @on(Input.Changed, "#detail-search-input")
    def _on_search_input_changed(self, event: Input.Changed) -> None:
        """Live filter as user types in the search input."""
        query = event.value
        if not query:
            self._content_search_active = False
            self._content_results = []
            self._detail_sessions = list(self._all_sessions)
            self._archived_sessions = list(self._all_archived)
            if self._archived_sessions:
                self.query_one("#detail-archived-pane").display = True
            self._rebuild_session_lists()
            return
        if self._content_ready:
            self._run_content_search_sync()
        else:
            self._apply_title_filter()

    @on(Input.Submitted, "#detail-search-input")
    def _on_search_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in search input shifts focus to results."""
        self._focus_search_results()

    def on_key(self, event) -> None:
        """Handle backspace navigation and special keys in search input."""
        if event.key in ("backspace", "ctrl+h"):
            event.stop()
            event.prevent_default()
            self.action_go_back()
            return
        search_input = self.query_one("#detail-search-input", _SearchInput)
        if search_input.has_focus:
            if event.key == "down":
                event.stop()
                event.prevent_default()
                self._focus_search_results()
            return
        # Ctrl+Space = archive current peeked session and close peek
        if event.key == "ctrl+space" and self._peek_mode:
            event.stop()
            event.prevent_default()
            self._archive_peek_session_and_close()
            return
        # Space = archive/unarchive session
        if event.key == "space" and self._active_pane in ("sessions", "archived"):
            event.stop()
            event.prevent_default()
            self.action_archive_session()

    # ── Session peek (replaces OptionList content in-place) ──

    def _open_peek(self):
        """Replace session options with conversation messages in the same OptionList."""
        if self._peek_mode:
            self._close_peek()
            return
        olist = self.query_one("#detail-sessions", OptionList)
        idx = olist.highlighted
        if idx is None:
            return
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid == "__separator__":
            return
        sid = oid.removeprefix("a:")
        session = self._find_session_by_id(sid)
        if not session:
            return
        from sessions import extract_session_content
        if session.session_id in self._content_cache:
            messages = self._content_cache[session.session_id]
        else:
            messages = extract_session_content(session.jsonl_path) if session.jsonl_path else []
            self._content_cache[session.session_id] = messages
        if not messages:
            self.app.notify("No conversation content to peek", timeout=2)
            return
        title_text = _session_title(session)
        header = (
            f"[bold {C_BLUE}]{_rich_escape(title_text)}[/bold {C_BLUE}]  "
            f"[{C_DIM}]{session.age} · {_short_model(session.model)} · "
            f"{session.message_count} msgs · {session.tokens_display}[/{C_DIM}]\n"
            f"[{C_DIM}]p[/{C_DIM}] close  [{C_DIM}]j/k[/{C_DIM}] scroll"
        )
        options = [Option(header, id="peek-header")]
        for i, msg in enumerate(messages):
            if msg.role == "user":
                role_fmt = f"[bold {C_CYAN}]you[/bold {C_CYAN}]"
            else:
                role_fmt = f"[bold {C_PURPLE}]claude[/bold {C_PURPLE}]"
            text = msg.text
            if len(text) > 2000:
                text = text[:2000] + "\n…(truncated)"
            prompt = f"{role_fmt}\n[{C_LIGHT}]{_rich_escape(text)}[/{C_LIGHT}]"
            options.append(Option(prompt, id=f"peek-msg-{i}"))
        olist.clear_options()
        olist.add_options(options)
        if olist.option_count > 0:
            olist.highlighted = olist.option_count - 1
        self._peek_mode = True
        self._peek_session_id = sid
        sess_label = self.query_one("#detail-sessions-label", Static)
        sess_label.update(
            f"[bold {C_BLUE}]Conversation[/bold {C_BLUE}] "
            f"[{C_DIM}]({len(messages)} messages)[/{C_DIM}]"
        )

    def _close_peek(self):
        """Restore the normal session list."""
        self._peek_mode = False
        self._peek_session_id = None
        self._build_session_list()
        olist = self.query_one("#detail-sessions", OptionList)
        if olist.option_count > 0:
            olist.highlighted = 0
        self._update_pane_labels()

    def _archive_peek_session_and_close(self):
        """Ctrl+Space: archive the peeked session and return to session list."""
        sid = self._peek_session_id
        if sid and sid not in self.ws.archived_sessions:
            self.ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
            self.store.update(self.ws)
        self._close_peek()
        self._refresh()
        self.app.notify("Session archived", timeout=1)

    @work(thread=True, exclusive=True, group="content_cache")
    def _warm_content_cache(self):
        """Background: extract conversation content from all session JSONLs."""
        from sessions import extract_session_content
        all_sessions = self._all_sessions + self._all_archived
        for s in all_sessions:
            if s.session_id not in self._content_cache and s.jsonl_path:
                self._content_cache[s.session_id] = extract_session_content(s.jsonl_path)
        self._content_ready = True
        # If user already typed a query while we were loading, run search now
        self.app.call_from_thread(self._on_cache_ready)

    def _on_cache_ready(self):
        """Called on main thread when content cache is warm."""
        if self._search_text:
            self._run_content_search_sync()

    def _run_content_search_sync(self):
        """Run content search synchronously (cache must be warm)."""
        all_sessions = self._all_sessions + self._all_archived
        results = content_search(self._search_text, all_sessions, self._content_cache)
        self._content_results = results
        self._content_search_active = True
        self._show_content_results()

    def _show_content_results(self):
        """Update the sessions OptionList with content search results."""
        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)

        if self._content_results:
            olist.display = True
            no_sess.display = False
            options = [Option(_render_content_search_result(r, ws_repo_path=self.ws.repo_path),
                              id=r.session.session_id)
                       for r in self._content_results]
            olist.clear_options()
            olist.add_options(options)
            if olist.option_count > 0:
                olist.highlighted = 0
            # Update the session list to match results for selection handling
            self._detail_sessions = [r.session for r in self._content_results]
        else:
            olist.display = False
            no_sess.update(f"[{C_DIM}]No matches[/{C_DIM}]")
            no_sess.display = True
            self._detail_sessions = []

        # Hide archived pane during search (results span full width)
        self.query_one("#detail-archived-pane").display = False
        arch_olist = self.query_one("#detail-archived", OptionList)
        arch_olist.clear_options()
        self._archived_sessions = []

        # Update labels
        sess_label = self.query_one("#detail-sessions-label", Static)
        n = len(self._content_results)
        sess_label.update(
            f"[bold {C_BLUE}]Search[/bold {C_BLUE}] "
            f"[{C_DIM}]({n} result{'s' if n != 1 else ''})[/{C_DIM}]"
        )
        self._update_pane_labels()

    def _apply_title_filter(self):
        """Fallback: fuzzy filter on session titles while content cache loads."""
        scored = []
        for s in self._all_sessions + self._all_archived:
            searchable = " ".join(filter(None, [s.display_name, s.last_message_text, s.model]))
            sc = fuzzy_match(self._search_text, searchable)
            if sc is not None:
                scored.append((s, sc))
        scored.sort(key=lambda t: t[1], reverse=True)
        self._detail_sessions = [s for s, _ in scored]
        self._archived_sessions = []
        self._content_search_active = False
        self._rebuild_session_lists()

    def _rebuild_session_lists(self):
        """Rebuild both session OptionLists from current state."""
        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        if self._detail_sessions:
            olist.display = True
            no_sess.display = False
            self._build_session_list()
            if olist.option_count > 0:
                olist.highlighted = 0
        else:
            olist.display = False
            if self._search_text:
                no_sess.update(f"[{C_DIM}]No matches[/{C_DIM}]")
            else:
                no_sess.update(f"[{C_DIM}]No sessions[/{C_DIM}]")
            no_sess.display = True

        arch_olist = self.query_one("#detail-archived", OptionList)
        no_arch = self.query_one("#detail-no-archived", Static)
        if self._archived_sessions:
            arch_olist.display = True
            no_arch.display = False
            self._build_archived_list()
            if arch_olist.option_count > 0:
                arch_olist.highlighted = 0
        else:
            arch_olist.display = False
            no_arch.display = True

        # Restore sessions label when not in content search
        if not self._content_search_active:
            n = len(self._detail_sessions)
            sess_label = self.query_one("#detail-sessions-label", Static)
            if self._active_pane == "sessions":
                sess_label.update(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}] [{C_DIM}]({n})[/{C_DIM}]")
            else:
                sess_label.update(f"[{C_DIM}]Sessions ({n})[/{C_DIM}]")

        self._update_pane_labels()
        self._update_animating_cache()

    def _refresh(self):
        with self.app.batch_update():
            self.query_one("#detail-title", Static).update(self._render_title() + "  " + self._render_meta())
            try:
                self.query_one("#detail-body", Static).update(self._render_body())
            except Exception:
                pass  # body not present when tig sidebar is active
            self._load_detail_sessions()
            self._load_feed()

    def action_quick_note(self):
        def on_note(text: str | None):
            if not text or not text.strip():
                return
            self.app.state.add_todo(self.ws.id, text.strip())
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()
            self.app.notify("Todo added", timeout=1)
        self.app.push_screen(QuickNoteScreen(self.ws), callback=on_note)

    def action_open_todos(self):
        # Reload from disk so CLI-created todos (e.g. crystallized) appear
        self.store.load()
        self.ws = self.store.get(self.ws.id) or self.ws
        def on_close(_):
            self.store.load()
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()
        self.app.push_screen(TodoScreen(self.ws, self.store), callback=on_close)

    def action_open_links(self):
        if self.ws.links:
            self.app.push_screen(LinksScreen(self.ws, self.store))
        else:
            self.app.notify("No links to open", timeout=2)

    def action_file_picker(self):
        """Open fzedit file picker in the workstream's working directory."""
        import shutil

        cwd = ws_working_dir(self.ws)
        if cwd == os.getcwd() and not self.ws.repo_path and not ws_directories(self.ws):
            self.app.notify("No directory linked to this workstream", timeout=2)
            return
        if not shutil.which("fzedit"):
            self.app.notify("fzedit not found on PATH", timeout=2)
            return
        with self.app.suspend():
            open_file_picker(cwd)

    def action_peek_session(self):
        """p = peek into session conversation."""
        if self._active_pane in ("sessions", "archived"):
            self._open_peek()

    def action_archive(self):
        self.ws.archived = True
        self.store.update(self.ws)
        self.app.notify(f"Archived: {self.ws.name}", timeout=2)
        self.dismiss()

    def action_archive_session(self):
        olist = self._focused_olist()
        idx = olist.highlighted
        log.debug("archive_session: pane=%s idx=%s option_count=%d detail=%d archived=%d",
                  self._active_pane, idx, olist.option_count,
                  len(self._detail_sessions), len(self._archived_sessions))
        if idx is None:
            return
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid == "__separator__":
            return
        if self._active_pane == "archived":
            sid = oid.removeprefix("a:")
            log.debug("archive_session: unarchiving sid=%s", sid)
            self.ws.archived_sessions.pop(sid, None)
            self.store.update(self.ws)
        else:
            sid = oid
            log.debug("archive_session: archiving sid=%s", sid)
            if sid not in self.ws.archived_sessions:
                self.ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
                self.store.update(self.ws)
        self._refresh()

    def action_defer_session(self):
        """z = defer/un-defer the highlighted session."""
        if self._active_pane != "sessions":
            return
        olist = self._focused_olist()
        idx = olist.highlighted
        if idx is None:
            return
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid.startswith("__"):
            return
        sid = oid
        if sid in self.ws.deferred_sessions:
            del self.ws.deferred_sessions[sid]
            self._deferred_set.discard(sid)
            self.store.update(self.ws)
            self.app.notify("Undeferred", timeout=1)
        else:
            self.ws.deferred_sessions[sid] = datetime.now(timezone.utc).isoformat()
            self._deferred_set.add(sid)
            self.store.update(self.ws)
            self.app.notify("Deferred", timeout=1)
        self._refresh()

    def action_spawn(self):
        self.app.launch_claude_session(self.ws)

    def action_resume(self):
        """Resume the currently highlighted session (same as Enter)."""
        if self._peek_mode:
            return
        olist = self._focused_olist()
        idx = olist.highlighted
        if idx is None or self._active_pane not in ("sessions", "archived"):
            return
        # Use option ID to find the session — the OptionList may contain
        # separators that shift indices relative to the sessions list.
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid == "__separator__":
            return
        sid = oid.removeprefix("a:")
        session = self._find_session_by_id(sid)
        if session:
            mark_thread_seen(session.session_id)
            self._last_seen_cache = load_last_seen()
            self.app.launch_claude_session(
                self.ws, session_id=session.session_id,
                cwd=session.project_path,
            )

    def action_yank_resume_cmd(self):
        """Copy 'claude --resume <session-id>' for the highlighted session."""
        olist = self._focused_olist()
        idx = olist.highlighted
        if idx is None or self._active_pane not in ("sessions", "archived"):
            return
        try:
            oid = olist.get_option_at_index(idx).id
        except Exception:
            return
        if not oid or oid == "__separator__":
            return
        sid = oid.removeprefix("a:")
        session = self._find_session_by_id(sid)
        if not session:
            return
        cmd = f"claude --resume {session.session_id}"
        copied = _copy_to_clipboard(cmd)
        # Show command in the footer help bar
        help_bar = self.query_one("#detail-help", Static)
        prefix = "Copied" if copied else "Resume"
        help_bar.update(f"[{C_GREEN}]{prefix}:[/{C_GREEN}] [{C_LIGHT}]{cmd}[/{C_LIGHT}]")
        self.set_timer(5, lambda: help_bar.update(self._render_help()))

    def action_add_link(self):
        def on_link(link: Link | None):
            if link:
                self.ws.links.append(link)
                self.ws.touch()
                self.store.update(self.ws)
                self._refresh()
                self.app.notify(f"Added {link.kind} link", timeout=2)
        self.app.push_screen(AddLinkScreen(self.ws.name), callback=on_link)

    def action_command_palette(self):
        """Delegate to app's command palette."""
        self.app.action_command_palette()

    def action_help(self):
        """Delegate to app's help screen."""
        self.app.action_help()



# ─── Brain Dump Screen ──────────────────────────────────────────────

class BrainDumpScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit", priority=True),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back", priority=True),
    ]

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    BrainDumpScreen {{ align: center middle; }}
    #brain-container {{
        width: 80; height: auto; max-height: 85%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #brain-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #brain-desc {{ color: {C_DIM}; padding-bottom: 1; }}
    #brain-editor {{ height: 12; margin: 0 0 1 0; }}
    #brain-hint {{ text-align: center; color: {C_DIM}; }}
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="brain-container"):
            yield Label("Brain Dump", id="brain-title")
            yield Static(
                f"[{C_DIM}]Type your stream of consciousness. Commas, newlines, "
                f"'also'/'and then' split into tasks.[/{C_DIM}]",
                id="brain-desc",
            )
            yield TextArea("", id="brain-editor")
            yield Static(f"[{C_DIM}]Ctrl+S[/{C_DIM}] submit  [{C_DIM}]^H[/{C_DIM}] back", id="brain-hint")

    def on_mount(self):
        self.query_one("#brain-editor", TextArea).focus()

    def action_submit(self):
        text = self.query_one("#brain-editor", TextArea).text.strip()
        if not text:
            self.app.notify("Nothing to parse", severity="warning", timeout=2)
            return
        self.dismiss(text)

    def action_cancel(self):
        self.dismiss(None)


# ─── Brain Preview Screen ───────────────────────────────────────────

class BrainPreviewScreen(ModalScreen[str]):
    """Preview parsed brain dump tasks.

    Dismisses with:
      - "add" to add all as workstreams
      - "launch" to add all and immediately launch Claude sessions
      - "" / None to cancel
    """

    BINDINGS = [
        Binding("enter,y", "confirm", "Add all"),
        Binding("l", "launch", "Add & Launch"),
        Binding("escape,n", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
    ]

    def action_go_back(self):
        self.dismiss("")

    DEFAULT_CSS = f"""
    BrainPreviewScreen {{ align: center middle; }}
    #brain-preview-container {{
        width: 80; height: auto; max-height: 85%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #brain-preview-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #brain-preview-body {{ padding: 0 1; max-height: 30; }}
    #brain-preview-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, tasks: list):
        super().__init__()
        self.tasks = tasks

    def compose(self) -> ComposeResult:
        with Vertical(id="brain-preview-container"):
            yield Static(f"[bold {C_PURPLE}]Parsed {len(self.tasks)} tasks[/bold {C_PURPLE}]", id="brain-preview-title")
            yield Rule()
            body_lines = []
            for i, task in enumerate(self.tasks, 1):
                body_lines.append(f"  [bold]{i}.[/bold] {task.name}")
                body_lines.append(f"     {_category_markup(task.category)}")
                if task.raw_text != task.name:
                    raw = task.raw_text[:80]
                    body_lines.append(f"     [{C_DIM}]{raw}[/{C_DIM}]")
                body_lines.append("")
            yield Static("\n".join(body_lines), id="brain-preview-body")
            yield Rule()
            yield Static(
                f"[{C_DIM}]Enter/y[/{C_DIM}] add  "
                f"[{C_DIM}]l[/{C_DIM}] add & launch  "
                f"[{C_DIM}]Esc[/{C_DIM}] cancel  "
                f"[{C_DIM}]^H[/{C_DIM}] back",
                id="brain-preview-hint",
            )

    def action_confirm(self):
        self.dismiss("add")

    def action_launch(self):
        self.dismiss("launch")

    def action_cancel(self):
        self.dismiss("")


# ─── Add Link Screen ────────────────────────────────────────────────

class AddLinkScreen(ModalForm):
    """Add a link to a workstream. Built on ModalForm base."""

    LINK_DESCRIPTIONS = {
        "worktree": "Git worktree path (e.g. ~/dev/project)",
        "ticket": "Jira/GitHub ticket ID (e.g. UB-1234)",
        "url": "Web URL",
        "file": "Local directory or file path",
        "claude-session": "Claude session ID",
        "slack": "Slack channel or thread URL",
    }

    def __init__(self, ws_name: str):
        self.ws_name = ws_name
        super().__init__(
            title=f"Add Link: {ws_name}",
            hint=f"[{C_DIM}]Enter[/{C_DIM}] add  [{C_DIM}]^H[/{C_DIM}] back",
        )

    def compose_form(self) -> ComposeResult:
        yield Select([(k, k) for k in LINK_KINDS], value="url", id="addlink-kind")
        yield Input(placeholder="Value (URL, path, ticket ID...)", id="addlink-value")
        yield Static(f"[{C_DIM}]{self.LINK_DESCRIPTIONS.get('url', '')}[/{C_DIM}]", id="addlink-desc")

    def on_mount(self):
        self.query_one("#addlink-value", Input).focus()

    @on(Select.Changed, "#addlink-kind")
    def _on_kind_changed(self, event: Select.Changed):
        desc = self.LINK_DESCRIPTIONS.get(str(event.value), "")
        try:
            self.query_one("#addlink-desc", Static).update(f"[{C_DIM}]{desc}[/{C_DIM}]")
        except Exception:
            pass

    @on(Input.Submitted, "#addlink-value")
    def on_value_submitted(self):
        self._create()

    def _create(self):
        kind = self.query_one("#addlink-kind", Select).value
        value = self.query_one("#addlink-value", Input).value.strip()
        if not value:
            self.app.notify("Value cannot be empty", severity="error", timeout=2)
            return
        self.dismiss(Link(kind=kind, label=kind, value=value))


# ─── Link Session Screen ────────────────────────────────────────────

class LinkSessionScreen(FuzzyPickerScreen):
    """Select a workstream to link a session to — with fuzzy search."""

    def __init__(self, store: Store, session: ClaudeSession):
        self._store = store
        self._session = session
        title = session.display_name
        super().__init__(title=f"Link session: {title}")

    def _get_items(self) -> list[tuple[str, str]]:
        items = []
        for ws in self._store.active:
            label = f"\u25cf {_rich_escape(ws.name)}  [{C_DIM}]{ws.category.value}[/{C_DIM}]"
            items.append((ws.id, label))
        return items

    def _on_selected(self, item_id: str) -> None:
        ws = self._store.get(item_id)
        if ws:
            self.dismiss(ws)
        else:
            self.dismiss(None)


# ─── Session Picker Screen ────────────────────────────────────────────

class SessionPickerScreen(_VimOptionListMixin, ModalScreen[ClaudeSession | None]):
    """Pick a session to resume from a workstream's matching sessions."""

    _option_list_id = "threadpick-list"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("enter", "confirm", "Resume"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    SessionPickerScreen {{ align: center middle; }}
    #threadpick-container {{
        width: 90%; height: auto; max-height: 85%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #threadpick-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #threadpick-list {{ height: auto; max-height: 24; }}
    #threadpick-list > .option-list--option-highlighted {{
        background: $primary 15%;
    }}
    #threadpick-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, ws: Workstream, sessions: list[ClaudeSession]):
        super().__init__()
        self.ws = ws
        self.picker_sessions = sessions
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="threadpick-container"):
            yield Label(f"Resume: {self.ws.name}", id="threadpick-title")
            yield OptionList(*self._build_options(), id="threadpick-list")
            yield Static(
                f"[{C_DIM}]Enter[/{C_DIM}] resume  [{C_DIM}]^H[/{C_DIM}] back",
                id="threadpick-hint",
            )

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._generate_titles()
        self._rebuild_options()
        self.set_interval(10, self._schedule_picker_liveness)

    def _schedule_picker_liveness(self):
        self._do_picker_liveness()

    @work(thread=True, exclusive=True, group="picker_liveness")
    def _do_picker_liveness(self):
        from actions import refresh_liveness
        refresh_liveness(self.picker_sessions)
        self.app.call_from_thread(self._rebuild_options)

    @work(thread=True)
    def _generate_titles(self):
        from thread_namer import get_session_title, title_sessions
        untitled = [s for s in self.picker_sessions if not get_session_title(s)]
        if untitled:
            title_sessions(untitled)
            self.app.call_from_thread(self._rebuild_options)

    def _picker_line_width(self) -> int:
        try:
            olist = self.query_one("#threadpick-list", OptionList)
            w = olist.size.width
            return w - 2 if w > 20 else 0
        except Exception:
            return 0

    def _build_options(self) -> list[Option]:
        lw = self._picker_line_width()
        options = []
        for i, s in enumerate(self.picker_sessions):
            act = session_activity(s, self._last_seen_cache)
            seen = _is_session_seen(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
            options.append(Option(prompt, id=str(i)))
        return options

    def _rebuild_options(self):
        olist = self.query_one("#threadpick-list", OptionList)
        highlighted = olist.highlighted
        options = self._build_options()
        olist.clear_options()
        olist.add_options(options)
        if highlighted is not None:
            olist.highlighted = highlighted

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_confirm()

    def action_confirm(self):
        option_list = self.query_one("#threadpick-list", OptionList)
        idx = option_list.highlighted
        if idx is not None and idx < len(self.picker_sessions):
            self.dismiss(self.picker_sessions[idx])
            return
        self.app.notify("No session selected", severity="error", timeout=2)

    def action_cancel(self):
        self.dismiss(None)


# ─── Repo Picker Screen ──────────────────────────────────────────────


class RepoPickerScreen(FuzzyPickerScreen):
    """fzf-style fuzzy repo picker — rebuilt on FuzzyPickerScreen."""

    def __init__(self, repos: list[str], ws_counts: dict[str, int]):
        self.all_repos = repos
        self.ws_counts = ws_counts
        super().__init__(title="Select Repository")

    def _get_items(self) -> list[tuple[str, str]]:
        home_str = str(Path.home())
        # Repos with workstreams first, then alphabetical
        with_ws = sorted(
            (r for r in self.all_repos if self.ws_counts.get(r, 0) > 0),
            key=lambda r: Path(r).name.lower(),
        )
        without_ws = sorted(
            (r for r in self.all_repos if self.ws_counts.get(r, 0) == 0),
            key=lambda r: Path(r).name.lower(),
        )
        items = []
        for repo in with_ws + without_ws:
            name = Path(repo).name
            short = repo.replace(home_str, "~")
            n_ws = self.ws_counts.get(repo, 0)
            if n_ws > 0:
                label = f"[bold]{name}[/bold]  [dim]({n_ws} ws)[/dim]  [{C_DIM}]{short}[/{C_DIM}]"
            else:
                label = f"[{C_DIM}]{name}  {short}[/{C_DIM}]"
            items.append((repo, label))
        return items

    def _on_selected(self, item_id: str) -> None:
        self.dismiss(item_id)


# ─── Workstream Picker Screen (for repo-spawn) ──────────────────────

_SENTINEL_NEW = "__new__"


class WorkstreamPickerScreen(FuzzyPickerScreen):
    """Pick a workstream for a repo, or create a new one.

    Dismisses with:
      - Workstream if an existing one was picked
      - _SENTINEL_NEW string if "Create new" was picked
      - None if cancelled
    """

    def __init__(self, workstreams: list[Workstream], repo_path: str):
        self.workstreams = workstreams
        self.repo_path = repo_path
        repo_name = Path(repo_path).name
        super().__init__(title=f"Workstreams in {repo_name}")

    def _get_items(self) -> list[tuple[str, str]]:
        items = []
        for ws in self.workstreams:
            label = f"\u25cf {_rich_escape(ws.name)}  [{C_DIM}]{ws.category.value}[/{C_DIM}]"
            items.append((ws.id, label))
        items.append((_SENTINEL_NEW, f"[{C_GREEN}]+ Create new workstream[/{C_GREEN}]"))
        return items

    def _on_selected(self, item_id: str) -> None:
        if item_id == _SENTINEL_NEW:
            self.dismiss(_SENTINEL_NEW)
            return
        for ws in self.workstreams:
            if ws.id == item_id:
                self.dismiss(ws)
                return
        self.dismiss(None)


# ─── Confirm Screen ─────────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape", "deny", "No"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
    ]

    def action_go_back(self):
        self.dismiss(False)

    DEFAULT_CSS = f"""
    ConfirmScreen {{ align: center middle; }}
    #confirm-container {{
        width: 50; height: auto; padding: 1 2;
        background: {BG_BASE}; border: round $error 40%;
    }}
    #confirm-msg {{ text-align: center; padding: 1; }}
    #confirm-hint {{ text-align: center; color: {C_DIM}; }}
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-container"):
            yield Static(self.message, id="confirm-msg")
            yield Static(f"[{C_DIM}]y[/{C_DIM}] yes  [{C_DIM}]n[/{C_DIM}] no  [{C_DIM}]^H[/{C_DIM}] back", id="confirm-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_deny(self):
        self.dismiss(False)


# ─── Current Sessions Screen ─────────────────────────────────────────

class CurrentSessionsScreen(_VimOptionListMixin, ModalScreen[None]):
    """Cross-workstream view: all non-deferred, non-archived sessions active today."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back", priority=True),
        Binding("backspace,ctrl+h", "dismiss", "back"),
        Binding("enter,l", "select_session", show=False),
        Binding("r", "resume", "Resume"),
        Binding("colon", "command_palette", ":", show=False),
        Binding("question_mark", "help", "?", show=False),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    CurrentSessionsScreen {{ align: center middle; }}
    #csd-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}
    #detail-tab-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_CHROME};
    }}
    #csd-title {{
        text-style: bold;
        background: {BG_RAISED};
        padding: 0 2;
    }}
    #csd-sessions {{
        height: 1fr;
        margin: 0 1; padding: 0;
        border: none;
        background: {BG_BASE};
    }}
    #csd-sessions > .option-list--option-highlighted {{
        background: #101010;
    }}
    #csd-no-sessions {{
        padding: 1 3;
        color: {C_DIM};
    }}
    #csd-help {{
        height: 1;
        padding: 0 2;
        background: {BG_CHROME};
        color: {C_DIM};
        dock: bottom;
    }}
    """

    _option_list_id = "csd-sessions"

    def __init__(self):
        super().__init__()
        self._sessions: list[tuple] = []  # list of (Workstream, ClaudeSession)
        self._session_ws_map: dict[str, object] = {}  # session_id -> Workstream
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}
        self._animating_sessions: list[tuple[int, ThreadActivity]] = []
        self._refresh_timer = None
        self._throbber_timer = None

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical as V
        with V(id="csd-container"):
            yield Static("", id="detail-tab-bar")
            yield Static(
                f"[bold {C_CYAN}]Sessions[/bold {C_CYAN}]  [{C_DIM}]today · active[/{C_DIM}]",
                id="csd-title",
            )
            yield OptionList(id="csd-sessions")
            yield Static(f"[{C_DIM}]No sessions active today[/{C_DIM}]", id="csd-no-sessions")
            yield Static(
                f"[{C_DIM}]↑↓/jk nav  enter/l/r open  ^H/esc back[/{C_DIM}]",
                id="csd-help",
            )

    def on_mount(self) -> None:
        self._last_seen_cache = load_last_seen()
        self._load_sessions()
        self._refresh_timer = self.set_interval(5.0, self._load_sessions)
        self._throbber_timer = self.set_interval(0.15, self._tick_throbber)

    def on_screen_resume(self) -> None:
        self._last_seen_cache = load_last_seen()
        self._load_sessions()

    def _tick_throbber(self) -> None:
        if self._animating_sessions:
            self._throbber_frame += 1
            self._build_list()

    def _load_sessions(self) -> None:
        app = self.app
        if not hasattr(app, "state"):
            return
        results = []
        for ws in app.state.store.active:
            sessions = app.state.sessions_for_ws(ws, include_archived_sessions=False)
            deferred = set(ws.deferred_sessions)
            for s in sessions:
                if s.session_id in deferred:
                    continue
                if not _is_today(s.last_activity or ""):
                    continue
                results.append((ws, s))
        results.sort(key=lambda x: x[1].last_activity or "", reverse=True)
        self._sessions = results
        self._session_ws_map = {s.session_id: ws for ws, s in results}
        self._build_list()

    def _build_list(self) -> None:
        try:
            olist = self.query_one("#csd-sessions", OptionList)
            no_sess = self.query_one("#csd-no-sessions", Static)
        except Exception:
            return

        if not self._sessions:
            olist.display = False
            no_sess.display = True
            return

        olist.display = True
        no_sess.display = False

        try:
            lw = olist.size.width - 4
        except Exception:
            lw = 80

        animating = []
        options = []
        idx = 0

        # Group by workstream, section order = most-recent-session first
        ws_groups: dict[str, list] = {}
        ws_order: list[str] = []
        ws_map_by_id: dict[str, object] = {}
        for ws, s in self._sessions:
            if ws.id not in ws_groups:
                ws_groups[ws.id] = []
                ws_order.append(ws.id)
                ws_map_by_id[ws.id] = ws
            ws_groups[ws.id].append(s)

        for ws_id in ws_order:
            ws = ws_map_by_id[ws_id]
            group = ws_groups[ws_id]
            icon = getattr(ws, "icon", "") or "◆"
            ws_label = f"[{C_CYAN}]{icon} {_rich_escape(ws.name)}[/{C_CYAN}]"
            options.append(Option(ws_label, id=f"__ws__{ws_id}", disabled=True))
            idx += 1
            for s in group:
                act = session_activity(s, self._last_seen_cache)
                if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                    animating.append((idx, act))
                seen = _is_session_seen(s, self._last_seen_cache)
                prompt = _render_session_option(
                    s, act, self._throbber_frame,
                    ws_repo_path=ws.repo_path or "",
                    seen=seen,
                    line_width=lw,
                )
                options.append(Option(prompt, id=s.session_id))
                idx += 1

        self._animating_sessions = animating

        # Use in-place updates when structure is unchanged — clear_options() +
        # add_options() remounts every option widget and triggers a full CSS
        # matching pass per option, which is very expensive at 10fps.
        old_idx = olist.highlighted
        if olist.option_count == len(options):
            with self.app.batch_update():
                for i, opt in enumerate(options):
                    try:
                        existing = olist.get_option_at_index(i)
                        if existing.prompt != opt.prompt:
                            olist.replace_option_prompt_at_index(i, opt.prompt)
                    except Exception:
                        olist.replace_option_prompt_at_index(i, opt.prompt)
        else:
            olist.clear_options()
            olist.add_options(options)
            if old_idx is not None and old_idx < olist.option_count:
                olist.highlighted = old_idx

    def _get_selected(self):
        """Return (Workstream, session_id) for the highlighted row, or (None, None)."""
        try:
            olist = self.query_one("#csd-sessions", OptionList)
            if olist.highlighted is None or not olist.option_count:
                return None, None
            opt = olist.get_option_at_index(olist.highlighted)
            sid = opt.id
            if not sid or sid.startswith("__"):
                return None, None
            ws = self._session_ws_map.get(sid)
            return ws, sid
        except Exception:
            return None, None

    def action_select_session(self) -> None:
        ws, sid = self._get_selected()
        if ws and sid:
            self.app.launch_claude_session(ws, session_id=sid)

    def action_resume(self) -> None:
        ws, sid = self._get_selected()
        if ws and sid:
            self.app.launch_claude_session(ws, session_id=sid)

    def action_command_palette(self) -> None:
        self.app.action_command_palette()

    def action_help(self) -> None:
        self.app.action_help()
