"""Modal screens — all ModalScreen subclasses for the orchestrator TUI.

Each screen is self-contained. They receive data via constructor params
and return results via dismiss(). No direct access to app state.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

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
    C_BLUE, C_CYAN, C_DIM, C_GREEN, C_LIGHT, C_ORANGE, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE, BG_RAISED, BG_SURFACE,
    STATUS_THEME, CATEGORY_THEME,
    LINK_TYPE_ICONS, LINK_ORDER, LINK_KINDS,
    THROBBER_FRAMES,
    _status_markup, _category_markup, _link_icon,
    _activity_icon, _activity_badge,
    _colored_tokens, _token_color_markup,
    _short_model, _short_project,
    _render_session_option, _session_title,
    _render_todo_option, _render_notification_option,
    _render_content_search_result,
    TODO_UNDONE_ICON, TODO_DONE_ICON,
)
from actions import (
    launch_orch_claude, ws_directories, resume_session_now, open_link,
)
from notifications import Notification, dismiss_notification, dismiss_all_for_dirs
from state import fuzzy_match, content_search, SessionSearchResult


# ─── Messages ────────────────────────────────────────────────────────

class SessionsChanged(Message):
    """Posted on the app when the session list has been updated."""
    pass


# ─── Help Screen ────────────────────────────────────────────────────

class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("question_mark,escape,q", "dismiss", "Close")]

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
  [{C_YELLOW}]Enter[/{C_YELLOW}]            View detail / resume session
  [{C_YELLOW}]Tab[/{C_YELLOW}]              Cycle views
  [{C_YELLOW}]Escape[/{C_YELLOW}]           Back / close

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
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

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
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] save  [{C_DIM}]Esc[/{C_DIM}] cancel", id="qnote-hint")

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
        Binding("q,escape", "dismiss", "Back"),
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
        background: #252525;
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

    @property
    def _app_state(self):
        return self.app.state

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-container"):
            yield Static(f"[bold {C_PURPLE}]Todos: {self.ws.name}[/bold {C_PURPLE}]", id="todo-title")

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
        from state import AppState
        self.ws = self.store.get(self.ws.id) or self.ws
        self._active_items = AppState.active_todos(self.ws)
        self._archived_items = AppState.archived_todos(self.ws)

        # Active pane
        olist = self.query_one("#todo-active", OptionList)
        no_active = self.query_one("#todo-no-active", Static)
        old_id = self._highlighted_item_id(olist, self._active_items)
        olist.clear_options()
        if self._active_items:
            olist.display = True
            no_active.display = False
            for item in self._active_items:
                prompt = _render_todo_option(item)
                olist.add_option(Option(prompt, id=item.id))
            self._restore_highlight(olist, self._active_items, old_id)
        else:
            olist.display = False
            no_active.display = True

        # Archived pane — only show if there are archived items or user is viewing them
        arch_olist = self.query_one("#todo-archived", OptionList)
        no_arch = self.query_one("#todo-no-archived", Static)
        arch_pane = self.query_one("#todo-archived-pane")
        old_arch_id = self._highlighted_item_id(arch_olist, self._archived_items)
        arch_olist.clear_options()
        if self._archived_items:
            arch_pane.display = True
            arch_olist.display = True
            no_arch.display = False
            for item in self._archived_items:
                prompt = _render_todo_option(item, is_archived=True)
                arch_olist.add_option(Option(prompt, id=item.id))
            self._restore_highlight(arch_olist, self._archived_items, old_arch_id)
        else:
            # Hide entire archived pane when empty
            arch_pane.display = False
            if self._active_pane == "archived":
                self._active_pane = "active"
                self.query_one("#todo-active", OptionList).focus()

        self._update_pane_labels()
        self._update_context_preview()

    @staticmethod
    def _highlighted_item_id(olist: OptionList, items: list[TodoItem]) -> str | None:
        if olist.highlighted is not None and olist.option_count > 0:
            try:
                return olist.get_option_at_index(olist.highlighted).id
            except Exception:
                pass
        return None

    @staticmethod
    def _restore_highlight(olist: OptionList, items: list[TodoItem], item_id: str | None):
        if not olist.option_count:
            return
        if item_id:
            for i, t in enumerate(items):
                if t.id == item_id:
                    olist.highlighted = i
                    return
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
            # Show first 4 lines of context
            lines = item.context.strip().split("\n")[:4]
            preview = "\n".join(lines)
            if len(item.context.strip().split("\n")) > 4:
                preview += f"\n[{C_DIM}]...[/{C_DIM}]"
            ctx_widget.update(f"[{C_BLUE}]Context:[/{C_BLUE}] {preview}")
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
        self._rebuild()

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
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

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
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] save  [{C_DIM}]Esc[/{C_DIM}] cancel", id="todo-edit-hint")

    def on_mount(self):
        self.query_one("#todo-edit-input", Input).focus()

    @on(Input.Submitted, "#todo-edit-input")
    def on_submit(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class _TodoContextScreen(ModalScreen[None]):
    """TextArea editor for a todo item's context field."""
    BINDINGS = [Binding("escape", "save_and_close", "Save & back", priority=True)]

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
            yield Static(f"[{C_DIM}]Esc[/{C_DIM}] save & back", id="todo-ctx-hint")

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
        Binding("escape,q", "dismiss", "Back"),
        Binding("enter", "open_link", "Open"),
    ] + _VimOptionListMixin.VIM_BINDINGS

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
                options.append(Option(f"{icon}  [{lnk.kind}] {lnk.label}: {lnk.value}", id=str(i)))
            if not options:
                options.append(Option("(no links)", id="none", disabled=True))
            yield OptionList(*options, id="links-list")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] open  [{C_DIM}]Esc[/{C_DIM}] back", id="links-hint")

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
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

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
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] create  [{C_DIM}]Esc[/{C_DIM}] cancel", id="add-hint")

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
        Binding("q,escape", "dismiss", "Back"),
        Binding("s", "cycle_status", "Status"),
        Binding("S", "cycle_status_back", "Status\u2190"),
        Binding("c", "spawn", "Spawn"),
        Binding("r", "resume", "Resume"),
        Binding("n", "quick_note", "+todo"),
        Binding("L", "add_link", "Link+"),
        Binding("e", "open_todos", "Todos"),
        Binding("o", "open_links", "Open links"),
        Binding("x", "archive", "Archive"),
        Binding("a", "archive_session", "Archive/restore", priority=True),
        Binding("h", "focus_sessions", show=False, priority=True),
        Binding("l", "focus_archived", show=False, priority=True),
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
        background: #252525;
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
        width: 2fr; min-width: 28;
        border: blank;
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
        background: #252525;
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
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}
        self._active_pane: str = "sessions"
        self._animating_sessions: list[tuple[int, ThreadActivity]] = []
        self._animating_archived: list[tuple[int, ThreadActivity]] = []
        self._search_text: str = ""
        self._searching: bool = False
        self._content_cache: dict[str, list] = {}  # session_id -> list[SessionMessage]
        self._content_ready: bool = False
        self._content_results: list[SessionSearchResult] = []
        self._content_search_active: bool = False
        # Full unfiltered lists (set once sessions are loaded)
        self._all_sessions: list[ClaudeSession] = []
        self._all_archived: list[ClaudeSession] = []

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
                    yield OptionList(id="detail-sessions")
                    yield Static(f"[{C_DIM}]No sessions[/{C_DIM}]", id="detail-no-sessions")
                with Vertical(id="detail-archived-pane", classes="detail-list-pane"):
                    yield Static(f"[{C_DIM}]Archived[/{C_DIM}]", id="detail-archived-label", classes="detail-list-label")
                    yield OptionList(id="detail-archived")
                    yield Static(f"[{C_DIM}]Empty[/{C_DIM}]", id="detail-no-archived")

            with Horizontal(id="detail-lower"):
                with VerticalScroll(id="detail-scroll"):
                    yield Static(self._render_body(), id="detail-body")
                with Vertical(id="detail-feed-pane"):
                    yield Static(self._render_feed_label(), id="detail-feed-label", classes="detail-feed-label")
                    yield OptionList(id="detail-feed")
                    yield Static(f"[{C_DIM}]No notifications[/{C_DIM}]", id="detail-no-feed")

            yield Static(self._render_help(), id="detail-help")

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._load_detail_sessions()
        self._load_feed()
        self.query_one("#detail-sessions", OptionList).focus()
        self._update_pane_labels()
        self._throbber_timer = self.set_interval(0.3, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)
        self.set_interval(10, self._poll_feed)

    def _focused_olist(self) -> OptionList:
        if self._active_pane == "archived":
            return self.query_one("#detail-archived", OptionList)
        if self._active_pane == "feed":
            return self.query_one("#detail-feed", OptionList)
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
        feed_label = self.query_one("#detail-feed-label", Static)
        n_active = len(self._detail_sessions)
        n_archived = len(self._archived_sessions)
        n_feed = len([n for n in self._feed_notifications if not n.dismissed])

        if self._active_pane == "sessions":
            sess_label.update(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}] [{C_DIM}]({n_active})[/{C_DIM}]")
        else:
            sess_label.update(f"[{C_DIM}]Sessions ({n_active})[/{C_DIM}]")

        if self._active_pane == "archived":
            arch_label.update(f"[bold {C_BLUE}]Archived[/bold {C_BLUE}] [{C_DIM}]({n_archived})[/{C_DIM}]")
        else:
            arch_label.update(f"[{C_DIM}]Archived ({n_archived})[/{C_DIM}]")

        if self._active_pane == "feed":
            feed_label.update(f"[bold {C_BLUE}]Feed[/bold {C_BLUE}] [{C_DIM}]({n_feed})[/{C_DIM}]")
        elif n_feed:
            feed_label.update(f"[{C_DIM}]Feed[/{C_DIM}] [{C_GREEN}]({n_feed})[/{C_GREEN}]")
        else:
            feed_label.update(f"[{C_DIM}]Feed[/{C_DIM}]")

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

    def _refresh_session_liveness(self):
        from actions import refresh_liveness
        from sessions import refresh_session_tail
        refresh_liveness(self._detail_sessions)
        refresh_liveness(self._archived_sessions)
        # Tail-read live/recently-live sessions to pick up new messages
        for s in self._detail_sessions:
            if s.is_live:
                refresh_session_tail(s)
        for s in self._archived_sessions:
            if s.is_live:
                refresh_session_tail(s)
        # Rebuild all options so non-animating sessions also update
        self._rebuild_all_options()
        self._update_animating_cache()

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
        olist = self.query_one("#detail-sessions", OptionList)
        for i, s in enumerate(self._detail_sessions):
            if i < olist.option_count:
                act = session_activity(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)
        arch_olist = self.query_one("#detail-archived", OptionList)
        for i, s in enumerate(self._archived_sessions):
            if i < arch_olist.option_count:
                act = session_activity(s, self._last_seen_cache)
                prompt = _render_session_option(s, act, self._throbber_frame)
                arch_olist.replace_option_prompt_at_index(i, prompt)

    def _tick_throbber(self):
        self._throbber_frame += 1
        # Only update options that are actually animating (cached from last build)
        # Recompute activity fresh so snippet styling stays in sync with state
        olist = self.query_one("#detail-sessions", OptionList)
        for i, _cached_act in self._animating_sessions:
            if i < olist.option_count and i < len(self._detail_sessions):
                act = session_activity(self._detail_sessions[i], self._last_seen_cache)
                prompt = _render_session_option(self._detail_sessions[i], act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)
        arch_olist = self.query_one("#detail-archived", OptionList)
        for i, _cached_act in self._animating_archived:
            if i < arch_olist.option_count and i < len(self._archived_sessions):
                act = session_activity(self._archived_sessions[i], self._last_seen_cache)
                prompt = _render_session_option(self._archived_sessions[i], act, self._throbber_frame)
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
            self._apply_search_filter()
            return

        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)

        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        if self._detail_sessions:
            olist.display = True
            no_sess.display = False
            old_sid = self._highlighted_session_id(olist)
            self._build_session_list()
            self._restore_highlight_by_sid(olist, self._detail_sessions, old_sid)
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
            self._build_archived_list()
            self._restore_highlight_by_sid(arch_olist, self._archived_sessions, old_sid)
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
    def _restore_highlight_by_sid(olist: OptionList, sessions: list, sid: str | None):
        """Restore highlight to the session with the given ID, or default to 0."""
        if not olist.option_count:
            return
        if sid:
            for i, s in enumerate(sessions):
                if s.session_id == sid:
                    olist.highlighted = i
                    return
        olist.highlighted = 0

    def _build_session_list(self):
        olist = self.query_one("#detail-sessions", OptionList)
        olist.clear_options()
        animating = []
        for i, s in enumerate(self._detail_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((i, act))
            prompt = _render_session_option(s, act, self._throbber_frame)
            log.debug("build_session_list[%d] id=%s title=%s", i, s.session_id, s.display_name)
            olist.add_option(Option(prompt, id=s.session_id))
        self._animating_sessions = animating
        log.debug("build_session_list: %d options added, option_count=%d", len(self._detail_sessions), olist.option_count)

    def _build_archived_list(self):
        olist = self.query_one("#detail-archived", OptionList)
        olist.clear_options()
        animating = []
        for i, s in enumerate(self._archived_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                animating.append((i, act))
            prompt = _render_session_option(s, act, self._throbber_frame)
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

        olist = self.query_one("#detail-feed", OptionList)
        no_feed = self.query_one("#detail-no-feed", Static)

        if self._feed_notifications:
            olist.display = True
            no_feed.display = False
            old_idx = olist.highlighted
            self._build_feed_list()
            if old_idx is not None and old_idx < olist.option_count:
                olist.highlighted = old_idx
            elif olist.option_count > 0:
                olist.highlighted = 0
        else:
            olist.display = False
            no_feed.display = True
            if self._active_pane == "feed":
                self._active_pane = "sessions"
                self.query_one("#detail-sessions", OptionList).focus()

        self.query_one("#detail-feed-label", Static).update(self._render_feed_label())

    def _build_feed_list(self):
        olist = self.query_one("#detail-feed", OptionList)
        olist.clear_options()
        for notif in self._feed_notifications:
            prompt = _render_notification_option(notif)
            olist.add_option(Option(prompt, id=f"notif:{notif.id}"))

    def _poll_feed(self):
        self._load_feed()

    def action_dismiss_notification(self):
        if self._active_pane != "feed":
            return
        olist = self.query_one("#detail-feed", OptionList)
        idx = olist.highlighted
        if idx is None or idx >= len(self._feed_notifications):
            return
        notif = self._feed_notifications[idx]
        if notif.dismissed:
            return
        dismiss_notification(notif.id)
        notif.dismissed = True
        self._load_feed()

    def action_dismiss_all_notifications(self):
        if self._active_pane != "feed":
            return
        dirs = set()
        if hasattr(self.app, 'state'):
            dirs = self.app.state._ws_dirs(self.ws)
        if dirs:
            dismiss_all_for_dirs(self._feed_notifications, dirs)
        for n in self._feed_notifications:
            n.dismissed = True
        self._load_feed()

    # ── Panel navigation (Ctrl+j/k) ──

    def _panel_ids(self) -> list[str]:
        """Focusable panel widget IDs, skipping empty ones."""
        panels = ["detail-sessions"]
        if self._archived_sessions:
            panels.append("detail-archived")
        panels.append("detail-scroll")
        if self._feed_notifications:
            panels.append("detail-feed")
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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
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
                    dirs = ws_directories(self.ws)
                    resume_session_now(self.ws, session, dirs, self.app)
                    return
            # No session to jump to — dismiss instead
            if notif and not notif.dismissed:
                dismiss_notification(notif.id)
                notif.dismissed = True
                self._load_feed()
            return

        if oid is None:
            log.warning("option_selected: option_id is None! Falling back to index lookup")
            # Fallback: use index from the focused list
            olist = self._focused_olist()
            idx = olist.highlighted
            sessions = self._archived_sessions if self._active_pane == "archived" else self._detail_sessions
            if idx is not None and idx < len(sessions):
                session = sessions[idx]
                log.debug("option_selected: fallback found session %s (%s)", session.session_id, session.display_name)
                mark_thread_seen(session.session_id)
                dirs = ws_directories(self.ws)
                resume_session_now(self.ws, session, dirs, self.app)
            return
        sid = oid.removeprefix("a:")  # archived IDs are "a:<session_id>"
        session = self._find_session_by_id(sid)
        log.debug("option_selected: sid=%s found=%s", sid, session is not None)
        if session:
            mark_thread_seen(session.session_id)
            dirs = ws_directories(self.ws)
            resume_session_now(self.ws, session, dirs, self.app)
        else:
            log.warning("option_selected: session not found for sid=%s, detail_sids=%s, archived_sids=%s",
                        sid, [s.session_id for s in self._detail_sessions],
                        [s.session_id for s in self._archived_sessions])

    def _render_title(self) -> str:
        return f"[bold {C_PURPLE}]{self.ws.name}[/bold {C_PURPLE}]"

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
        dirs = ws_directories(self.ws)
        other_links = [lnk for lnk in self.ws.links
                       if lnk.kind not in ("worktree", "file")
                       or not os.path.isdir(os.path.expanduser(lnk.value))]
        if dirs or other_links:
            lines.append(f"[bold {C_BLUE}]Context[/bold {C_BLUE}]")
            for d in dirs:
                short = d.replace(str(Path.home()), "~")
                lines.append(f"  [{C_DIM}]{short}[/{C_DIM}]")
            for lnk in other_links:
                icon = _link_icon(lnk.kind)
                lines.append(f"  {icon} [{C_DIM}]{lnk.label}:[/{C_DIM}] {lnk.value}")
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
            ("Enter", "resume"), ("s/S", "status"), ("c", "spawn"),
            ("n", "+todo"), ("e", "todos"),
            ("o", "open"), ("x", "archive ws"),
            ("a", "archive/restore"),
            ("^j/^k", "panels"), ("/", "search"),
            ("q", "back"),
        ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    # ── Content search (vim-style / in help bar) ──

    def _render_search_bar(self) -> str:
        """Render the help bar content when in search mode."""
        cursor = f"[bold {C_BLUE}]▎[/bold {C_BLUE}]"
        return f"[{C_YELLOW}]/[/{C_YELLOW}]{self._search_text}{cursor}"

    def _update_help_bar(self):
        help_bar = self.query_one("#detail-help", Static)
        if self._searching:
            help_bar.update(self._render_search_bar())
        else:
            help_bar.update(self._render_help())

    def action_search(self):
        self._searching = True
        self._search_text = ""
        self._update_help_bar()
        # Start warming the content cache in background
        if not self._content_ready:
            self._warm_content_cache()

    def _cancel_search(self):
        self._searching = False
        self._search_text = ""
        self._content_search_active = False
        self._content_results = []
        self._update_help_bar()
        # Restore normal session lists
        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)
        self._rebuild_session_lists()

    def _commit_search(self):
        """Exit search mode but keep results visible."""
        self._searching = False
        self._update_help_bar()

    def _on_search_text_changed(self):
        """Called whenever search text changes."""
        self._update_help_bar()
        if not self._search_text:
            self._content_search_active = False
            self._content_results = []
            self._detail_sessions = list(self._all_sessions)
            self._archived_sessions = list(self._all_archived)
            self._rebuild_session_lists()
            return
        if self._content_ready:
            self._run_content_search_sync()
        else:
            self._apply_title_filter()

    def check_action(self, action: str, parameters) -> bool:
        """Block all non-search actions while in search mode."""
        if self._searching and action not in ("search", "dismiss"):
            return False
        return True

    def on_key(self, event) -> None:
        if not self._searching:
            return
        key = event.key
        if key == "escape":
            event.stop()
            event.prevent_default()
            self._cancel_search()
        elif key == "enter":
            event.stop()
            event.prevent_default()
            self._commit_search()
        elif key == "ctrl+w":
            event.stop()
            event.prevent_default()
            # Delete last word
            txt = self._search_text.rstrip()
            if txt:
                last_space = txt.rfind(" ")
                self._search_text = txt[:last_space + 1] if last_space >= 0 else ""
            else:
                self._search_text = ""
            self._on_search_text_changed()
        elif key == "ctrl+u":
            event.stop()
            event.prevent_default()
            self._search_text = ""
            self._on_search_text_changed()
        elif key == "backspace":
            event.stop()
            event.prevent_default()
            self._search_text = self._search_text[:-1]
            self._on_search_text_changed()
        elif event.character and event.character.isprintable():
            event.stop()
            event.prevent_default()
            self._search_text += event.character
            self._on_search_text_changed()
        elif key == "space":
            event.stop()
            event.prevent_default()
            self._search_text += " "
            self._on_search_text_changed()

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
                prompt = _render_content_search_result(r)
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

        # Hide archived pane during search (results span both)
        arch_olist = self.query_one("#detail-archived", OptionList)
        arch_olist.clear_options()
        self._archived_sessions = []
        no_arch = self.query_one("#detail-no-archived", Static)
        no_arch.display = True

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
        if self._active_pane == "archived":
            if idx is None or idx >= len(self._archived_sessions):
                log.warning("archive_session: idx out of range for archived")
                return
            sid = self._archived_sessions[idx].session_id
            log.debug("archive_session: unarchiving sid=%s", sid)
            self.ws.archived_sessions.pop(sid, None)
            self.store.update(self.ws)
        else:
            if idx is None or idx >= len(self._detail_sessions):
                log.warning("archive_session: idx out of range for detail")
                return
            sid = self._detail_sessions[idx].session_id
            log.debug("archive_session: archiving sid=%s", sid)
            if sid not in self.ws.archived_sessions:
                self.ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
                self.store.update(self.ws)
        self._refresh()

    def action_spawn(self):
        ok, err = launch_orch_claude(self.ws, store=self.store)
        if ok:
            self.app.notify("Session spawned", timeout=2)
            self._add_spawning_placeholder()
        else:
            self.app.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def _add_spawning_placeholder(self):
        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        olist.display = True
        no_sess.display = False
        frame = THROBBER_FRAMES[self._throbber_frame % len(THROBBER_FRAMES)]
        line1 = f" [bold {C_CYAN}]{frame}[/bold {C_CYAN}]  [bold]Starting session…[/bold]"
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
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

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
            yield Static(f"[{C_DIM}]Ctrl+S[/{C_DIM}] submit  [{C_DIM}]Esc[/{C_DIM}] cancel", id="brain-hint")

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
    ]

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
            yield Static(f"[{C_DIM}]Enter/y[/{C_DIM}] add all  [{C_DIM}]Esc/n[/{C_DIM}] cancel", id="brain-preview-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


# ─── Add Link Screen ────────────────────────────────────────────────

class AddLinkScreen(ModalScreen[Link | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

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
            yield Input(placeholder="Label (optional, defaults to kind)", id="addlink-label")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] add  [{C_DIM}]Esc[/{C_DIM}] cancel", id="addlink-hint")

    def on_mount(self):
        self.query_one("#addlink-value", Input).focus()

    @on(Input.Submitted, "#addlink-value")
    def on_value_submitted(self):
        self.query_one("#addlink-label", Input).focus()

    @on(Input.Submitted, "#addlink-label")
    def on_label_submitted(self):
        self._create()

    def _create(self):
        kind = self.query_one("#addlink-kind", Select).value
        value = self.query_one("#addlink-value", Input).value.strip()
        label = self.query_one("#addlink-label", Input).value.strip()
        if not value:
            self.app.notify("Value cannot be empty", severity="error", timeout=2)
            return
        if not label:
            label = kind
        self.dismiss(Link(kind=kind, label=label, value=value))

    def action_cancel(self):
        self.dismiss(None)


# ─── Link Session Screen ────────────────────────────────────────────

class LinkSessionScreen(_VimOptionListMixin, ModalScreen[Workstream | None]):
    """Select a workstream to link a session to."""

    _option_list_id = "linksession-list"
    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Link"),
    ] + _VimOptionListMixin.VIM_BINDINGS

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
                    f"{STATUS_ICONS[ws.status]} {ws.name}  ({ws.category.value})",
                    id=ws.id,
                ))
            if not options:
                options.append(Option("(no workstreams)", id="none", disabled=True))
            yield OptionList(*options, id="linksession-list")
            yield Static(f"[{C_DIM}]Enter[/{C_DIM}] link  [{C_DIM}]Esc[/{C_DIM}] cancel", id="linksession-hint")

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
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Resume"),
    ] + _VimOptionListMixin.VIM_BINDINGS

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
                f"[{C_DIM}]Enter[/{C_DIM}] resume  [{C_DIM}]Esc[/{C_DIM}] cancel",
                id="threadpick-hint",
            )

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._generate_titles()
        self._rebuild_options()
        self._throbber_timer = self.set_interval(0.3, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

    def _refresh_session_liveness(self):
        from actions import refresh_liveness
        refresh_liveness(self.picker_sessions)

    def _tick_throbber(self):
        self._throbber_frame += 1
        olist = self.query_one("#threadpick-list", OptionList)
        for i, s in enumerate(self.picker_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)

    @work(thread=True)
    def _generate_titles(self):
        from thread_namer import get_session_title, title_sessions
        untitled = [s for s in self.picker_sessions if not get_session_title(s)]
        if untitled:
            title_sessions(untitled)
            self.app.call_from_thread(self._rebuild_options)

    def _build_options(self) -> list[Option]:
        options = []
        for i, s in enumerate(self.picker_sessions):
            act = session_activity(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame)
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

class RepoPickerScreen(_VimOptionListMixin, ModalScreen[str | None]):
    """Pick a repo from known project paths."""

    _option_list_id = "repopick-list"
    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Select"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    RepoPickerScreen {{ align: center middle; }}
    #repopick-container {{
        width: 70; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #repopick-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #repopick-list {{ height: auto; max-height: 20; }}
    #repopick-list > .option-list--option-highlighted {{
        background: $primary 15%;
    }}
    #repopick-hint {{ text-align: center; color: {C_DIM}; padding-top: 1; }}
    """

    def __init__(self, repos: list[str]):
        super().__init__()
        self.repos = repos

    def compose(self) -> ComposeResult:
        with Vertical(id="repopick-container"):
            yield Label("Select repo:", id="repopick-title")
            options = []
            for repo in self.repos:
                name = Path(repo).name
                short = repo.replace(str(Path.home()), "~")
                options.append(Option(
                    f"[bold]{name}[/bold]  [{C_DIM}]{short}[/{C_DIM}]",
                    id=repo,
                ))
            if not options:
                options.append(Option("(no repos found)", id="none", disabled=True))
            yield OptionList(*options, id="repopick-list")
            yield Static(
                f"[{C_DIM}]Enter[/{C_DIM}] select  [{C_DIM}]Esc[/{C_DIM}] cancel",
                id="repopick-hint",
            )

    def action_confirm(self):
        option_list = self.query_one("#repopick-list", OptionList)
        idx = option_list.highlighted
        if idx is not None and idx < len(self.repos):
            self.dismiss(self.repos[idx])
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
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Select"),
    ] + _VimOptionListMixin.VIM_BINDINGS

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
                    f"{STATUS_ICONS[ws.status]} {ws.name}  [{C_DIM}]{ws.category.value}[/{C_DIM}]",
                    id=ws.id,
                ))
            options.append(Option(
                f"[{C_GREEN}]+ Create new workstream[/{C_GREEN}]",
                id=_SENTINEL_NEW,
            ))
            yield OptionList(*options, id="wspick-list")
            yield Static(
                f"[{C_DIM}]Enter[/{C_DIM}] select  [{C_DIM}]Esc[/{C_DIM}] cancel",
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
        Binding("n,escape,q", "deny", "No"),
    ]

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
            yield Static(f"[{C_DIM}]y[/{C_DIM}] yes  [{C_DIM}]n[/{C_DIM}] no", id="confirm-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_deny(self):
        self.dismiss(False)
