"""Claude Orchestrator TUI — central hub for managing workstreams and Claude sessions.

This is the thin Textual shell. Business logic lives in state.py,
modal screens in screens.py, rendering helpers in rendering.py,
and external process actions in actions.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Input,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from config import build_app_bindings
from models import (
    Category, Link, Origin, Status, Store, Workstream,
    STATUS_ICONS, _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, load_last_seen, mark_thread_seen, discover_threads
from thread_namer import apply_cached_names, name_uncached_threads, title_sessions, get_session_title
from watcher import SessionWatcher
from workstream_synthesizer import (
    synthesize_workstreams, get_discovered_workstreams, get_assigned_thread_ids,
    pin_workstream, dismiss_workstream,
)
from description_refresher import refresh_descriptions

# Import from new modules
from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_GREEN, C_ORANGE, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE, BG_SURFACE, BG_RAISED,
    STATUS_THEME, CATEGORY_THEME,
    LINK_TYPE_ICONS, LINK_ORDER, LINK_KINDS,
    ViewMode,
    _token_color, _token_color_markup, _colored_tokens,
    _status_markup, _category_markup, _link_icon,
    _ws_indicators, _short_project, _short_model, _worktree_styled,
    THROBBER_FRAMES, _ACTIVITY_PRIORITY,
    _activity_icon, _activity_badge, _best_activity,
    _render_session_option, _session_title,
    _rich_escape,
)
from state import AppState
from actions import (
    has_tmux, launch_orch_claude, ws_directories, ws_working_dir,
    find_sessions_for_ws, do_resume, resume_session_now, open_link,
    refresh_liveness,
)
from screens import (
    SessionsChanged,
    HelpScreen, QuickNoteScreen, TodoScreen, LinksScreen,
    AddScreen, DetailScreen, BrainDumpScreen, BrainPreviewScreen,
    AddLinkScreen, LinkSessionScreen, SessionPickerScreen, ConfirmScreen,
    RepoPickerScreen, WorkstreamPickerScreen, _SENTINEL_NEW,
)


# ─── Inline Inputs ──────────────────────────────────────────────────

class SearchInput(Input):
    BINDINGS = [Binding("escape", "cancel_search", "Cancel", priority=True)]

    def action_cancel_search(self):
        self.value = ""
        app = self.app
        app.state.search_text = ""
        app._refresh_ws_table()
        self.display = False
        app._active_table().focus()


class CommandInput(Input):
    BINDINGS = [Binding("escape", "cancel_command", "Cancel", priority=True)]

    def action_cancel_command(self):
        self.value = ""
        self.display = False
        self.app._active_table().focus()


class QuickNoteInput(Input):
    BINDINGS = [Binding("escape", "cancel_note", "Cancel", priority=True)]

    def action_cancel_note(self):
        self.value = ""
        self.display = False
        self.app._active_table().focus()


class RenameInput(Input):
    BINDINGS = [Binding("escape", "cancel_rename", "Cancel", priority=True)]

    def action_cancel_rename(self):
        self.value = ""
        self.display = False
        self.app._active_table().focus()


# ─── Main App ───────────────────────────────────────────────────────

class OrchestratorApp(App):
    """Claude Orchestrator — workstream & session dashboard."""

    CSS = f"""
    Screen {{
        background: {BG_BASE};
    }}
    #status-bar {{
        height: 1; padding: 0 1; background: {BG_BASE}; dock: top;
    }}
    #view-bar {{
        height: 1; padding: 0 1; background: {BG_BASE}; dock: top;
    }}
    #filter-bar {{
        height: 1; padding: 0 1; background: {BG_BASE}; dock: top;
    }}
    #summary-bar {{
        height: 1; padding: 0 1; background: {BG_BASE}; color: {C_DIM}; dock: bottom;
    }}
    #main-content {{ height: 1fr; }}
    DataTable {{ width: 3fr; }}
    #preview-pane {{
        width: 2fr; min-width: 36; border-left: blank;
        padding: 1 2; background: {BG_BASE};
    }}
    #preview-content {{ width: 100%; }}
    #preview-sessions {{
        height: auto; max-height: 16; width: 100%; margin: 0; padding: 0;
    }}
    #search-input, #command-input, #note-input, #rename-input {{
        dock: bottom; height: 1; display: none; border: none; background: {BG_BASE};
    }}
    #search-input:focus, #command-input:focus, #note-input:focus, #rename-input:focus {{
        border: none; background: {BG_BASE};
    }}
    """

    TITLE = "orchestrator"
    CSS_PATH = "orchestrator.tcss"
    theme = "mellow"

    BINDINGS = build_app_bindings()

    def __init__(self):
        super().__init__()
        self.register_theme(Theme(
            name="mellow",
            primary="#87afaf",
            secondary="#af87ff",
            background="#141414",
            surface="#1a1a1a",
            panel="#222222",
            foreground="#a0a0a0",
            accent="#5fd7ff",
            warning="#ffd75f",
            error="#d75f5f",
            success="#87d787",
            dark=True,
            luminosity_spread=0.08,
            text_alpha=0.85,
            variables={
                "scrollbar": "#333333",
                "scrollbar-hover": "#555555",
                "scrollbar-active": "#87afaf",
                "scrollbar-background": "#141414",
                "scrollbar-background-hover": "#1a1a1a",
                "scrollbar-background-active": "#1a1a1a",
                "scrollbar-corner-color": "#141414",
                "footer-background": "#141414",
                "footer-foreground": "#555555",
                "block-cursor-text-style": "bold",
                "border": "#333333",
                "border-blurred": "#282828",
                "input-cursor-background": "#87afaf",
                "input-cursor-foreground": "#141414",
                "input-selection-background": "#87afaf 30%",
            },
        ))
        self.theme = "mellow"
        self.state = AppState(Store())
        self._throbber_timer = None
        self._session_watcher: SessionWatcher | None = None

    async def on_event(self, event) -> None:
        """Override to log key events BEFORE Textual's binding check."""
        from textual.events import Key
        if isinstance(event, Key) and not event.is_forwarded:
            with open("/tmp/orch_keys.log", "a") as f:
                f.write(f"App.on_event: key={event.key!r} char={event.character!r}\n")
        await super().on_event(event)

    def on_key(self, event) -> None:
        # DEBUG: log keys that reach App.on_key (after bubbling)
        with open("/tmp/orch_keys.log", "a") as f:
            f.write(f"App.on_key: key={event.key!r} forwarded={event.is_forwarded}\n")
        if event.key in ("ctrl+j", "ctrl+k"):
            event.prevent_default()
            event.stop()
            screen = self.screen
            if event.key == "ctrl+j":
                if hasattr(screen, 'action_next_panel'):
                    screen.action_next_panel()
                else:
                    self.action_next_panel()
            else:
                if hasattr(screen, 'action_prev_panel'):
                    screen.action_prev_panel()
                else:
                    self.action_prev_panel()
        elif event.key == "ctrl+l":
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, 'action_go_forward'):
                screen.action_go_forward()
            elif hasattr(screen, 'action_select_item'):
                screen.action_select_item()
            else:
                self.action_select_item()

    # ── Convenience accessors for backward compat ──

    @property
    def store(self):
        return self.state.store

    @store.setter
    def store(self, value):
        self.state.store = value

    @property
    def view_mode(self):
        return self.state.view_mode

    @view_mode.setter
    def view_mode(self, value):
        self.state.view_mode = value

    @property
    def filter_mode(self):
        return self.state.filter_mode

    @filter_mode.setter
    def filter_mode(self, value):
        self.state.filter_mode = value

    @property
    def sort_mode(self):
        return self.state.sort_mode

    @property
    def search_text(self):
        return self.state.search_text

    @search_text.setter
    def search_text(self, value):
        self.state.search_text = value

    @property
    def sessions(self):
        return self.state.sessions

    @property
    def threads(self):
        return self.state.threads

    @property
    def discovered_ws(self):
        return self.state.discovered_ws

    @property
    def preview_visible(self):
        return self.state.preview_visible

    @preview_visible.setter
    def preview_visible(self, value):
        self.state.preview_visible = value

    # ── Compose ──

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        yield Static("", id="view-bar")
        yield Static("", id="filter-bar")
        with Horizontal(id="main-content"):
            yield DataTable(id="ws-table")
            yield DataTable(id="sessions-table")
            yield DataTable(id="archived-table")
            with VerticalScroll(id="preview-pane"):
                yield Static("", id="preview-content")
                yield OptionList(id="preview-sessions")
        yield SearchInput(placeholder="Search...", id="search-input")
        yield CommandInput(placeholder=":", id="command-input")
        yield QuickNoteInput(placeholder="note: ", id="note-input")
        yield RenameInput(placeholder="rename: ", id="rename-input")
        yield Static("", id="summary-bar")

    def on_mount(self):
        ws_table = self.query_one("#ws-table", DataTable)
        ws_table.cursor_type = "row"
        ws_table.zebra_stripes = False
        ws_table.add_columns("", "Name", "Worktree", "Sess", "Category", "Updated")

        sessions_table = self.query_one("#sessions-table", DataTable)
        sessions_table.cursor_type = "row"
        sessions_table.zebra_stripes = False
        sessions_table.add_columns("Title", "Workstream", "Model", "Tokens", "Age")
        sessions_table.display = False

        archived_table = self.query_one("#archived-table", DataTable)
        archived_table.cursor_type = "row"
        archived_table.zebra_stripes = False
        archived_table.add_columns("", "Name", "Worktree", "Sess", "Category", "Updated")
        archived_table.display = False

        self._refresh_ws_table()
        self._load_sessions()
        self._refresh_archived_table()
        self._update_all_bars()

        self.query_one("#preview-sessions", OptionList).display = False

        self._poll_tmux()
        self.set_interval(30, self._poll_tmux)

        self._session_watcher = SessionWatcher(
            on_change=lambda: self.call_from_thread(self._poll_sessions),
            debounce=1.0,
        )
        self._session_watcher.start()
        self.set_interval(10, self._poll_sessions)

        self._throbber_timer = self.set_interval(0.1, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

        ws_table.focus()

    def on_unmount(self):
        if self._session_watcher:
            self._session_watcher.stop()

    # ── Active table helper ──

    def _active_table(self) -> DataTable:
        if self.state.view_mode == ViewMode.SESSIONS:
            return self.query_one("#sessions-table", DataTable)
        elif self.state.view_mode == ViewMode.ARCHIVED:
            return self.query_one("#archived-table", DataTable)
        return self.query_one("#ws-table", DataTable)

    # ── View switching ──

    def action_next_view(self):
        self.state.next_view()
        self._apply_view()

    def action_prev_view(self):
        self.state.prev_view()
        self._apply_view()

    def _apply_view(self):
        ws_table = self.query_one("#ws-table", DataTable)
        sessions_table = self.query_one("#sessions-table", DataTable)
        archived_table = self.query_one("#archived-table", DataTable)
        filter_bar = self.query_one("#filter-bar", Static)

        ws_table.display = self.state.view_mode == ViewMode.WORKSTREAMS
        sessions_table.display = self.state.view_mode == ViewMode.SESSIONS
        archived_table.display = self.state.view_mode == ViewMode.ARCHIVED
        filter_bar.display = self.state.view_mode == ViewMode.WORKSTREAMS

        if self.state.view_mode == ViewMode.SESSIONS:
            self._load_sessions()

        self._active_table().focus()
        self._update_all_bars()
        self._update_preview()

    # ── Navigation ──

    def action_cursor_down(self):
        self._active_table().action_cursor_down()

    def action_cursor_up(self):
        self._active_table().action_cursor_up()

    def action_cursor_top(self):
        table = self._active_table()
        if table.row_count > 0:
            table.move_cursor(row=0)

    def action_cursor_bottom(self):
        table = self._active_table()
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)

    def action_half_page_down(self):
        table = self._active_table()
        page_size = max(1, (table.size.height - 2) // 2)
        for _ in range(page_size):
            table.action_cursor_down()

    def action_half_page_up(self):
        table = self._active_table()
        page_size = max(1, (table.size.height - 2) // 2)
        for _ in range(page_size):
            table.action_cursor_up()

    # ── Preview pane ──

    # ── Panel navigation (Ctrl+j/k) ──

    def _panel_cycle(self) -> list[str]:
        """Widget IDs for the current view's focusable panels."""
        table_id = {
            ViewMode.WORKSTREAMS: "ws-table",
            ViewMode.SESSIONS: "sessions-table",
            ViewMode.ARCHIVED: "archived-table",
        }.get(self.state.view_mode, "ws-table")
        panels = [table_id]
        if self.state.preview_visible:
            panels.append("preview-sessions")
        return panels

    def _current_panel_index(self) -> int:
        focused = self.focused
        focused_id = focused.id if focused else ""
        panels = self._panel_cycle()
        for i, pid in enumerate(panels):
            if focused_id == pid:
                return i
        return 0

    def action_next_panel(self):
        panels = self._panel_cycle()
        if not panels:
            return
        idx = (self._current_panel_index() + 1) % len(panels)
        try:
            self.query_one(f"#{panels[idx]}").focus()
        except Exception:
            pass

    def action_prev_panel(self):
        panels = self._panel_cycle()
        if not panels:
            return
        idx = (self._current_panel_index() - 1) % len(panels)
        try:
            self.query_one(f"#{panels[idx]}").focus()
        except Exception:
            pass

    def action_toggle_preview(self):
        pane = self.query_one("#preview-pane")
        self.state.preview_visible = not self.state.preview_visible
        pane.display = self.state.preview_visible

    def _refresh_session_liveness(self):
        changed = self.state.refresh_liveness()
        if changed:
            self._refresh_ws_table()
            if self.state.view_mode == ViewMode.SESSIONS:
                self._refresh_sessions_table()

    def _tick_throbber(self):
        self.state.throbber_frame += 1
        olist = self.query_one("#preview-sessions", OptionList)
        for i, s in enumerate(self.state.preview_sessions):
            act = session_activity(s, self.state.last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self.state.throbber_frame, title_width=35)
                olist.replace_option_prompt_at_index(i, prompt)

    def _update_preview(self):
        if not self.state.preview_visible:
            return
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
            self._render_ws_preview(ws)
        elif self.state.view_mode == ViewMode.SESSIONS:
            session = self._selected_session()
            self._render_session_preview(session)
        elif self.state.view_mode == ViewMode.ARCHIVED:
            ws = self._selected_archived()
            self._render_ws_preview(ws, archived=True)

    @staticmethod
    def _hint_line(pairs: list[tuple[str, str]]) -> str:
        parts = [f"[{C_YELLOW}]{key}[/{C_YELLOW}] {label}" for key, label in pairs]
        return f"[{C_DIM}]{' \u00b7 '.join(parts)}[/{C_DIM}]"

    def _nav_hints(self) -> str:
        return self._hint_line([("j/k", "navigate"), ("Tab", "views"), ("?", "help")])

    def _render_ws_preview(self, ws: Workstream | None, archived: bool = False):
        content = self.query_one("#preview-content", Static)
        olist = self.query_one("#preview-sessions", OptionList)
        if not ws:
            content.update(f"[{C_DIM}]Select a workstream[/{C_DIM}]\n\n{self._nav_hints()}")
            olist.display = False
            self.state.preview_sessions = []
            return

        lines = []
        lines.append(f"[bold {C_PURPLE}]{_rich_escape(ws.name)}[/bold {C_PURPLE}]")
        lines.append(f"{_status_markup(ws.status)}  {_category_markup(ws.category)}")
        if archived:
            lines.append(f"[{C_DIM}]Archived[/{C_DIM}]")
        lines.append("")

        if ws.description:
            lines.append(ws.description)
            lines.append("")

        ws_sessions = self.state.sessions_for_ws(ws)
        if ws_sessions:
            total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in ws_sessions)
            total_msgs = sum(s.message_count for s in ws_sessions)
            _tk = f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000 else str(total_tokens)
            last_active = ws_sessions[0].age

            lines.append(f"[bold {C_BLUE}]Activity[/bold {C_BLUE}]")
            lines.append(
                f"  [{C_CYAN}]{len(ws_sessions)}[/{C_CYAN}] sessions  "
                f"[{C_DIM}]\u00b7[/{C_DIM}]  {total_msgs} messages  "
                f"[{C_DIM}]\u00b7[/{C_DIM}]  {_token_color_markup(_tk, total_tokens)}"
            )
            lines.append(f"  [{C_DIM}]Last active[/{C_DIM}] {last_active}")
            lines.append("")

            archived_count = len(ws.archived_sessions)
            if archived_count:
                lines.append(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]  [{C_DIM}]({archived_count} archived)[/{C_DIM}]")
            else:
                lines.append(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]")
        else:
            lines.append(f"[{C_DIM}]No Claude sessions found[/{C_DIM}]")
            dirs = ws_directories(ws)
            if not dirs:
                lines.append(f"[{C_DIM}]Link a directory to auto-discover sessions[/{C_DIM}]")
            lines.append("")

        dirs = ws_directories(ws)
        if dirs:
            lines.append(f"[bold {C_BLUE}]Context[/bold {C_BLUE}]")
            for d in dirs:
                short = d.replace(str(Path.home()), "~")
                lines.append(f"  [{C_DIM}]{short}[/{C_DIM}]")
            lines.append("")

        if ws.notes:
            lines.append(f"[bold {C_BLUE}]Notes[/bold {C_BLUE}]")
            for line in ws.notes.split("\n")[:8]:
                lines.append(f"  {line}")
            if ws.notes.count("\n") > 8:
                lines.append(f"  [{C_DIM}]...[/{C_DIM}]")
            lines.append("")

        lines.append(f"[{C_DIM}]Created {_relative_time(ws.created_at)} \u00b7 Updated {_relative_time(ws.updated_at)}[/{C_DIM}]")

        lines.append("")
        if archived:
            lines.append(self._nav_hints())
        else:
            lines.append(self._hint_line([
                ("r", "resume"), ("c", "new session"), ("s", "status"),
                ("n", "note"), ("o", "open"),
            ]))

        content.update("\n".join(lines))

        self.state.preview_sessions = ws_sessions
        self.state.last_seen_cache = load_last_seen()
        if ws_sessions:
            olist.display = True
            self._refresh_preview_sessions()
        else:
            olist.display = False

    def _refresh_preview_sessions(self):
        olist = self.query_one("#preview-sessions", OptionList)
        highlighted = olist.highlighted
        olist.clear_options()
        for i, s in enumerate(self.state.preview_sessions):
            act = session_activity(s, self.state.last_seen_cache)
            olist.add_option(Option(
                _render_session_option(s, act, self.state.throbber_frame, title_width=35),
                id=str(i),
            ))
        if highlighted is not None and highlighted < len(self.state.preview_sessions):
            olist.highlighted = highlighted

    def _render_session_preview(self, session: ClaudeSession | None):
        self.query_one("#preview-sessions", OptionList).display = False
        self.state.preview_sessions = []
        content = self.query_one("#preview-content", Static)
        if not session:
            content.update(f"[{C_DIM}]No session selected[/{C_DIM}]\n\n{self._nav_hints()}")
            return

        lines = []
        lines.append(f"[bold {C_PURPLE}]{session.display_name}[/bold {C_PURPLE}]")
        if session.is_live:
            lines.append(f"[bold {C_GREEN}]\u25cf LIVE[/bold {C_GREEN}]")
        lines.append("")

        lines.append(f"[bold {C_BLUE}]Model[/bold {C_BLUE}]")
        lines.append(f"  {session.model or 'unknown'}")
        lines.append("")

        lines.append(f"[bold {C_BLUE}]Usage[/bold {C_BLUE}]")
        lines.append(f"  [{C_DIM}]Input[/{C_DIM}]    {session.total_input_tokens:,}")
        lines.append(f"  [{C_DIM}]Output[/{C_DIM}]   {session.total_output_tokens:,}")
        lines.append(f"  [{C_DIM}]Total[/{C_DIM}]    {session.tokens_display}")
        lines.append("")

        lines.append(f"[bold {C_BLUE}]Activity[/bold {C_BLUE}]")
        lines.append(f"  [{C_DIM}]Messages[/{C_DIM}]  {session.message_count}")
        lines.append(f"  [{C_DIM}]Last[/{C_DIM}]      {session.age}")
        lines.append("")

        lines.append(f"[bold {C_BLUE}]Project[/bold {C_BLUE}]")
        project = session.project_path
        if project.startswith(str(Path.home())):
            project = project.replace(str(Path.home()), "~")
        lines.append(f"  {project}")
        lines.append("")

        lines.append(f"[{C_DIM}]Session: {session.session_id[:16]}...[/{C_DIM}]")
        lines.append("")
        lines.append(self._hint_line([("r", "resume"), ("l", "link to workstream")]))

        content.update("\n".join(lines))

    @on(DataTable.RowHighlighted, "#ws-table")
    def on_ws_highlighted(self, event: DataTable.RowHighlighted):
        self._update_preview()

    @on(DataTable.RowHighlighted, "#sessions-table")
    def on_session_highlighted(self, event: DataTable.RowHighlighted):
        self._update_preview()

    @on(DataTable.RowHighlighted, "#archived-table")
    def on_archived_highlighted(self, event: DataTable.RowHighlighted):
        self._update_preview()

    # ── Bar rendering ──

    def _update_all_bars(self):
        try:
            self.query_one("#status-bar", Static).update(self._render_status_bar())
            self.query_one("#view-bar", Static).update(self._render_view_bar())
            self.query_one("#filter-bar", Static).update(self._render_filter_bar())
            self.query_one("#summary-bar", Static).update(self._render_summary_bar())
        except Exception:
            pass

    def _render_status_bar(self) -> str:
        total = len(self.state.store.active)
        in_prog = len([w for w in self.state.store.active if w.status == Status.IN_PROGRESS])
        blocked = len([w for w in self.state.store.active if w.status == Status.BLOCKED])
        review = len([w for w in self.state.store.active if w.status == Status.AWAITING_REVIEW])
        done = len([w for w in self.state.store.active if w.status == Status.DONE])
        stale = len(self.state.store.stale())

        parts = [
            f"[bold {C_BLUE}] ORCH [/bold {C_BLUE}]",
            f"[bold]{total}[/bold] streams",
            f"[{C_CYAN}]{STATUS_ICONS[Status.IN_PROGRESS]} {in_prog}[/{C_CYAN}]",
            f"[{C_RED}]{STATUS_ICONS[Status.BLOCKED]} {blocked}[/{C_RED}]",
            f"[{C_PURPLE}]{STATUS_ICONS[Status.AWAITING_REVIEW]} {review}[/{C_PURPLE}]",
            f"[{C_GREEN}]{STATUS_ICONS[Status.DONE]} {done}[/{C_GREEN}]",
        ]
        if stale:
            parts.append(f"[{C_DIM}]{stale} stale[/{C_DIM}]")

        if self.state.sessions:
            total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in self.state.sessions)
            if total_tokens > 0:
                _tk = f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000 else str(total_tokens)
                parts.append(f"[{C_DIM}]\u2502[/{C_DIM}]")
                parts.append(f"{_token_color_markup(_tk, total_tokens)}")

        return "  ".join(parts)

    def _render_view_bar(self) -> str:
        views = [
            (ViewMode.WORKSTREAMS, f"Workstreams ({len(self.state.store.active) + len(self.state.discovered_ws)})"),
            (ViewMode.SESSIONS, f"Sessions ({len(self.state.sessions)})"),
            (ViewMode.ARCHIVED, f"Archived ({len(self.state.store.archived)})"),
        ]
        parts = []
        for mode, label in views:
            if self.state.view_mode == mode:
                parts.append(f"[bold {C_CYAN}] \u25b8 {label} [/bold {C_CYAN}]")
            else:
                parts.append(f"[{C_DIM}]   {label} [/{C_DIM}]")
        return "".join(parts)

    def _render_filter_bar(self) -> str:
        filters = {
            "all": "1:All", "work": "2:Work", "personal": "3:Personal",
            "active": "4:Active", "stale": "5:Stale",
        }
        parts = []
        for key, label in filters.items():
            if self.state.filter_mode == key:
                parts.append(f"[bold {C_CYAN}] {label} [/bold {C_CYAN}]")
            else:
                parts.append(f"[{C_DIM}]{label}[/{C_DIM}]")

        sort_labels = {
            "status": "Status", "updated": "Updated", "created": "Created",
            "category": "Category", "name": "Name",
        }
        sort_label = sort_labels.get(self.state.sort_mode, self.state.sort_mode)
        parts.append(f"  [{C_DIM}]Sort:[/{C_DIM}][bold {C_BLUE}]{sort_label}[/bold {C_BLUE}]")

        if self.state.search_text:
            parts.append(f"  [{C_DIM}]Search:[/{C_DIM}][{C_YELLOW}]{_rich_escape(self.state.search_text)}[/{C_YELLOW}]")

        return " ".join(parts)

    def _render_summary_bar(self) -> str:
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            count = self._active_table().row_count
            return (
                f"  {count} workstreams  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]r[/{C_DIM}] resume  "
                f"[{C_DIM}]c[/{C_DIM}] new session  "
                f"[{C_DIM}]n[/{C_DIM}] note  "
                f"[{C_DIM}]s[/{C_DIM}] status  "
                f"[{C_DIM}]/[/{C_DIM}] search  "
                f"[{C_DIM}]?[/{C_DIM}] help  "
                f"[{C_DIM}]Tab[/{C_DIM}] views"
            )
        elif self.state.view_mode == ViewMode.SESSIONS:
            count = len(self.state.sessions)
            return (
                f"  {count} sessions  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]r[/{C_DIM}] resume  "
                f"[{C_DIM}]l[/{C_DIM}] link to workstream  "
                f"[{C_DIM}]Tab[/{C_DIM}] views  "
                f"[{C_DIM}]R[/{C_DIM}] refresh"
            )
        else:
            count = len(self.state.store.archived)
            return (
                f"  {count} archived  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]u[/{C_DIM}] unarchive  "
                f"[{C_DIM}]d[/{C_DIM}] delete  "
                f"[{C_DIM}]Tab[/{C_DIM}] views"
            )

    # ── Workstreams table ──

    def _refresh_ws_table(self):
        try:
            table = self.query_one("#ws-table", DataTable)
        except Exception:
            return
        old_key = self._get_cursor_key(table)
        table.clear()

        items = self.state.get_unified_items()
        last_seen = load_last_seen()

        for ws in items:
            is_discovered = ws.origin == Origin.DISCOVERED

            if is_discovered:
                ws_sessions = self.state.sessions_for_ws(ws)
                best = _best_activity(ws_sessions, last_seen)
                _ACTIVITY_ICONS = {
                    ThreadActivity.THINKING: ("◉", C_CYAN),
                    ThreadActivity.AWAITING_INPUT: ("◉", C_YELLOW),
                    ThreadActivity.RESPONSE_FRESH: ("●", C_GREEN),
                    ThreadActivity.RESPONSE_READY: ("●", C_ORANGE),
                    ThreadActivity.IDLE: ("·", C_DIM),
                }
                icon, color = _ACTIVITY_ICONS[best]
                status_cell = Text(icon, style=color)
            else:
                status_cell = Text(STATUS_ICONS[ws.status], style=STATUS_THEME[ws.status])

            indicators = ""
            if not is_discovered:
                indicators = _ws_indicators(ws, tmux_check=self.state.ws_has_tmux)
            ws_sessions = self.state.sessions_for_ws(ws)

            name_str = _rich_escape(ws.name)
            if indicators:
                name_str += "  " + indicators
            name_cell = Text.from_markup(name_str)

            wt_text, wt_color = _worktree_styled(ws)
            repo_cell = Text(wt_text, style=wt_color or C_DIM)

            sess_count = len(ws_sessions) if ws_sessions else 0
            sess_cell = Text(str(sess_count) if sess_count else "", style=C_DIM)

            cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
            updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)

            table.add_row(status_cell, name_cell, repo_cell, sess_cell, cat_cell, updated_cell, key=ws.id)

        self._restore_cursor(table, old_key)
        self._update_all_bars()
        self._update_preview()

    def _selected_ws(self) -> Workstream | None:
        try:
            table = self.query_one("#ws-table", DataTable)
        except Exception:
            return None
        key = self._get_cursor_key(table)
        if not key:
            return None
        return self.state.get_ws(key)

    def _sessions_for_ws(self, ws: Workstream, include_archived_sessions: bool = False) -> list[ClaudeSession]:
        """Delegate to state — kept for backward compat with DetailScreen."""
        return self.state.sessions_for_ws(ws, include_archived_sessions)

    # ── Sessions table ──

    def _load_sessions(self):
        self._do_load_sessions()

    def _poll_sessions(self):
        self._do_poll_sessions()

    @work(thread=True, exclusive=True, group="poll_sessions")
    def _do_poll_sessions(self):
        threads = discover_threads()
        apply_cached_names(threads)

        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)

        def _fingerprint(sl):
            return {(s.session_id, s.is_live, s.last_message_role, s.last_activity) for s in sl}
        old_ids = {s.session_id for s in self.state.sessions}
        new_ids = {s.session_id for s in sessions}
        if _fingerprint(self.state.sessions) == _fingerprint(sessions):
            return

        discovered = get_discovered_workstreams(threads)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

        if new_ids - old_ids:
            self._do_load_sessions()

    @work(thread=True, exclusive=True, group="sessions")
    def _do_load_sessions(self):
        threads = discover_threads()
        apply_cached_names(threads)

        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)

        discovered = get_discovered_workstreams(threads)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

        named = name_uncached_threads(threads)
        if named > 0:
            apply_cached_names(threads)

        new_count = synthesize_workstreams(threads, self.state.store.active)
        if new_count > 0 or named > 0:
            discovered = get_discovered_workstreams(threads)
            self.call_from_thread(self._apply_synthesis, threads, discovered)

        # Lightweight description re-evaluation (rate-limited internally to 6h per ws)
        desc_updated = refresh_descriptions(self.state.store, sessions)
        if desc_updated > 0:
            self.call_from_thread(self._refresh_ws_table)

    def _apply_sessions(self, sessions: list[ClaudeSession],
                        threads: list[Thread], discovered: list[Workstream]):
        self.state.update_sessions(sessions, threads, discovered)
        self._refresh_ws_table()
        self._refresh_sessions_table()
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _apply_synthesis(self, threads: list[Thread], discovered: list[Workstream]):
        self.state.threads = threads
        self.state.discovered_ws = discovered
        self._refresh_ws_table()

    def _refresh_sessions_table(self):
        table = self.query_one("#sessions-table", DataTable)
        old_key = self._get_cursor_key(table)
        table.clear()

        ws_lookup: dict[str, str] = {}
        for ws in self.state.store.active:
            ws_sessions = find_sessions_for_ws(ws, self.state.sessions)
            for s in ws_sessions:
                if s.session_id not in ws_lookup:
                    ws_lookup[s.session_id] = ws.name

        for session in self.state.sessions:
            live_prefix = "\u25cf " if session.is_live else "  "
            title_text = live_prefix + session.display_name
            title_style = C_GREEN if session.is_live else ""
            title_cell = Text(title_text, style=title_style)

            linked_ws = ws_lookup.get(session.session_id)
            if linked_ws:
                ws_cell = Text(linked_ws, style=C_CYAN)
            else:
                ws_cell = Text(_short_project(session.project_path), style=C_DIM)

            model_cell = Text(_short_model(session.model), style=C_DIM)
            tokens_cell = Text(session.tokens_display, style=_token_color(session.total_input_tokens + session.total_output_tokens))
            age_cell = Text(session.age, style=C_DIM)

            table.add_row(title_cell, ws_cell, model_cell, tokens_cell, age_cell,
                          key=session.session_id)

        self._restore_cursor(table, old_key)
        self._update_all_bars()

    def _selected_session(self) -> ClaudeSession | None:
        try:
            table = self.query_one("#sessions-table", DataTable)
        except Exception:
            return None
        key = self._get_cursor_key(table)
        if key:
            return self.state.get_session(key)
        return None

    # ── Archived table ──

    def _refresh_archived_table(self):
        table = self.query_one("#archived-table", DataTable)
        old_key = self._get_cursor_key(table)
        table.clear()

        for ws in self.state.store.archived:
            status_cell = Text(STATUS_ICONS[ws.status], style=STATUS_THEME[ws.status])
            name_cell = Text(ws.name)
            wt_text, wt_color = _worktree_styled(ws)
            repo_cell = Text(wt_text, style=wt_color or C_DIM)
            sess_cell = Text("", style=C_DIM)
            cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
            updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)
            table.add_row(status_cell, name_cell, repo_cell, sess_cell, cat_cell, updated_cell, key=ws.id)

        self._restore_cursor(table, old_key)

    def _selected_archived(self) -> Workstream | None:
        try:
            table = self.query_one("#archived-table", DataTable)
        except Exception:
            return None
        key = self._get_cursor_key(table)
        if key:
            return self.state.get_archived(key)
        return None

    # ── Cursor helpers ──

    def _get_cursor_key(self, table: DataTable) -> str | None:
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return str(row_key.value)
        except Exception:
            return None

    def _restore_cursor(self, table: DataTable, old_key: str | None):
        if old_key:
            for i, row_key in enumerate(table.rows):
                if str(row_key.value) == old_key:
                    table.move_cursor(row=i)
                    return

    # ── Primary action (Enter) ──

    def action_select_item(self):
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            self._open_detail()
        elif self.state.view_mode == ViewMode.SESSIONS:
            self._resume_session()
        elif self.state.view_mode == ViewMode.ARCHIVED:
            self._open_archived_detail()

    @on(DataTable.RowSelected, "#ws-table")
    def on_ws_row_selected(self, event: DataTable.RowSelected):
        self._open_detail()

    @on(DataTable.RowSelected, "#sessions-table")
    def on_session_row_selected(self, event: DataTable.RowSelected):
        self._resume_session()

    @on(DataTable.RowSelected, "#archived-table")
    def on_archived_row_selected(self, event: DataTable.RowSelected):
        self._open_archived_detail()

    @on(OptionList.OptionSelected, "#preview-sessions")
    def on_preview_session_selected(self, event: OptionList.OptionSelected):
        idx = int(event.option_id)
        if idx < len(self.state.preview_sessions):
            session = self.state.preview_sessions[idx]
            mark_thread_seen(session.session_id)
            ws = self._selected_ws()
            if ws:
                dirs = ws_directories(ws)
                resume_session_now(ws, session, dirs, self)
            else:
                self._suspend_claude(
                    ["claude", "--resume", session.session_id],
                    cwd=session.project_path,
                )

    def _open_detail(self):
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                DetailScreen(ws, self.state.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def _open_archived_detail(self):
        ws = self._selected_archived()
        if ws:
            self.push_screen(
                DetailScreen(ws, self.state.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    # ── Workstream actions ──

    def action_add(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return

        def on_result(ws: Workstream | None):
            if ws:
                self.state.store.add(ws)
                self.notify(f"Created: {ws.name}", timeout=2)
            self._refresh_ws_table()

        self.push_screen(AddScreen(), callback=on_result)

    def action_cycle_status(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            ws = self.state.cycle_status(ws.id)
            if ws:
                self._refresh_ws_table()
                self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_cycle_status_back(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            ws = self.state.cycle_status(ws.id, forward=False)
            if ws:
                self._refresh_ws_table()
                self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_quick_note(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            return

        def on_note(text: str | None):
            if not text or not text.strip():
                return
            self.state.add_todo(ws.id, text)
            self._refresh_ws_table()
            self.notify("Todo added", timeout=1)

        self.push_screen(QuickNoteScreen(ws), callback=on_note)

    @on(Input.Submitted, "#note-input")
    def on_note_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        note_input = self.query_one("#note-input", QuickNoteInput)
        note_input.display = False
        self._active_table().focus()
        if text:
            ws = self._selected_ws()
            if ws:
                self.state.add_todo(ws.id, text)
                self._refresh_ws_table()
                self.notify("Todo added", timeout=1)

    def action_rename(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            return
        self.query_one("#search-input").display = False
        self.query_one("#command-input").display = False
        self.query_one("#note-input").display = False
        rename_input = self.query_one("#rename-input", RenameInput)
        rename_input.display = True
        rename_input.value = ws.name
        rename_input.focus()

    @on(Input.Submitted, "#rename-input")
    def on_rename_submitted(self, event: Input.Submitted):
        new_name = event.value.strip()
        rename_input = self.query_one("#rename-input", RenameInput)
        rename_input.display = False
        self._active_table().focus()
        if new_name:
            ws = self._selected_ws()
            if ws:
                self.state.rename(ws.id, new_name)
                self._refresh_ws_table()
                self.notify(f"Renamed to: {new_name}", timeout=1)

    def action_edit_notes(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                TodoScreen(ws, self.state.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def action_open_links(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            return
        if ws.links:
            if len(ws.links) == 1:
                open_link(ws.links[0], ws=ws, app=self)
                self.notify(f"Opening {ws.links[0].label}...", timeout=2)
            else:
                self.push_screen(LinksScreen(ws, self.state.store))
        else:
            self.notify("No links", timeout=1)

    def action_toggle_archive(self):
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
            if ws:
                name = self.state.archive(ws.id)
                if name:
                    self.notify(f"Archived: {name}", timeout=2)
                    self._refresh_ws_table()
                    self._refresh_archived_table()
        elif self.state.view_mode == ViewMode.ARCHIVED:
            ws = self._selected_archived()
            if ws:
                name = self.state.unarchive(ws.id)
                if name:
                    self.notify(f"Restored: {name}", timeout=2)
                    self._refresh_ws_table()
                    self._refresh_archived_table()

    # Keep legacy action names so DetailScreen and other callers still work
    def action_archive(self):
        self.action_toggle_archive()

    def action_unarchive(self):
        self.action_toggle_archive()

    def action_delete_item(self):
        if self.state.view_mode == ViewMode.SESSIONS:
            return

        ws = None
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
        elif self.state.view_mode == ViewMode.ARCHIVED:
            ws = self._selected_archived()

        if ws:
            def on_confirm(confirmed: bool):
                if confirmed:
                    self.state.delete(ws.id)
                    self.notify(f"Deleted: {ws.name}", timeout=2)
                    self._refresh_ws_table()
                    self._refresh_archived_table()

            self.push_screen(
                ConfirmScreen(f"[bold {C_RED}]Delete[/bold {C_RED}] [bold]{_rich_escape(ws.name)}[/bold]?"),
                callback=on_confirm,
            )

    # ── Brain dump ──

    def action_brain_dump(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return

        def on_text(text: str | None):
            if text is None:
                return
            self._do_brain(text)

        self.push_screen(BrainDumpScreen(), callback=on_text)

    def _do_brain(self, text: str):
        from brain import parse_brain_dump

        tasks = parse_brain_dump(text)
        if not tasks:
            self.notify("No tasks found in input", severity="warning", timeout=2)
            return

        def on_confirm(confirmed: bool):
            if confirmed:
                for task in tasks:
                    ws = Workstream(
                        name=task.name,
                        description=task.raw_text,
                        category=task.category,
                        status=task.status,
                    )
                    self.state.store.add(ws)
                self.notify(f"Added {len(tasks)} workstreams", timeout=2)
                self._refresh_ws_table()

        self.push_screen(BrainPreviewScreen(tasks), callback=on_confirm)

    # ── Spawn & resume ──

    def action_spawn(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        ok, err = launch_orch_claude(ws, store=self.state.store)
        if ok:
            self.notify("Session spawned", timeout=2)
        else:
            self.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def action_repo_spawn(self):
        repos = self.state.known_repos()
        if not repos:
            self.notify("No repos found in session history", timeout=2)
            return

        def on_repo_picked(repo_path: str | None):
            if not repo_path:
                return
            matches = self.state.workstreams_for_repo(repo_path)
            if len(matches) == 0:
                ws = self.state.create_ws_for_repo(repo_path)
                self._spawn_in_ws(ws)
            elif len(matches) == 1:
                self._spawn_in_ws(matches[0])
            else:
                def on_ws_picked(result):
                    if result is None:
                        return
                    if result == _SENTINEL_NEW:
                        ws = self.state.create_ws_for_repo(repo_path)
                        self._spawn_in_ws(ws)
                    else:
                        self._spawn_in_ws(result)
                self.push_screen(
                    WorkstreamPickerScreen(matches, repo_path),
                    callback=on_ws_picked,
                )

        self.push_screen(RepoPickerScreen(repos), callback=on_repo_picked)

    def _spawn_in_ws(self, ws: Workstream):
        ok, err = launch_orch_claude(ws, store=self.state.store)
        if ok:
            self.notify(f"Spawned in {ws.name}", timeout=2)
            self._refresh_ws_table()
        else:
            self.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def action_resume(self):
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
            if ws:
                do_resume(ws, self, self.state.sessions,
                          sessions_for_ws_fn=lambda w: self.state.sessions_for_ws(w))
        elif self.state.view_mode == ViewMode.SESSIONS:
            self._resume_session()

    def _resume_session(self):
        session = self._selected_session()
        if not session:
            self.notify("No session selected", timeout=2)
            return

        ws = self.state.find_ws_for_session(session)
        if ws:
            launch_orch_claude(ws, session_id=session.session_id, cwd=session.project_path)
        else:
            self._suspend_claude(
                ["claude", "--resume", session.session_id],
                cwd=session.project_path,
            )

    def _suspend_claude(self, cmd: list[str], cwd: str | None = None):
        with self.suspend():
            subprocess.run(cmd, cwd=cwd)

    def _find_ws_for_session(self, session: ClaudeSession) -> Workstream | None:
        """Backward compat — delegate to state."""
        return self.state.find_ws_for_session(session)

    # ── Link action ──

    def action_link_action(self):
        if self.state.view_mode == ViewMode.WORKSTREAMS:
            self._add_link_to_ws()
        elif self.state.view_mode == ViewMode.SESSIONS:
            self._link_session_to_ws()

    def _add_link_to_ws(self):
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return

        def on_link(link: Link | None):
            if link:
                self.state.add_link(ws.id, link)
                self._refresh_ws_table()
                self.notify(f"Added {link.kind} link to {ws.name}", timeout=2)

        self.push_screen(AddLinkScreen(ws.name), callback=on_link)

    def _link_session_to_ws(self):
        session = self._selected_session()
        if not session:
            self.notify("No session selected", timeout=2)
            return

        def on_ws(ws: Workstream | None):
            if ws:
                ws.add_link(
                    kind="claude-session",
                    value=session.session_id,
                    label=session.display_name,
                )
                self.state.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"Linked session to {ws.name}", timeout=2)

        self.push_screen(LinkSessionScreen(self.state.store, session), callback=on_ws)

    # ── Filter & sort ──

    def action_filter(self, mode: str):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        self.state.set_filter(mode)
        self._refresh_ws_table()

    def action_sort(self, mode: str):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        self.state.set_sort(mode)
        self._refresh_ws_table()

    def action_search(self):
        if self.state.view_mode != ViewMode.WORKSTREAMS:
            return
        self.query_one("#command-input", CommandInput).display = False
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = True
        search_input.value = self.state.search_text
        search_input.focus()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted):
        self.state.set_search(event.value.strip())
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = False
        self._refresh_ws_table()
        self._active_table().focus()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed):
        self.state.set_search(event.value.strip())
        self._refresh_ws_table()

    # ── Command palette ──

    def action_command_palette(self):
        self.query_one("#search-input", SearchInput).display = False
        cmd_input = self.query_one("#command-input", CommandInput)
        cmd_input.display = True
        cmd_input.value = ""
        cmd_input.focus()

    @on(Input.Submitted, "#command-input")
    def on_command_submitted(self, event: Input.Submitted):
        cmd_text = event.value.strip()
        cmd_input = self.query_one("#command-input", CommandInput)
        cmd_input.display = False
        self._active_table().focus()
        if cmd_text:
            self._execute_command(cmd_text)

    def _execute_command(self, cmd_text: str):
        ws = self._selected_ws() if self.state.view_mode == ViewMode.WORKSTREAMS else None
        result = self.state.execute_command(cmd_text, ws.id if ws else None)

        action = result.get("action", "noop")
        msg = result.get("msg", "")

        if action == "view":
            self._apply_view()
        elif action == "refresh":
            self._refresh_ws_table()
            self._refresh_archived_table()
            if msg:
                self.notify(msg, timeout=2)
        elif action == "notify":
            self.notify(msg, timeout=2)
        elif action == "error":
            self.notify(msg, severity="error", timeout=2)
        elif action == "spawn":
            self.action_spawn()
        elif action == "resume":
            self.action_resume()
        elif action == "export":
            output, count = self.state.do_export(result.get("path", ""))
            self.notify(f"Exported {count} workstreams to {output}", timeout=3)
        elif action == "brain":
            text = result.get("text", "")
            if text:
                self._do_brain(text)
            else:
                self.action_brain_dump()
        elif action == "help":
            self.push_screen(HelpScreen())
        elif action == "delete":
            self.action_delete_item()
        elif action == "unarchive":
            self.action_unarchive()

    # ── Tmux polling ──

    def _poll_tmux(self):
        self._do_tmux_check()

    @work(thread=True, exclusive=True, group="tmux")
    def _do_tmux_check(self):
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-a", "-F",
                 "#{window_name}\t#{pane_current_path}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return
            paths: set[str] = set()
            names: set[str] = set()
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if "\t" in line:
                    name, path = line.split("\t", 1)
                    names.add(name)
                    paths.add(path.rstrip("/"))
                else:
                    names.add(line.strip())
            self.call_from_thread(self._apply_tmux_status, paths, names)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    def _apply_tmux_status(self, paths: set[str], names: set[str]):
        if self.state.update_tmux_status(paths, names):
            self._refresh_ws_table()

    # ── Other ──

    def action_refresh(self):
        self.state.store.load()
        self._refresh_ws_table()
        self._refresh_archived_table()
        self._load_sessions()
        self._poll_tmux()
        self.notify("Refreshed", timeout=1)

    def action_help(self):
        self.push_screen(HelpScreen())

    def _on_return_from_modal(self):
        self.state.store.load()
        self._refresh_ws_table()
        self._refresh_archived_table()
        self._update_preview()


if __name__ == "__main__":
    OrchestratorApp().run()
