"""Modal screens — all ModalScreen subclasses for the orchestrator TUI.

Each screen is self-contained. They receive data via constructor params
and return results via dismiss(). No direct access to app state.
"""

from __future__ import annotations

import logging
import os
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
    Category, Link, Status, Store, TodoItem, Workstream,
    STATUS_ICONS, _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, load_last_seen, mark_thread_seen
from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_GOLD, C_GREEN, C_LIGHT, C_ORANGE, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE, BG_RAISED, BG_SURFACE,
    STATUS_THEME, CATEGORY_THEME,
    LINK_TYPE_ICONS, LINK_ORDER, LINK_KINDS,
    THROBBER_FRAMES,
    _status_markup, _category_markup, _link_icon,
    _activity_icon, _activity_badge, _is_session_seen, _parse_iso,
    _colored_tokens, _token_color_markup,
    _short_model, _short_project,
    _render_session_option, _session_title,
    _render_todo_option, _render_notification_option,
    _render_notified_session_option, QUIET_SEPARATOR_LABEL,
    _render_content_search_result, tool_bar_legend,
    TODO_UNDONE_ICON, TODO_DONE_ICON,
    _rich_escape,
)
from actions import (
    launch_orch_claude, ws_directories, resume_session_now, open_link,
    switch_to_tmux_window,
)
from notifications import Notification, dismiss_notification, dismiss_all_for_dirs
from state import fuzzy_match, content_search, SessionSearchResult


def _label_with_legend(left: str, legend: str) -> RichTable:
    """Return a Rich grid with left text and right-aligned legend."""
    t = RichTable.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right")
    t.add_row(left, legend)
    return t


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

class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("question_mark,escape", "dismiss", "Close"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
    ]

    def action_go_back(self):
        self.dismiss()

    DEFAULT_CSS = f"""
    HelpScreen {{
        align: center middle;
    }}
    #help-container {{
        width: 64;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: {BG_BASE};
        border: round $primary 30%;
    }}
    #help-content {{
        padding: 0 1;
    }}
    """

    def compose(self) -> ComposeResult:
        help_text = f"""\
[bold {C_PURPLE}] ORCHESTRATOR KEYBOARD REFERENCE [/bold {C_PURPLE}]

[bold {C_CYAN}]Navigation[/bold {C_CYAN}]
  [{C_YELLOW}]j / \u2193 / Ctrl+N[/{C_YELLOW}]   Move down
  [{C_YELLOW}]k / \u2191 / Ctrl+P[/{C_YELLOW}]   Move up
  [{C_YELLOW}]Ctrl+D / Ctrl+U[/{C_YELLOW}]  Half-page down / up
  [{C_YELLOW}]g / G[/{C_YELLOW}]            Jump to top / bottom
  [{C_YELLOW}]Ctrl+L[/{C_YELLOW}]           Drill in / resume session
  [{C_YELLOW}]Ctrl+H[/{C_YELLOW}]           Back / close
  [{C_YELLOW}]Enter[/{C_YELLOW}]            Confirm / resume session
  [{C_YELLOW}]Tab[/{C_YELLOW}]              Cycle views

[bold {C_CYAN}]Actions (Workstreams)[/bold {C_CYAN}]
  [{C_YELLOW}]a[/{C_YELLOW}]   Add new workstream
  [{C_YELLOW}]b[/{C_YELLOW}]   Brain dump (multi-line)
  [{C_YELLOW}]n[/{C_YELLOW}]   Quick todo (inline)
  [{C_YELLOW}]s/S[/{C_YELLOW}] Cycle status forward / backward
  [{C_YELLOW}]c[/{C_YELLOW}]   New Claude session (with context)
  [{C_YELLOW}]r[/{C_YELLOW}]   Resume most recent session
  [{C_YELLOW}]l[/{C_YELLOW}]   Add link
  [{C_YELLOW}]e[/{C_YELLOW}]   Todo list (full screen)
  [{C_YELLOW}]E[/{C_YELLOW}]   Rename workstream
  [{C_YELLOW}]o[/{C_YELLOW}]   Open links
  [{C_YELLOW}]x[/{C_YELLOW}]   Archive
  [{C_YELLOW}]d[/{C_YELLOW}]   Delete

[bold {C_CYAN}]Inside Claude Session[/bold {C_CYAN}]
  [{C_YELLOW}]Ctrl+D[/{C_YELLOW}]  Clean exit (returns to orch)
  [{C_YELLOW}]/exit[/{C_YELLOW}]   Clean exit (alternative)
  [{C_DIM}]Session auto-links to workstream on exit[/{C_DIM}]
  [{C_DIM}]Header bar shows live stats[/{C_DIM}]

[bold {C_CYAN}]Actions (Sessions)[/bold {C_CYAN}]
  [{C_YELLOW}]r[/{C_YELLOW}]   Resume selected session
  [{C_YELLOW}]l[/{C_YELLOW}]   Link session to workstream

[bold {C_CYAN}]Actions (Archived)[/bold {C_CYAN}]
  [{C_YELLOW}]u[/{C_YELLOW}]   Unarchive workstream
  [{C_YELLOW}]d[/{C_YELLOW}]   Permanently delete

[bold {C_CYAN}]Filters[/bold {C_CYAN}]
  [{C_YELLOW}]1[/{C_YELLOW}]   All          [{C_YELLOW}]2[/{C_YELLOW}] Work
  [{C_YELLOW}]3[/{C_YELLOW}]   Personal     [{C_YELLOW}]4[/{C_YELLOW}] Active
  [{C_YELLOW}]5[/{C_YELLOW}]   Stale        [{C_YELLOW}]/[/{C_YELLOW}] Search

[bold {C_CYAN}]Sort[/bold {C_CYAN}]
  [{C_YELLOW}]F1[/{C_YELLOW}]  Status  [{C_YELLOW}]F2[/{C_YELLOW}] Updated  [{C_YELLOW}]F3[/{C_YELLOW}] Created
  [{C_YELLOW}]F4[/{C_YELLOW}]  Category  [{C_YELLOW}]F5[/{C_YELLOW}] Name

[bold {C_CYAN}]Other[/bold {C_CYAN}]
  [{C_YELLOW}]:[/{C_YELLOW}]   Command palette    [{C_YELLOW}]p[/{C_YELLOW}] Toggle preview
  [{C_YELLOW}]R[/{C_YELLOW}]   Refresh            [{C_YELLOW}]?[/{C_YELLOW}] This help
  [{C_YELLOW}]q[/{C_YELLOW}]   Quit\
"""
        with Vertical(id="help-container"):
            yield Static(help_text, id="help-content")


