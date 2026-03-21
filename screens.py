"""Modal screens — all ModalScreen subclasses for the orchestrator TUI.

Each screen is self-contained. They receive data via constructor params
and return results via dismiss(). No direct access to app state.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

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
    Category, Link, Status, Store, Workstream,
    STATUS_ICONS, _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, load_last_seen, mark_thread_seen
from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_GREEN, C_ORANGE, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE,
    STATUS_THEME, CATEGORY_THEME,
    LINK_TYPE_ICONS, LINK_ORDER, LINK_KINDS,
    THROBBER_FRAMES,
    _status_markup, _category_markup, _link_icon,
    _activity_icon, _activity_badge,
    _colored_tokens, _token_color_markup,
    _short_model, _short_project,
    _render_session_option, _session_title,
)
from actions import (
    launch_orch_claude, ws_directories, resume_session_now, open_link,
)


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

[bold {C_CYAN}]Actions (Threads)[/bold {C_CYAN}]
  [{C_YELLOW}]a[/{C_YELLOW}]   Add new thread
  [{C_YELLOW}]b[/{C_YELLOW}]   Brain dump (multi-line)
  [{C_YELLOW}]n[/{C_YELLOW}]   Quick note (inline)
  [{C_YELLOW}]s/S[/{C_YELLOW}] Cycle status forward / backward
  [{C_YELLOW}]c[/{C_YELLOW}]   New Claude session (with context)
  [{C_YELLOW}]r[/{C_YELLOW}]   Resume most recent session
  [{C_YELLOW}]l[/{C_YELLOW}]   Add link
  [{C_YELLOW}]e[/{C_YELLOW}]   Edit notes (full editor)
  [{C_YELLOW}]E[/{C_YELLOW}]   Rename thread
  [{C_YELLOW}]o[/{C_YELLOW}]   Open links
  [{C_YELLOW}]x[/{C_YELLOW}]   Archive
  [{C_YELLOW}]d[/{C_YELLOW}]   Delete

[bold {C_CYAN}]Inside Claude Session[/bold {C_CYAN}]
  [{C_YELLOW}]Ctrl+D[/{C_YELLOW}]  Clean exit (returns to orch)
  [{C_YELLOW}]/exit[/{C_YELLOW}]   Clean exit (alternative)
  [{C_DIM}]Session auto-links to thread on exit[/{C_DIM}]
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


# ─── Notes Screen ───────────────────────────────────────────────────

class NotesScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "save_and_close", "Save & back", priority=True)]

    DEFAULT_CSS = f"""
    NotesScreen {{ align: center middle; }}
    #notes-container {{
        width: 80; height: auto; max-height: 80%;
        padding: 1 2; background: {BG_BASE}; border: round $primary 30%;
    }}
    #notes-title {{ text-style: bold; color: {C_PURPLE}; padding-bottom: 1; }}
    #notes-editor {{ height: 20; margin: 0 0 1 0; }}
    #notes-hint {{ text-align: center; color: {C_DIM}; }}
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="notes-container"):
            yield Label(f"Notes: {self.ws.name}", id="notes-title")
            yield TextArea(self.ws.notes or "", id="notes-editor")
            yield Static(f"[{C_DIM}]Esc[/{C_DIM}] save & back", id="notes-hint")

    def action_save_and_close(self):
        editor = self.query_one("#notes-editor", TextArea)
        self.ws.notes = editor.text
        self.store.update(self.ws)
        self.dismiss()


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
            yield Label("New Thread", id="add-title")
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
        Binding("n", "quick_note", "Note"),
        Binding("L", "add_link", "Link+"),
        Binding("e", "edit_notes", "Edit notes"),
        Binding("o", "open_links", "Open links"),
        Binding("x", "archive", "Archive"),
        Binding("a", "archive_thread", "Archive/restore", priority=True),
        Binding("h", "focus_sessions", show=False, priority=True),
        Binding("l", "focus_archived", show=False, priority=True),
        Binding("enter", "enter_session", "Enter session", show=False),
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
    #detail-scroll {{
        height: 1fr;
        border-top: blank;
    }}
    #detail-body {{
        padding: 1 3;
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
        self._throbber_frame: int = 0
        self._last_seen_cache: dict[str, str] = {}
        self._active_pane: str = "sessions"
        self._animating_sessions: list[tuple[int, ThreadActivity]] = []
        self._animating_archived: list[tuple[int, ThreadActivity]] = []

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

            with VerticalScroll(id="detail-scroll"):
                yield Static(self._render_body(), id="detail-body")

            yield Static(self._render_help(), id="detail-help")

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._load_detail_sessions()
        self.query_one("#detail-sessions", OptionList).focus()
        self._throbber_timer = self.set_interval(0.3, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

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

    def _update_pane_labels(self):
        sess_label = self.query_one("#detail-sessions-label", Static)
        arch_label = self.query_one("#detail-archived-label", Static)
        n_active = len(self._detail_sessions)
        n_archived = len(self._archived_sessions)
        if self._active_pane == "sessions":
            sess_label.update(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}] [{C_DIM}]({n_active})[/{C_DIM}]")
            arch_label.update(f"[{C_DIM}]Archived ({n_archived})[/{C_DIM}]")
        else:
            sess_label.update(f"[{C_DIM}]Sessions ({n_active})[/{C_DIM}]")
            arch_label.update(f"[bold {C_BLUE}]Archived[/bold {C_BLUE}] [{C_DIM}]({n_archived})[/{C_DIM}]")

    def _refresh_session_liveness(self):
        from actions import refresh_liveness
        refresh_liveness(self._detail_sessions)
        refresh_liveness(self._archived_sessions)
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

    def _tick_throbber(self):
        self._throbber_frame += 1
        # Only update options that are actually animating (cached from last build)
        olist = self.query_one("#detail-sessions", OptionList)
        for i, act in self._animating_sessions:
            if i < olist.option_count:
                prompt = _render_session_option(self._detail_sessions[i], act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)
        arch_olist = self.query_one("#detail-archived", OptionList)
        for i, act in self._animating_archived:
            if i < arch_olist.option_count:
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
            all_sessions = app.state.sessions_for_ws(self.ws, include_archived_threads=True)
            archived = self.ws.archived_sessions
            revived = set()
            for s in all_sessions:
                if s.session_id in archived:
                    archived_at = archived[s.session_id]
                    last_act = s.last_activity or ""
                    if last_act and archived_at and self._parse_ts(last_act) > self._parse_ts(archived_at):
                        revived.add(s.session_id)
            if revived:
                for sid in revived:
                    del self.ws.archived_sessions[sid]
                self.store.update(self.ws)
            hidden = set(self.ws.archived_sessions)
            self._detail_sessions = [s for s in all_sessions if s.session_id not in hidden]
            self._archived_sessions = [s for s in all_sessions if s.session_id in hidden]
        else:
            from actions import find_sessions_for_ws
            self._detail_sessions = find_sessions_for_ws(self.ws, getattr(app, 'sessions', []))
            self._archived_sessions = []

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
            olist.add_option(Option(prompt, id=s.session_id))
        self._animating_sessions = animating

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

    def _find_session_by_id(self, sid: str) -> ClaudeSession | None:
        for s in self._detail_sessions:
            if s.session_id == sid:
                return s
        for s in self._archived_sessions:
            if s.session_id == sid:
                return s
        return None

    def action_enter_session(self):
        """Screen-level Enter handler — works even when OptionList lacks focus."""
        olist = self._focused_olist()
        if olist.highlighted is not None and olist.option_count > 0:
            try:
                opt = olist.get_option_at_index(olist.highlighted)
                sid = opt.id.removeprefix("a:")
                session = self._find_session_by_id(sid)
                if session:
                    mark_thread_seen(session.session_id)
                    dirs = ws_directories(self.ws)
                    resume_session_now(self.ws, session, dirs, self.app)
                    return
            except Exception:
                pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        oid = event.option_id
        sid = oid.removeprefix("a:")  # archived IDs are "a:<session_id>"
        session = self._find_session_by_id(sid)
        if session:
            mark_thread_seen(session.session_id)
            dirs = ws_directories(self.ws)
            resume_session_now(self.ws, session, dirs, self.app)

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
                f"{_token_color_markup(_tk, total_tok)} tok[/{C_DIM}]"
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
        if self.ws.notes:
            lines.append(f"[bold {C_BLUE}]Notes[/bold {C_BLUE}]")
            for line in self.ws.notes.split("\n"):
                lines.append(f"  {line}")
            lines.append("")
        lines.append(
            f"[{C_DIM}]Created {_relative_time(self.ws.created_at)} \u00b7 "
            f"Updated {_relative_time(self.ws.updated_at)}[/{C_DIM}]"
        )
        return "\n".join(lines)

    def _render_help(self) -> str:
        pairs = [
            ("Enter", "resume"), ("s/S", "status"), ("c", "spawn"),
            ("n", "note"), ("e", "edit"),
            ("o", "open"), ("x", "archive ws"),
            ("a", "archive/restore"), ("h/l", "panes"),
            ("q", "back"),
        ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    def _refresh(self):
        self.query_one("#detail-title", Static).update(self._render_title())
        self.query_one("#detail-meta", Static).update(self._render_meta())
        self.query_one("#detail-body", Static).update(self._render_body())
        self._load_detail_sessions()

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
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"[{timestamp}] {text.strip()}"
            self.ws.notes = (self.ws.notes + "\n" + entry) if self.ws.notes else entry
            self.store.update(self.ws)
            self._refresh()
            self.app.notify("Note added", timeout=1)
        self.app.push_screen(QuickNoteScreen(self.ws), callback=on_note)

    def action_edit_notes(self):
        def on_notes_close(_):
            self.store.load()
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()
        self.app.push_screen(NotesScreen(self.ws, self.store), callback=on_notes_close)

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

    def action_archive_thread(self):
        olist = self._focused_olist()
        idx = olist.highlighted
        if self._active_pane == "archived":
            if idx is None or idx >= len(self._archived_sessions):
                return
            sid = self._archived_sessions[idx].session_id
            self.ws.archived_sessions.pop(sid, None)
            self.store.update(self.ws)
        else:
            if idx is None or idx >= len(self._detail_sessions):
                return
            sid = self._detail_sessions[idx].session_id
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
        from actions import do_resume
        do_resume(self.ws, self.app,
                  getattr(self.app, 'sessions', getattr(getattr(self.app, 'state', None), 'sessions', [])),
                  sessions_for_ws_fn=lambda ws: getattr(self.app, 'state', self.app).sessions_for_ws(ws) if hasattr(getattr(self.app, 'state', self.app), 'sessions_for_ws') else [])

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


# ─── Thread Picker Screen ────────────────────────────────────────────

class ThreadPickerScreen(_VimOptionListMixin, ModalScreen[ClaudeSession | None]):
    """Pick a thread to resume from a workstream's matching sessions."""

    _option_list_id = "threadpick-list"
    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "confirm", "Resume"),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    ThreadPickerScreen {{ align: center middle; }}
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
        self.thread_sessions = sessions
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
        refresh_liveness(self.thread_sessions)

    def _tick_throbber(self):
        self._throbber_frame += 1
        olist = self.query_one("#threadpick-list", OptionList)
        for i, s in enumerate(self.thread_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)

    @work(thread=True)
    def _generate_titles(self):
        from thread_namer import get_session_title, title_sessions
        untitled = [s for s in self.thread_sessions if not get_session_title(s)]
        if untitled:
            title_sessions(untitled)
            self.app.call_from_thread(self._rebuild_options)

    def _build_options(self) -> list[Option]:
        options = []
        for i, s in enumerate(self.thread_sessions):
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
        if idx is not None and idx < len(self.thread_sessions):
            self.dismiss(self.thread_sessions[idx])
            return
        self.app.notify("No thread selected", severity="error", timeout=2)

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