# ─── Quick Note Screen ───────────────────────────────────────────────

class QuickNoteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back", priority=True),
    ]

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    QuickNoteScreen {{ align: center middle; }}
    #qnote-container {{
        width: 70; height: 9;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #qnote-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #qnote-input {{ height: 3; }}
    #qnote-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, ws: Workstream):
        super().__init__()
        self.ws = ws

    def compose(self) -> ComposeResult:
        with Vertical(id="qnote-container"):
            yield Label(f"Note: {self.ws.name}", id="qnote-title")
            yield Input(placeholder="type a note...", id="qnote-input")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] save  [{C_DIM}]^H[/{C_DIM}] back", id="qnote-hint")

    def on_mount(self):
        self.query_one("#qnote-input", Input).focus()

    @on(Input.Submitted, "#qnote-input")
    def on_submit(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)



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
        if ol.highlighted is not None and ol.highlighted < ol.option_count - 1:
            ol.action_cursor_down()

    def action_cursor_up(self):
        ol = self._olist()
        if ol.highlighted is not None and ol.highlighted > 0:
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

    _option_list_id = "todo-active"

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("a", "add_todo", "Add"),
        Binding("enter,space", "toggle_done", "Toggle done", priority=True),
        Binding("e", "edit_todo", "Edit"),
        Binding("x", "archive_todo", "Archive/Restore"),
        Binding("c", "spawn_todo", "Spawn"),
        Binding("d", "delete_todo", "Delete"),
        Binding("E", "edit_context", "Edit context"),
        Binding("K", "move_up", "Move \u2191", show=False),
        Binding("J", "move_down", "Move \u2193", show=False),
        Binding("h", "focus_active", show=False, priority=True),
        Binding("l", "focus_archived", show=False, priority=True),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss()

    DEFAULT_CSS = f"""
    TodoScreen {{ align: center middle; }}
    #todo-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}
    #todo-title {{
        text-style: bold; color: {C_PURPLE};
        padding: 1 3;
    }}
    #todo-lists {{
        height: 1fr;
    }}
    .todo-list-pane {{
        width: 1fr;
    }}
    .todo-list-label {{
        padding: 0 3;
        color: {C_BLUE};
        text-style: bold;
    }}
    #todo-active, #todo-archived {{
        height: auto;
        margin: 0 1; padding: 0;
        border: none;
        background: {BG_BASE};
    }}
    #todo-active > .option-list--option-highlighted,
    #todo-archived > .option-list--option-highlighted {{
        background: #101010;
    }}
    #todo-no-active, #todo-no-archived {{
        padding: 1 3;
        color: {C_DIM};
    }}
    #todo-archived-pane {{
        display: none;
    }}
    #todo-context {{
        height: auto;
        max-height: 6;
        padding: 0 3;
        color: {C_DIM};
        border-top: blank;
    }}
    #todo-help {{
        height: 1;
        padding: 0 2;
        background: {BG_BASE};
        color: {C_DIM};
        dock: bottom;
    }}
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store
        self._active_pane: str = "active"
        self._active_items: list[TodoItem] = []
        self._archived_items: list[TodoItem] = []
        self._rebuilding: bool = False

    @property
    def _app_state(self):
        return self.app.state

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-container"):
            yield Static(f"[bold {C_PURPLE}]Todos: {_rich_escape(self.ws.name)}[/bold {C_PURPLE}]", id="todo-title")

            with Horizontal(id="todo-lists"):
                with Vertical(id="todo-active-pane", classes="todo-list-pane"):
                    yield Static(f"[bold {C_BLUE}]Active[/bold {C_BLUE}]", id="todo-active-label", classes="todo-list-label")
                    yield OptionList(id="todo-active")
                    yield Static(f"[{C_DIM}]No todos \u2014 press a to add[/{C_DIM}]", id="todo-no-active")
                with Vertical(id="todo-archived-pane", classes="todo-list-pane"):
                    yield Static(f"[{C_DIM}]Archived[/{C_DIM}]", id="todo-archived-label", classes="todo-list-label")
                    yield OptionList(id="todo-archived")
                    yield Static(f"[{C_DIM}]Empty[/{C_DIM}]", id="todo-no-archived")

            yield Static("", id="todo-context")
            yield Static(self._render_help(), id="todo-help")

    def on_mount(self):
        self._rebuild()
        self.query_one("#todo-active", OptionList).focus()

    def _focused_olist(self) -> OptionList:
        if self._active_pane == "archived":
            return self.query_one("#todo-archived", OptionList)
        return self.query_one("#todo-active", OptionList)

    def _olist(self) -> OptionList:
        return self._focused_olist()

    def on_focus(self, event):
        """Sync _active_pane with actual widget focus."""
        widget = event.control
        if not isinstance(widget, OptionList):
            return
        if widget.id == "todo-archived" and self._archived_items:
            if self._active_pane != "archived":
                self._active_pane = "archived"
                self._update_pane_labels()
                self._update_context_preview()
        elif widget.id == "todo-active":
            if self._active_pane != "active":
                self._active_pane = "active"
                self._update_pane_labels()
                self._update_context_preview()

    def _rebuild(self):
        """Reload data and rebuild both panes."""
        self._rebuilding = True
        try:
            self._rebuild_inner()
        finally:
            self._rebuilding = False

    def _rebuild_inner(self):
        from state import AppState
        self.ws = self.store.get(self.ws.id) or self.ws
        self._active_items = AppState.active_todos(self.ws)
        self._archived_items = AppState.archived_todos(self.ws)

        # Active pane
        olist = self.query_one("#todo-active", OptionList)
        no_active = self.query_one("#todo-no-active", Static)
        old_id = self._highlighted_item_id(olist, self._active_items)
        old_active_idx = olist.highlighted
        olist.clear_options()
        if self._active_items:
            olist.display = True
            no_active.display = False
            for item in self._active_items:
                prompt = _render_todo_option(item)
                olist.add_option(Option(prompt, id=item.id))
            self._restore_highlight(olist, self._active_items, old_id, old_active_idx)
        else:
            olist.display = False
            no_active.display = True

        # Archived pane — only show if there are archived items or user is viewing them
        arch_olist = self.query_one("#todo-archived", OptionList)
        no_arch = self.query_one("#todo-no-archived", Static)
        arch_pane = self.query_one("#todo-archived-pane")
        old_arch_id = self._highlighted_item_id(arch_olist, self._archived_items)
        old_arch_idx = arch_olist.highlighted
        arch_olist.clear_options()
        if self._archived_items:
            arch_pane.display = True
            arch_olist.display = True
            no_arch.display = False
            for item in self._archived_items:
                prompt = _render_todo_option(item, is_archived=True)
                arch_olist.add_option(Option(prompt, id=item.id))
            self._restore_highlight(arch_olist, self._archived_items, old_arch_id, old_arch_idx)
        else:
            # Hide entire archived pane when empty
            arch_pane.display = False
            if self._active_pane == "archived":
                self._active_pane = "active"
                self.query_one("#todo-active", OptionList).focus()

        self._update_pane_labels()
        self._update_context_preview()
        # Restore focus — clear_options() can cause the OptionList to lose focus.
        # Defer to after refresh so Textual finishes processing internal events first.
        self.call_after_refresh(self._focused_olist().focus)

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

    def _update_pane_labels(self):
        active_label = self.query_one("#todo-active-label", Static)
        arch_label = self.query_one("#todo-archived-label", Static)
        na = len(self._active_items)
        narch = len(self._archived_items)
        if self._active_pane == "active":
            active_label.update(f"[bold {C_BLUE}]Active[/bold {C_BLUE}] [{C_DIM}]({na})[/{C_DIM}]")
            arch_label.update(f"[{C_DIM}]Archived ({narch})[/{C_DIM}]")
        else:
            active_label.update(f"[{C_DIM}]Active ({na})[/{C_DIM}]")
            arch_label.update(f"[bold {C_BLUE}]Archived[/bold {C_BLUE}] [{C_DIM}]({narch})[/{C_DIM}]")

    def _update_context_preview(self):
        item = self._highlighted_item()
        ctx_widget = self.query_one("#todo-context", Static)
        if item and item.context:
            is_crystal = getattr(item, "origin", "manual") == "crystallized"
            # Show first 4 lines of context
            lines = item.context.strip().split("\n")[:4]
            preview = "\n".join(lines)
            if len(item.context.strip().split("\n")) > 4:
                preview += f"\n[{C_DIM}]...[/{C_DIM}]"
            label_color = C_GOLD if is_crystal else C_BLUE
            ctx_widget.update(f"[{label_color}]Context:[/{label_color}] {preview}")
        elif item:
            ctx_widget.update(f"[{C_DIM}]No context \u2014 E to add[/{C_DIM}]")
        else:
            ctx_widget.update("")

    def _highlighted_item(self) -> TodoItem | None:
        olist = self._focused_olist()
        items = self._archived_items if self._active_pane == "archived" else self._active_items
        if olist.highlighted is not None and olist.highlighted < len(items):
            return items[olist.highlighted]
        return None

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if self._rebuilding:
            return
        # Sync pane state based on which list emitted the event
        if event.option_list.id == "todo-archived":
            if self._active_pane != "archived":
                self._active_pane = "archived"
                self._update_pane_labels()
        elif event.option_list.id == "todo-active":
            if self._active_pane != "active":
                self._active_pane = "active"
                self._update_pane_labels()
        self._update_context_preview()

    def _render_help(self) -> str:
        pairs = [
            ("a", "add"), ("Enter", "done"), ("e", "edit"), ("c", "spawn"),
            ("x", "archive"), ("d", "delete"), ("E", "context"),
            ("J/K", "reorder"), ("h/l", "panes"), ("q", "back"),
        ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    # ── Actions ────────────────────────────────────────────────────

    def action_add_todo(self):
        def on_text(text: str | None):
            if text and text.strip():
                self._app_state.add_todo(self.ws.id, text.strip())
                self._active_pane = "active"
                self._rebuild()
                # Focus active list and jump to bottom (new item)
                olist = self.query_one("#todo-active", OptionList)
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
        # Re-sort and replace all prompts in place — avoids clear_options() focus loss.
        # Option count doesn't change (toggling done doesn't remove from active).
        from state import AppState
        self.ws = self.store.get(self.ws.id) or self.ws
        new_items = AppState.active_todos(self.ws)
        olist = self._focused_olist()
        is_archived = self._active_pane == "archived"
        for i, t in enumerate(new_items):
            prompt = _render_todo_option(t, is_archived=is_archived)
            olist.replace_option_prompt_at_index(i, prompt)
        # Move highlight to the item's new position
        new_idx = next((i for i, t in enumerate(new_items) if t.id == item.id), 0)
        olist.highlighted = new_idx
        if is_archived:
            self._archived_items = new_items
        else:
            self._active_items = new_items
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

    def action_archive_todo(self):
        item = self._highlighted_item()
        if not item:
            return
        if self._active_pane == "archived":
            self._app_state.unarchive_todo(self.ws.id, item.id)
        else:
            self._app_state.archive_todo(self.ws.id, item.id)
        self._rebuild()

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
        ok, err = launch_orch_claude(self.ws, store=self.store, prompt=prompt)
        if ok:
            self._app_state.toggle_todo(self.ws.id, item.id)  # mark done
            self._rebuild()
            self.app.notify("Session spawned", timeout=2)
        else:
            self.app.notify(f"Spawn failed: {err}", severity="error", timeout=4)

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
        if item and self._active_pane == "active":
            self._app_state.reorder_todo(self.ws.id, item.id, -1)
            self._rebuild()

    def action_move_down(self):
        item = self._highlighted_item()
        if item and self._active_pane == "active":
            self._app_state.reorder_todo(self.ws.id, item.id, 1)
            self._rebuild()

    def action_focus_active(self):
        self._active_pane = "active"
        olist = self.query_one("#todo-active", OptionList)
        if olist.display:
            olist.focus()
        self._update_pane_labels()
        self._update_context_preview()

    def action_focus_archived(self):
        if not self._archived_items:
            return
        self._active_pane = "archived"
        self.query_one("#todo-archived", OptionList).focus()
        self._update_pane_labels()
        self._update_context_preview()

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

class AddScreen(ModalScreen[Workstream | None]):
    BINDINGS = [
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back"),
    ]

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    AddScreen {{ align: center middle; }}
    #add-container {{
        width: 70; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #add-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #add-container Input {{ margin: 0 0 1 0; }}
    #add-container Select {{ margin: 0 0 1 0; }}
    #add-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="add-container"):
            yield Label("New Workstream", id="add-title")
            yield Input(placeholder="Name", id="add-name")
            yield Input(placeholder="Description (optional)", id="add-desc")
            yield Select(
                [(c.value, c) for c in Category],
                value=Category.PERSONAL,
                id="add-category",
            )
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] create  [{C_DIM}]^H[/{C_DIM}] back", id="add-hint")

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

    def action_cancel(self):
        self.dismiss(None)


# ─── Detail Screen ──────────────────────────────────────────────────

class DetailScreen(_VimOptionListMixin, ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Back", priority=True),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("ctrl+l", "go_forward", "^L resume"),
        Binding("s", "cycle_status", "Status"),
        Binding("S", "cycle_status_back", "Status\u2190"),
        Binding("c", "spawn", "Spawn"),
        Binding("r", "resume", "Resume"),
        Binding("n", "quick_note", "+todo"),
        Binding("L", "add_link", "Link+"),
        Binding("e", "open_todos", "Todos"),
        Binding("o", "open_links", "Open links"),
        Binding("x", "archive", "Archive"),
        Binding("p", "peek_session", "Peek", priority=True),
        Binding("h", "go_back", show=False),
        Binding("enter,l", "select_session", show=False),
        Binding("d", "dismiss_notification", "Dismiss", show=False),
        Binding("D", "dismiss_all_notifications", "Dismiss all", show=False),
        Binding("/", "search", "Search", show=False, priority=True),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    DetailScreen {{ align: center middle; }}
    #detail-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}
    #detail-header {{
        height: auto;
        padding: 1 3;
        background: {BG_BASE};
    }}
    #detail-title {{ text-style: bold; }}
    #detail-meta {{ color: {C_DIM}; }}
    #detail-desc {{ padding-top: 1; }}
    #detail-lists {{
        height: auto; max-height: 50%;
    }}
    .detail-list-pane {{
        width: 1fr;
        border: blank;
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
    }}
    #detail-scroll {{
        width: 3fr;
        border: blank;
    }}
    #detail-scroll.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
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
        background: {BG_BASE};
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
        self._feed_notifications: list[Notification] = []
        self._session_notifications: dict[str, Notification] = {}  # session_id -> latest notif
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}
        self._active_pane: str = "sessions"
        self._animating_sessions: list[tuple[int, ThreadActivity]] = []
        self._animating_archived: list[tuple[int, ThreadActivity]] = []
        self._content_cache: dict[str, list] = {}  # session_id -> list[SessionMessage]
        self._content_ready: bool = False
        self._content_results: list[SessionSearchResult] = []
        self._content_search_active: bool = False
        # Full unfiltered lists (set once sessions are loaded)
        self._all_sessions: list[ClaudeSession] = []
        self._all_archived: list[ClaudeSession] = []
        self._peek_mode: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-container"):
            with Vertical(id="detail-header"):
                yield Static(self._render_title(), id="detail-title")
                yield Static(self._render_meta(), id="detail-meta")
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
        self.query_one("#detail-sessions", OptionList).focus()
        self._update_pane_labels()
        self.set_interval(10, self._schedule_liveness_refresh)
        self.set_interval(10, self._poll_feed)

    def _mark_all_seen(self):
        """Mark all sessions in this workstream as seen right now."""
        app = self.app
        if hasattr(app, 'state'):
            sessions = app.state.sessions_for_ws(self.ws, include_archived_sessions=True)
        else:
            sessions = self._detail_sessions + self._archived_sessions
        for s in sessions:
            mark_thread_seen(s.session_id)
        self._last_seen_cache = load_last_seen()

    def _schedule_liveness_refresh(self):
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
        self.app.call_from_thread(self._rebuild_all_options)

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
    }

    def _update_pane_labels(self):
        sess_label = self.query_one("#detail-sessions-label", Static)
        arch_label = self.query_one("#detail-archived-label", Static)
        n_active = len(self._detail_sessions)
        n_archived = len(self._archived_sessions)
        n_notified = len(self._session_notifications)

        legend = tool_bar_legend()
        notif_badge = f" [{C_GREEN}]({n_notified} new)[/{C_GREEN}]" if n_notified else ""
        if self._active_pane == "sessions":
            left = f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}] [{C_DIM}]({n_active})[/{C_DIM}]{notif_badge}"
        else:
            left = f"[{C_DIM}]Sessions ({n_active})[/{C_DIM}]{notif_badge}"
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
        self._refresh()

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
        olist = self.query_one("#detail-sessions", OptionList)

        # Rebuild respecting the notified/separator/quiet structure
        # Rather than tracking indices through the separator, just do a full rebuild
        # which is safe since replace_option_prompt_at_index preserves highlight
        notified = []
        quiet = []
        for s in self._detail_sessions:
            if s.session_id in self._session_notifications:
                notified.append(s)
            else:
                quiet.append(s)
        notified.sort(key=lambda s: self._session_notifications[s.session_id].dt, reverse=True)

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

        if notified and quiet:
            idx += 1  # skip separator

        for s in quiet:
            if idx < olist.option_count:
                act = session_activity(s, self._last_seen_cache)
                seen = _is_session_seen(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
                olist.replace_option_prompt_at_index(idx, prompt)
            idx += 1

        alw = self._session_line_width("#detail-archived")
        arch_olist = self.query_one("#detail-archived", OptionList)
        for i, s in enumerate(self._archived_sessions):
            if i < arch_olist.option_count:
                act = session_activity(s, self._last_seen_cache)
                seen = _is_session_seen(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=alw)
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
            hidden = set(self.ws.archived_sessions)
            self._all_sessions = [s for s in all_sessions if s.session_id not in hidden]
            self._all_archived = [s for s in all_sessions if s.session_id in hidden]
            log.debug("load_detail: active=%d archived=%d",
                      len(self._all_sessions), len(self._all_archived))
        else:
            from actions import find_sessions_for_ws
            self._all_sessions = find_sessions_for_ws(self.ws, getattr(app, 'sessions', []))
            self._all_archived = []

        # Apply search filter if active, otherwise show all
        if self._search_text:
            if self._content_ready:
                self._run_content_search_sync()
            else:
                self._apply_title_filter()
            return

        # Stable merge when the user is focused on that pane (avoid jarring
        # reorders mid-interaction); fresh sorted order otherwise so active
        # threads float to the top when the user looks back.
        if self._active_pane == "sessions":
            self._detail_sessions = self._stable_merge(self._detail_sessions, self._all_sessions)
        else:
            self._detail_sessions = list(self._all_sessions)
        if self._active_pane == "archived":
            self._archived_sessions = self._stable_merge(self._archived_sessions, self._all_archived)
        else:
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

        arch_olist = self.query_one("#detail-archived", OptionList)
        no_arch = self.query_one("#detail-no-archived", Static)
        arch_pane = self.query_one("#detail-archived-pane")
        arch_pane.display = True
        if self._archived_sessions:
            arch_olist.display = True
            no_arch.display = False
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
        olist.clear_options()
        animating = []

        # Split into notified (have a notification) and quiet (no notification)
        notified = []
        quiet = []
        for s in self._detail_sessions:
            if s.session_id in self._session_notifications:
                notified.append(s)
            else:
                quiet.append(s)

        # Sort notified by notification recency (newest first)
        notified.sort(key=lambda s: self._session_notifications[s.session_id].dt, reverse=True)

        # Build the unified list: notified first, separator, then quiet
        self._notified_count = len(notified)
        lw = self._session_line_width("#detail-sessions")
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
            olist.add_option(Option(prompt, id=s.session_id))
            idx += 1

        if notified and quiet:
            olist.add_option(Option(QUIET_SEPARATOR_LABEL, id="__separator__", disabled=True))
            idx += 1

        for s in quiet:
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((idx, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw)
            olist.add_option(Option(prompt, id=s.session_id))
            idx += 1

        self._animating_sessions = animating
        log.debug("build_session_list: %d notified + %d quiet, option_count=%d",
                  len(notified), len(quiet), olist.option_count)

    def _build_archived_list(self):
        olist = self.query_one("#detail-archived", OptionList)
        olist.clear_options()
        animating = []
        alw = self._session_line_width("#detail-archived")
        for i, s in enumerate(self._archived_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((i, act))
            seen = _is_session_seen(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path, seen=seen, line_width=alw)
            olist.add_option(Option(prompt, id=f"a:{s.session_id}"))
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
        olist.clear_options()
        for notif in self._feed_notifications:
            prompt = _render_notification_option(notif)
            olist.add_option(Option(prompt, id=f"notif:{notif.id}"))

    def _poll_feed(self):
        old_notif_sids = set(self._session_notifications.keys())
        self._load_feed()
        new_notif_sids = set(self._session_notifications.keys())
        # If notification-to-session mapping changed, rebuild the session list
        if old_notif_sids != new_notif_sids:
            self._load_detail_sessions()

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
        panels.append("detail-scroll")
        # Feed pane no longer in cycle — notifications are inline in sessions
        return panels

    _PANEL_ID_TO_NAME = {
        "detail-sessions": "sessions",
        "detail-archived": "archived",
        "detail-scroll": "body",
        "detail-feed": "feed",
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
                    dirs = ws_directories(self.ws)
                    resume_session_now(self.ws, session, dirs, self.app)
                    return
            # No session to jump to — dismiss instead
            if notif and not notif.dismissed:
                dismiss_notification(notif.id)
                notif.dismissed = True
                self._load_feed()
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
            # Auto-dismiss notification when entering the session
            notif = self._session_notifications.get(sid)
            if notif and not notif.dismissed:
                dismiss_notification(notif.id)
                notif.dismissed = True
                self._session_notifications.pop(sid, None)
            dirs = ws_directories(self.ws)
            resume_session_now(self.ws, session, dirs, self.app)
        else:
            log.warning("option_selected: session not found for sid=%s, detail_sids=%s, archived_sids=%s",
                        sid, [s.session_id for s in self._detail_sessions],
                        [s.session_id for s in self._archived_sessions])

    def _render_title(self) -> str:
        return f"[bold {C_PURPLE}]{_rich_escape(self.ws.name)}[/bold {C_PURPLE}]"

    def _render_meta(self) -> str:
        parts = [_status_markup(self.ws.status), _category_markup(self.ws.category)]
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
            ("^L", "resume"), ("s/S", "status"), ("c", "spawn"),
            ("n", "+todo"), ("e", "todos"), ("L", "+link"),
            ("o", "open"), ("x", "archive ws"),
            ("space", "archive/restore"),
            ("p", "peek"),
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

    def action_dismiss(self) -> None:
        """Override dismiss: close peek, then search, then screen."""
        if self._peek_mode:
            self._close_peek()
        elif self._search_is_active():
            self._cancel_search()
        else:
            super().action_dismiss(None)

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
            return
        title_text = _session_title(session)
        header = (
            f"[bold {C_BLUE}]{_rich_escape(title_text)}[/bold {C_BLUE}]  "
            f"[{C_DIM}]{session.age} · {_short_model(session.model)} · "
            f"{session.message_count} msgs · {session.tokens_display}[/{C_DIM}]\n"
            f"[{C_DIM}]p[/{C_DIM}] close  [{C_DIM}]j/k[/{C_DIM}] scroll"
        )
        olist.clear_options()
        olist.add_option(Option(header, id="peek-header"))
        for i, msg in enumerate(messages):
            if msg.role == "user":
                role_fmt = f"[bold {C_CYAN}]you[/bold {C_CYAN}]"
            else:
                role_fmt = f"[bold {C_PURPLE}]claude[/bold {C_PURPLE}]"
            text = msg.text
            if len(text) > 2000:
                text = text[:2000] + "\n…(truncated)"
            prompt = f"{role_fmt}\n[{C_LIGHT}]{_rich_escape(text)}[/{C_LIGHT}]"
            olist.add_option(Option(prompt, id=f"peek-msg-{i}"))
        if olist.option_count > 0:
            olist.highlighted = olist.option_count - 1
        self._peek_mode = True
        sess_label = self.query_one("#detail-sessions-label", Static)
        sess_label.update(
            f"[bold {C_BLUE}]Conversation[/bold {C_BLUE}] "
            f"[{C_DIM}]({len(messages)} messages)[/{C_DIM}]"
        )

    def _close_peek(self):
        """Restore the normal session list."""
        self._peek_mode = False
        self._build_session_list()
        olist = self.query_one("#detail-sessions", OptionList)
        if olist.option_count > 0:
            olist.highlighted = 0
        self._update_pane_labels()

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
        olist.clear_options()

        if self._content_results:
            olist.display = True
            no_sess.display = False
            for r in self._content_results:
                prompt = _render_content_search_result(r, ws_repo_path=self.ws.repo_path)
                olist.add_option(Option(prompt, id=r.session.session_id))
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
        self.query_one("#detail-title", Static).update(self._render_title())
        self.query_one("#detail-meta", Static).update(self._render_meta())
        self.query_one("#detail-body", Static).update(self._render_body())
        self._load_detail_sessions()
        self._load_feed()

    def action_cycle_status(self):
        statuses = list(Status)
        idx = statuses.index(self.ws.status)
        self.ws.set_status(statuses[(idx + 1) % len(statuses)])
        self.store.update(self.ws)
        self._refresh()

    def action_cycle_status_back(self):
        statuses = list(Status)
        idx = statuses.index(self.ws.status)
        self.ws.set_status(statuses[(idx - 1) % len(statuses)])
        self.store.update(self.ws)
        self._refresh()

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

    def action_spawn(self):
        # If there's a live tmux window for this ws whose session hasn't
        # appeared in our session list yet (still composing first prompt),
        # switch to it instead of spawning a fresh one.
        wid = self._find_composing_window()
        if wid and switch_to_tmux_window(wid):
            return
        ok, err = launch_orch_claude(self.ws, store=self.store)
        if ok:
            self.app.notify("Session spawned", timeout=2)
            self._add_spawning_placeholder()
        else:
            self.app.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def _find_composing_window(self) -> str | None:
        """Find a tmux window for this ws where the user hasn't submitted yet."""
        from actions import find_tmux_windows_for_ws
        all_sessions = self._all_sessions + self._all_archived
        known_sids = {s.session_id for s in all_sessions}
        known_sids |= {sid for s in all_sessions for sid in s.all_session_ids}
        for sid, wid in find_tmux_windows_for_ws(self.ws.name):
            if sid not in known_sids:
                return wid
        return None

    def _add_spawning_placeholder(self):
        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        olist.display = True
        no_sess.display = False
        line1 = f" [bold {C_CYAN}]◉[/bold {C_CYAN}]  [bold]Starting session…[/bold]"
        line2 = f"      [{C_DIM}]waiting for Claude to initialize[/{C_DIM}]"
        olist.add_option(Option(f"{line1}\n{line2}", id="spawning"))
        self._refresh()

    def action_resume(self):
        """Resume the currently highlighted session (same as Enter)."""
        olist = self._focused_olist()
        idx = olist.highlighted
        if self._active_pane in ("sessions", "archived"):
            sessions = self._archived_sessions if self._active_pane == "archived" else self._detail_sessions
            if idx is not None and idx < len(sessions):
                session = sessions[idx]
                mark_thread_seen(session.session_id)
                self._last_seen_cache = load_last_seen()
                dirs = ws_directories(self.ws)
                resume_session_now(self.ws, session, dirs, self.app)

    def action_add_link(self):
        def on_link(link: Link | None):
            if link:
                self.ws.links.append(link)
                self.ws.touch()
                self.store.update(self.ws)
                self._refresh()
                self.app.notify(f"Added {link.kind} link", timeout=2)
        self.app.push_screen(AddLinkScreen(self.ws.name), callback=on_link)



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

class BrainPreviewScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter,y", "confirm", "Add all"),
        Binding("escape,n", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
    ]

    def action_go_back(self):
        self.dismiss(False)

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
                body_lines.append(f"     {_status_markup(task.status)}  {_category_markup(task.category)}")
                if task.raw_text != task.name:
                    raw = task.raw_text[:80]
                    body_lines.append(f"     [{C_DIM}]{raw}[/{C_DIM}]")
                body_lines.append("")
            yield Static("\n".join(body_lines), id="brain-preview-body")
            yield Rule()
            yield Static(f"[{C_DIM}]Enter/y[/{C_DIM}] add all  [{C_DIM}]Esc/n[/{C_DIM}] cancel  [{C_DIM}]^H[/{C_DIM}] back", id="brain-preview-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


# ─── Add Link Screen ────────────────────────────────────────────────

class AddLinkScreen(ModalScreen[Link | None]):
    BINDINGS = [
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("escape", "go_back", "Esc back"),
    ]

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    AddLinkScreen {{ align: center middle; }}
    #addlink-container {{
        width: 70; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #addlink-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #addlink-container Input {{ margin: 0 0 1 0; }}
    #addlink-container Select {{ margin: 0 0 1 0; }}
    #addlink-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, ws_name: str):
        super().__init__()
        self.ws_name = ws_name

    def compose(self) -> ComposeResult:
        with Vertical(id="addlink-container"):
            yield Label(f"Add Link: {self.ws_name}", id="addlink-title")
            yield Select([(k, k) for k in LINK_KINDS], value="url", id="addlink-kind")
            yield Input(placeholder="Value (URL, path, ticket ID, session ID...)", id="addlink-value")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] add  [{C_DIM}]^H[/{C_DIM}] back", id="addlink-hint")

    def on_mount(self):
        self.query_one("#addlink-value", Input).focus()

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

    def action_cancel(self):
        self.dismiss(None)


# ─── Link Session Screen ────────────────────────────────────────────

class LinkSessionScreen(_VimOptionListMixin, ModalScreen[Workstream | None]):
    """Select a workstream to link a session to."""

    _option_list_id = "linksession-list"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("enter", "confirm", "Link"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    LinkSessionScreen {{ align: center middle; }}
    #linksession-container {{
        width: 70; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #linksession-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #linksession-list {{ height: auto; max-height: 20; }}
    #linksession-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, store: Store, session: ClaudeSession):
        super().__init__()
        self.store = store
        self.session = session

    def compose(self) -> ComposeResult:
        title = self.session.display_name
        with Vertical(id="linksession-container"):
            yield Label(f"Link session to workstream: {title}", id="linksession-title")
            options = []
            for ws in self.store.active:
                options.append(Option(
                    f"{STATUS_ICONS[ws.status]} {_rich_escape(ws.name)}  ({ws.category.value})",
                    id=ws.id,
                ))
            if not options:
                options.append(Option("(no workstreams)", id="none", disabled=True))
            yield OptionList(*options, id="linksession-list")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] link  [{C_DIM}]^H[/{C_DIM}] back", id="linksession-hint")

    def action_confirm(self):
        option_list = self.query_one("#linksession-list", OptionList)
        idx = option_list.highlighted
        if idx is not None:
            opt = option_list.get_option_at_index(idx)
            ws = self.store.get(str(opt.id))
            if ws:
                self.dismiss(ws)
                return
        self.app.notify("No workstream selected", severity="error", timeout=2)

    def action_cancel(self):
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
        olist.clear_options()
        for opt in self._build_options():
            olist.add_option(opt)
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

class RepoPickerScreen(ModalScreen[str | None]):
    """fzf-style fuzzy repo picker with full home-dir scanning."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("enter", "confirm", "Select"),
    ]

    def action_go_back(self):
        inp = self.query_one("#repopick-input", Input)
        if inp.value:
            return  # let Input handle backspace normally
        self.dismiss(None)

    DEFAULT_CSS = f"""
    RepoPickerScreen {{ align: center middle; }}
    #repopick-container {{
        width: 80; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #repopick-input {{
        dock: top; margin-bottom: 1;
    }}
    #repopick-list {{ height: auto; max-height: 24; }}
    #repopick-list > .option-list--option-highlighted {{
        background: $primary 15%;
    }}
    #repopick-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, repos: list[str], ws_counts: dict[str, int]):
        super().__init__()
        self.all_repos = repos
        self.ws_counts = ws_counts  # repo_path -> number of workstreams
        self._filtered: list[str] = []  # current filtered list shown

    def compose(self) -> ComposeResult:
        with Vertical(id="repopick-container"):
            yield Input(placeholder="Type to filter repos…", id="repopick-input")
            yield OptionList(id="repopick-list")
            yield Static("", id="repopick-hint")

    def on_mount(self) -> None:
        self._rebuild_list("")
        self.query_one("#repopick-input", Input).focus()

    def _rebuild_list(self, query: str) -> None:
        """Recompute filtered repos and repopulate the OptionList."""
        home_str = str(Path.home())

        if query:
            scored: list[tuple[int, str]] = []
            for repo in self.all_repos:
                basename = Path(repo).name
                # Match against both basename and full path
                s1 = fuzzy_match(query, basename)
                s2 = fuzzy_match(query, repo)
                best = max(s for s in (s1, s2) if s is not None) if (s1 is not None or s2 is not None) else None
                if best is not None:
                    scored.append((best, repo))
            scored.sort(key=lambda t: -t[0])
            self._filtered = [repo for _, repo in scored]
        else:
            # No query: repos with workstreams first, then alpha
            with_ws = sorted(
                (r for r in self.all_repos if self.ws_counts.get(r, 0) > 0),
                key=lambda r: Path(r).name.lower(),
            )
            without_ws = sorted(
                (r for r in self.all_repos if self.ws_counts.get(r, 0) == 0),
                key=lambda r: Path(r).name.lower(),
            )
            self._filtered = with_ws + without_ws

        ol = self.query_one("#repopick-list", OptionList)
        ol.clear_options()
        for repo in self._filtered:
            name = Path(repo).name
            short = repo.replace(home_str, "~")
            n_ws = self.ws_counts.get(repo, 0)
            if n_ws > 0:
                label = f"[bold]{name}[/bold]  [dim]({n_ws} ws)[/dim]  [{C_DIM}]{short}[/{C_DIM}]"
            else:
                label = f"[{C_DIM}]{name}  {short}[/{C_DIM}]"
            ol.add_option(Option(label, id=repo))

        if not self._filtered:
            ol.add_option(Option(f"[{C_DIM}](no matches)[/{C_DIM}]", id="__none__", disabled=True))

        # Status line
        n_with = sum(1 for r in self._filtered if self.ws_counts.get(r, 0) > 0)
        hint = self.query_one("#repopick-hint", Static)
        hint.update(
            f"[{C_DIM}]{len(self._filtered)} repos · {n_with} with workstreams  "
            f"  Enter select  Esc cancel[/{C_DIM}]"
        )

    @on(Input.Changed, "#repopick-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._rebuild_list(event.value)

    def _on_key(self, event) -> None:
        """Route navigation keys to the option list while input stays focused."""
        ol = self.query_one("#repopick-list", OptionList)
        key = event.key
        if key in ("down", "ctrl+n"):
            if ol.highlighted is not None and ol.highlighted < ol.option_count - 1:
                ol.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif key in ("up", "ctrl+p"):
            if ol.highlighted is not None and ol.highlighted > 0:
                ol.action_cursor_up()
            event.prevent_default()
            event.stop()

    def action_confirm(self):
        ol = self.query_one("#repopick-list", OptionList)
        idx = ol.highlighted
        if idx is not None and idx < len(self._filtered):
            self.dismiss(self._filtered[idx])
            return
        self.app.notify("No repo selected", severity="error", timeout=2)

    def action_cancel(self):
        self.dismiss(None)


# ─── Workstream Picker Screen (for repo-spawn) ──────────────────────

_SENTINEL_NEW = "__new__"


class WorkstreamPickerScreen(_VimOptionListMixin, ModalScreen[Workstream | str | None]):
    """Pick a workstream for a repo, or create a new one.

    Dismisses with:
      - Workstream if an existing one was picked
      - _SENTINEL_NEW string if "Create new" was picked
      - None if cancelled
    """

    _option_list_id = "wspick-list"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace,ctrl+h", "go_back", "^H back"),
        Binding("enter", "confirm", "Select"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    def action_go_back(self):
        self.dismiss(None)

    DEFAULT_CSS = f"""
    WorkstreamPickerScreen {{ align: center middle; }}
    #wspick-container {{
        width: 70; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #wspick-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #wspick-list {{ height: auto; max-height: 20; }}
    #wspick-list > .option-list--option-highlighted {{
        background: $primary 15%;
    }}
    #wspick-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, workstreams: list[Workstream], repo_path: str):
        super().__init__()
        self.workstreams = workstreams
        self.repo_path = repo_path

    def compose(self) -> ComposeResult:
        repo_name = Path(self.repo_path).name
        with Vertical(id="wspick-container"):
            yield Label(f"Workstreams in {repo_name}:", id="wspick-title")
            options = []
            for ws in self.workstreams:
                options.append(Option(
                    f"{STATUS_ICONS[ws.status]} {_rich_escape(ws.name)}  [{C_DIM}]{ws.category.value}[/{C_DIM}]",
                    id=ws.id,
                ))
            options.append(Option(
                f"[{C_GREEN}]+ Create new workstream[/{C_GREEN}]",
                id=_SENTINEL_NEW,
            ))
            yield OptionList(*options, id="wspick-list")
            yield Static(
                f"[{C_DIM}]Enter[/{C_DIM}] select  [{C_DIM}]^H[/{C_DIM}] back",
                id="wspick-hint",
            )

    def action_confirm(self):
        option_list = self.query_one("#wspick-list", OptionList)
        idx = option_list.highlighted
        if idx is not None:
            opt = option_list.get_option_at_index(idx)
            opt_id = str(opt.id)
            if opt_id == _SENTINEL_NEW:
                self.dismiss(_SENTINEL_NEW)
                return
            for ws in self.workstreams:
                if ws.id == opt_id:
                    self.dismiss(ws)
                    return
        self.app.notify("No selection", severity="error", timeout=2)

    def action_cancel(self):
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
