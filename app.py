"""Claude Orchestrator TUI — central hub for managing workstreams and Claude sessions."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from enum import Enum
from pathlib import Path

from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
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
    Category, Link, Origin, Status, Store, Workstream,
    STATUS_ICONS, STATUS_ORDER,
    _relative_time,
)
from sessions import discover_sessions, get_live_session_ids, refresh_session_tail, ClaudeSession
from threads import (
    Thread, ThreadActivity, discover_threads, _extract_first_message,
    mark_thread_seen, session_activity, load_last_seen,
)
from thread_namer import (
    apply_cached_names, name_uncached_threads,
    title_sessions, get_session_title,
)
from watcher import SessionWatcher
from workstream_synthesizer import (
    synthesize_workstreams,
    get_discovered_workstreams,
    get_assigned_thread_ids,
    pin_workstream,
    dismiss_workstream,
)


from textual.message import Message


class SessionsChanged(Message):
    """Posted on the app when the session list has been updated."""
    pass


# ─── Color Palette (matching fzedit / jira-fzf) ─────────────────────
# ANSI 256-color equivalents as hex for the mellow, desaturated palette

C_BLUE = "#87afaf"       # 109 — borders, structural
C_PURPLE = "#af87ff"     # 141 — headings, personal category
C_CYAN = "#5fd7ff"       # 81  — active states, work category
C_GREEN = "#87d787"      # 114 — success, done
C_YELLOW = "#ffd75f"     # 221 — warnings, queued
C_ORANGE = "#d7875f"     # 173 — secondary accents
C_RED = "#d75f5f"        # 167 — errors, blocked
C_LIGHT = "#a0a0a0"      # soft foreground text
C_DIM = "#585858"        # subdued — present but not loud

# ─── Background Palette (hardcoded to bypass Textual's auto-tinting) ──
BG_BASE = "#141414"      # deepest — screen background
BG_SURFACE = "#1a1a1a"   # slightly lifted — tables, panes
BG_RAISED = "#222222"    # bars, headers, inputs


def _token_color(total_tokens: int) -> str:
    """Color-code token counts by magnitude for at-a-glance readability."""
    if total_tokens >= 10_000_000:
        return C_RED
    if total_tokens >= 1_000_000:
        return C_ORANGE
    if total_tokens >= 100_000:
        return C_LIGHT
    return C_DIM


def _token_color_markup(text: str, total_tokens: int) -> str:
    """Wrap text in Rich color markup based on token magnitude."""
    color = _token_color(total_tokens)
    return f"[{color}]{text}[/{color}]"


def _colored_tokens(session_or_thread) -> str:
    """Return a Rich-markup token string colored by magnitude."""
    total = getattr(session_or_thread, 'total_tokens', None)
    if total is None:
        total = session_or_thread.total_input_tokens + session_or_thread.total_output_tokens
    color = _token_color(total)
    return f"[{color}]{session_or_thread.tokens_display}[/{color}]"


STATUS_THEME = {
    Status.QUEUED: C_DIM,
    Status.IN_PROGRESS: C_CYAN,
    Status.AWAITING_REVIEW: C_PURPLE,
    Status.DONE: C_GREEN,
    Status.BLOCKED: C_RED,
}

CATEGORY_THEME = {
    Category.WORK: C_CYAN,
    Category.PERSONAL: C_PURPLE,
    Category.META: C_DIM,
}

# ─── Thread Activity Display ─────────────────────────────────────────

THROBBER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def _refresh_liveness(sessions: list[ClaudeSession]) -> None:
    """Update is_live flags on cached sessions from current process state."""
    live_ids = get_live_session_ids()
    for s in sessions:
        s.is_live = s.session_id in live_ids

_ACTIVITY_PRIORITY = {
    ThreadActivity.THINKING: 0,
    ThreadActivity.AWAITING_INPUT: 1,
    ThreadActivity.RESPONSE_FRESH: 2,
    ThreadActivity.RESPONSE_READY: 3,
    ThreadActivity.IDLE: 4,
}


def _activity_icon(activity: ThreadActivity, throbber_frame: int = 0) -> str:
    """Return a Rich-markup activity indicator. Animated for THINKING."""
    if activity == ThreadActivity.THINKING:
        frame = THROBBER_FRAMES[throbber_frame % len(THROBBER_FRAMES)]
        return f"[bold {C_CYAN}]{frame}[/bold {C_CYAN}]"
    if activity == ThreadActivity.AWAITING_INPUT:
        return f"[{C_YELLOW}]◉[/{C_YELLOW}]"
    if activity == ThreadActivity.RESPONSE_FRESH:
        return f"[bold {C_GREEN}]●[/bold {C_GREEN}]"
    if activity == ThreadActivity.RESPONSE_READY:
        return f"[{C_ORANGE}]●[/{C_ORANGE}]"
    return f"[{C_DIM}]·[/{C_DIM}]"


def _activity_badge(activity: ThreadActivity) -> str:
    """Return a Rich-markup pill/badge for non-idle activity states."""
    if activity == ThreadActivity.THINKING:
        return f"[italic {C_CYAN}]thinking…[/italic {C_CYAN}]"
    if activity == ThreadActivity.AWAITING_INPUT:
        return f"[{C_YELLOW}]your turn[/{C_YELLOW}]"
    if activity == ThreadActivity.RESPONSE_FRESH:
        return f"[bold {C_GREEN}]done[/bold {C_GREEN}]"
    if activity == ThreadActivity.RESPONSE_READY:
        return f"[{C_ORANGE}]done[/{C_ORANGE}]"
    return ""


def _best_activity(sessions: list, last_seen: dict[str, str] | None = None) -> ThreadActivity:
    """Return the most urgent activity state across a list of sessions."""
    if not sessions:
        return ThreadActivity.IDLE
    best = ThreadActivity.IDLE
    for s in sessions:
        act = session_activity(s, last_seen)
        if _ACTIVITY_PRIORITY[act] < _ACTIVITY_PRIORITY[best]:
            best = act
    return best


def _render_session_option(
    s: ClaudeSession, act: ThreadActivity, throbber_frame: int = 0,
    title_width: int = 48,
) -> str:
    """Render a session as a formatted two-line OptionList entry."""
    icon = _activity_icon(act, throbber_frame)
    badge = _activity_badge(act)
    model = _short_model(s.model)
    title = _session_title(s)[:title_width]
    tokens = _colored_tokens(s)

    # Title color by state
    if act == ThreadActivity.IDLE:
        title_fmt = f"[{C_DIM}]{title}[/{C_DIM}]"
    elif act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
        title_fmt = f"[bold]{title}[/bold]"
    elif act == ThreadActivity.RESPONSE_FRESH:
        title_fmt = f"[bold {C_GREEN}]{title}[/bold {C_GREEN}]"
    elif act == ThreadActivity.RESPONSE_READY:
        title_fmt = f"[{C_ORANGE}]{title}[/{C_ORANGE}]"
    else:
        title_fmt = title

    # Align badges by padding raw title to fixed width
    pad = " " * max(1, title_width + 2 - len(title))
    badge_part = f"{pad}{badge}" if badge else ""
    line1 = f" {icon}  {title_fmt}{badge_part}"
    line2 = (
        f"      [{C_DIM}]{model} · {s.message_count} msgs · "
        f"[/{C_DIM}]{tokens}[{C_DIM}] tok · {s.age}[/{C_DIM}]"
    )
    return f"{line1}\n{line2}"

LINK_TYPE_ICONS = {
    "worktree": "\U0001f333",
    "ticket": "\U0001f3ab",
    "claude-session": "\U0001f916",
    "slack": "\U0001f4ac",
    "file": "\U0001f4c4",
    "url": "\U0001f517",
}
LINK_ORDER = ["worktree", "ticket", "claude-session", "file", "url", "slack"]
LINK_KINDS = list(LINK_ORDER)


class ViewMode(str, Enum):
    WORKSTREAMS = "workstreams"
    SESSIONS = "sessions"
    ARCHIVED = "archived"


# ─── Rich Markup Helpers ────────────────────────────────────────────

def _status_markup(status: Status) -> str:
    c = STATUS_THEME[status]
    return f"[{c}]{STATUS_ICONS[status]} {status.value}[/{c}]"


def _category_markup(cat: Category) -> str:
    c = CATEGORY_THEME[cat]
    return f"[{c}]{cat.value}[/{c}]"


def _link_icon(kind: str) -> str:
    return LINK_TYPE_ICONS.get(kind, "\u2022")


def _ws_indicators(ws: Workstream, tmux_check=None) -> str:
    """Build indicator string for a workstream row."""
    parts = []
    if tmux_check and tmux_check(ws):
        parts.append("\u26a1")
    if ws.is_stale and ws.status != Status.DONE:
        parts.append("\u23f0")
    link_types = set(lnk.kind for lnk in ws.links)
    if link_types:
        icons = "".join(LINK_TYPE_ICONS.get(t, "") for t in LINK_ORDER if t in link_types)
        if icons:
            parts.append(icons)
    return " ".join(parts) if parts else ""


def _short_project(path: str) -> str:
    """Abbreviate project path to just the directory name."""
    cleaned = path.replace(str(Path.home()), "~")
    return Path(cleaned).name or cleaned


def _short_model(model: str) -> str:
    lower = model.lower()
    if "opus" in lower:
        return "opus"
    if "sonnet" in lower:
        return "sonnet"
    if "haiku" in lower:
        return "haiku"
    return model[:12] if model else "\u2014"


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

    _option_list_id: str = ""  # subclass sets this

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
            _open_link(link, ws=self.ws, app=self.app)
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
        Binding("a", "archive_thread", "Archive/restore"),
        Binding("h", "focus_sessions", show=False),
        Binding("l", "focus_archived", show=False),
    ] + _VimOptionListMixin.VIM_BINDINGS

    DEFAULT_CSS = f"""
    DetailScreen {{ align: center middle; }}
    #detail-container {{
        width: 100%; height: 100%;
        padding: 0; background: {BG_BASE};
    }}

    /* Header */
    #detail-header {{
        height: auto;
        padding: 1 3;
        background: {BG_BASE};
    }}
    #detail-title {{ text-style: bold; }}
    #detail-meta {{ color: {C_DIM}; }}
    #detail-desc {{ padding-top: 1; }}

    /* Session lists side-by-side */
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

    /* Body */
    #detail-scroll {{
        height: 1fr;
        border-top: blank;
    }}
    #detail-body {{
        padding: 1 3;
    }}

    /* Help bar */
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
        self._active_pane: str = "sessions"  # "sessions" or "archived"

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-container"):
            # Header band
            with Vertical(id="detail-header"):
                yield Static(self._render_title(), id="detail-title")
                yield Static(self._render_meta(), id="detail-meta")
                if self.ws.description:
                    yield Static(self.ws.description, id="detail-desc")

            # Session lists side-by-side
            with Horizontal(id="detail-lists"):
                with Vertical(id="detail-sessions-pane", classes="detail-list-pane"):
                    yield Static(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]", id="detail-sessions-label", classes="detail-list-label")
                    yield OptionList(id="detail-sessions")
                    yield Static(f"[{C_DIM}]No sessions[/{C_DIM}]", id="detail-no-sessions")
                with Vertical(id="detail-archived-pane", classes="detail-list-pane"):
                    yield Static(f"[{C_DIM}]Archived[/{C_DIM}]", id="detail-archived-label", classes="detail-list-label")
                    yield OptionList(id="detail-archived")
                    yield Static(f"[{C_DIM}]Empty[/{C_DIM}]", id="detail-no-archived")

            # Scrollable body (context, notes, timeline)
            with VerticalScroll(id="detail-scroll"):
                yield Static(self._render_body(), id="detail-body")

            # Help bar
            yield Static(self._render_help(), id="detail-help")

    def on_mount(self):
        self._last_seen_cache = load_last_seen()
        self._load_detail_sessions()
        self.query_one("#detail-sessions", OptionList).focus()
        self._throbber_timer = self.set_interval(0.08, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

    def _focused_olist(self) -> OptionList:
        """Return the currently active pane's OptionList."""
        if self._active_pane == "archived":
            return self.query_one("#detail-archived", OptionList)
        return self.query_one("#detail-sessions", OptionList)

    def _olist(self) -> OptionList:
        """Override mixin to route vim keys to the active pane."""
        return self._focused_olist()

    def action_focus_sessions(self):
        """Switch to the sessions pane (h)."""
        self._active_pane = "sessions"
        olist = self.query_one("#detail-sessions", OptionList)
        olist.focus()
        self._update_pane_labels()

    def action_focus_archived(self):
        """Switch to the archived pane (l)."""
        if not self.ws.archived_session_ids:
            return
        self._active_pane = "archived"
        olist = self.query_one("#detail-archived", OptionList)
        olist.focus()
        self._update_pane_labels()

    def _update_pane_labels(self):
        """Highlight the active pane's label."""
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
        _refresh_liveness(self._detail_sessions)
        _refresh_liveness(self._archived_sessions)

    def on_sessions_changed(self, event: SessionsChanged):
        """React to real-time session updates from the file watcher."""
        self._load_detail_sessions()
        self._refresh()

    # -- Throbber animation --

    def _tick_throbber(self):
        """Animate only the thinking/awaiting sessions in-place."""
        self._throbber_frame += 1
        olist = self.query_one("#detail-sessions", OptionList)
        for i, s in enumerate(self._detail_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)
        arch_olist = self.query_one("#detail-archived", OptionList)
        for i, s in enumerate(self._archived_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame)
                arch_olist.replace_option_prompt_at_index(i, prompt)

    # -- Session list --

    def _load_detail_sessions(self):
        app = self.app
        if hasattr(app, '_sessions_for_ws'):
            all_sessions = app._sessions_for_ws(self.ws, include_archived_threads=True)
            hidden = set(self.ws.archived_session_ids)
            self._detail_sessions = [s for s in all_sessions if s.session_id not in hidden]
            self._archived_sessions = [s for s in all_sessions if s.session_id in hidden]
        else:
            self._detail_sessions = _find_sessions_for_ws(self.ws, getattr(app, 'sessions', []))
            self._archived_sessions = []

        # Active sessions pane
        olist = self.query_one("#detail-sessions", OptionList)
        no_sess = self.query_one("#detail-no-sessions", Static)
        if self._detail_sessions:
            olist.display = True
            no_sess.display = False
            self._build_session_list()
            if olist.option_count > 0 and olist.highlighted is None:
                olist.highlighted = 0
        else:
            olist.display = False
            no_sess.display = True

        # Archived sessions pane
        arch_olist = self.query_one("#detail-archived", OptionList)
        no_arch = self.query_one("#detail-no-archived", Static)
        arch_pane = self.query_one("#detail-archived-pane")
        if self._archived_sessions:
            arch_pane.display = True
            arch_olist.display = True
            no_arch.display = False
            self._build_archived_list()
            if arch_olist.option_count > 0 and arch_olist.highlighted is None:
                arch_olist.highlighted = 0
        elif self._active_pane == "archived":
            # Was viewing archived but it's now empty — switch back
            self._active_pane = "sessions"
            arch_pane.display = False
            self.query_one("#detail-sessions", OptionList).focus()
        else:
            arch_pane.display = False

        self._update_pane_labels()


    def _build_session_list(self):
        """Full rebuild of the active session OptionList."""
        olist = self.query_one("#detail-sessions", OptionList)
        olist.clear_options()
        for i, s in enumerate(self._detail_sessions):
            act = session_activity(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame)
            olist.add_option(Option(prompt, id=str(i)))

    def _build_archived_list(self):
        """Full rebuild of the archived session OptionList."""
        olist = self.query_one("#detail-archived", OptionList)
        olist.clear_options()
        for i, s in enumerate(self._archived_sessions):
            act = session_activity(s, self._last_seen_cache)
            prompt = _render_session_option(s, act, self._throbber_frame)
            olist.add_option(Option(prompt, id=f"a{i}"))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        oid = event.option_id
        if oid.startswith("a"):
            idx = int(oid[1:])
            if idx < len(self._archived_sessions):
                session = self._archived_sessions[idx]
                mark_thread_seen(session.session_id)
                dirs = _ws_directories(self.ws)
                _resume_session_now(self.ws, session, dirs, self.app)
        else:
            idx = int(oid)
            if idx < len(self._detail_sessions):
                session = self._detail_sessions[idx]
                mark_thread_seen(session.session_id)
                dirs = _ws_directories(self.ws)
                _resume_session_now(self.ws, session, dirs, self.app)

    # -- Render helpers --

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

        # Context
        dirs = _ws_directories(self.ws)
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

        # Notes
        if self.ws.notes:
            lines.append(f"[bold {C_BLUE}]Notes[/bold {C_BLUE}]")
            for line in self.ws.notes.split("\n"):
                lines.append(f"  {line}")
            lines.append("")

        # Timeline
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

    # -- Actions --

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
        """Archive or restore the selected session depending on active pane."""
        olist = self._focused_olist()
        idx = olist.highlighted
        if self._active_pane == "archived":
            # Restore from archived
            if idx is None or idx >= len(self._archived_sessions):
                return
            sid = self._archived_sessions[idx].session_id
            if sid in self.ws.archived_session_ids:
                self.ws.archived_session_ids.remove(sid)
                self.store.update(self.ws)
        else:
            # Archive from active
            if idx is None or idx >= len(self._detail_sessions):
                return
            sid = self._detail_sessions[idx].session_id
            if sid not in self.ws.archived_session_ids:
                self.ws.archived_session_ids.append(sid)
                self.store.update(self.ws)
        old_idx = idx
        self._refresh()
        olist = self._focused_olist()
        items = self._archived_sessions if self._active_pane == "archived" else self._detail_sessions
        if items and old_idx is not None:
            olist.highlighted = min(old_idx, len(items) - 1)

    def action_spawn(self):
        ok, err = _launch_orch_claude(self.ws, store=self.store)
        if ok:
            self.app.notify("Session spawned", timeout=2)
            self._add_spawning_placeholder()
        else:
            self.app.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def _add_spawning_placeholder(self):
        """Show a placeholder entry instantly while the real session spins up."""
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
        _do_resume(self.ws, self.app, getattr(self.app, 'sessions', []))

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

def _session_title(session: ClaudeSession, titles: dict[str, str] | None = None) -> str:
    """Best available title for a session: AI title > cached > first message > project."""
    # AI-generated title (from cache)
    if titles and session.session_id in titles:
        return titles[session.session_id]
    cached = get_session_title(session)
    if cached:
        return cached
    # Fall back to first user message
    first_msg = _extract_first_message(session)
    if first_msg:
        line = first_msg.split("\n")[0].strip()
        if line.startswith("#"):
            line = line.lstrip("# ")
        if len(line) > 60:
            line = line[:57] + "..."
        return line
    return _short_project(session.project_path)


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
        self._throbber_timer = self.set_interval(0.08, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

    def _refresh_session_liveness(self):
        _refresh_liveness(self.thread_sessions)

    def _tick_throbber(self):
        """Animate only the thinking/awaiting sessions in-place."""
        self._throbber_frame += 1
        olist = self.query_one("#threadpick-list", OptionList)
        for i, s in enumerate(self.thread_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame)
                olist.replace_option_prompt_at_index(i, prompt)

    @work(thread=True)
    def _generate_titles(self):
        """Generate AI titles for untitled sessions in the background."""
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
        """Full rebuild after titles are generated."""
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


# ─── Inline Inputs ──────────────────────────────────────────────────

class SearchInput(Input):
    BINDINGS = [Binding("escape", "cancel_search", "Cancel", priority=True)]

    def action_cancel_search(self):
        self.value = ""
        app = self.app
        app.search_text = ""
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
    """Inline input for adding a quick note to the selected workstream."""
    BINDINGS = [Binding("escape", "cancel_note", "Cancel", priority=True)]

    def action_cancel_note(self):
        self.value = ""
        self.display = False
        self.app._active_table().focus()


class RenameInput(Input):
    """Inline input for renaming the selected workstream."""
    BINDINGS = [Binding("escape", "cancel_rename", "Cancel", priority=True)]

    def action_cancel_rename(self):
        self.value = ""
        self.display = False
        self.app._active_table().focus()


# ─── Utilities ──────────────────────────────────────────────────────

def _has_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _find_tmux_window_for_session(session_id: str) -> str | None:
    """Find a tmux window already running a Claude session (via @orch_session_id tag).

    Returns the window_id if found, None otherwise.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-F",
             "#{@orch_session_id}\t#{window_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split("\n"):
            if "\t" not in line:
                continue
            tag, wid = line.split("\t", 1)
            if tag == session_id:
                return wid
    except Exception:
        pass
    return None


def _switch_to_tmux_window(window_id: str) -> bool:
    """Switch to an existing tmux window by ID."""
    try:
        result = subprocess.run(
            ["tmux", "select-window", "-t", window_id],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _launch_orch_claude(
    ws: Workstream,
    store: Store | None = None,
    session_id: str | None = None,
    prompt: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Launch Claude via the orch-claude wrapper in a new tmux window.

    Returns (success, error_message).
    """
    if not os.environ.get("TMUX"):
        return False, "Not running inside tmux"

    wrapper = str(Path(__file__).parent / "orch-claude")

    if cwd is None:
        cwd = _ws_working_dir(ws)

    # Save the prompt as a note so it's never lost
    if prompt and prompt.strip() and store:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] spawn: {prompt.strip()}"
        ws.notes = (ws.notes + "\n" + entry) if ws.notes else entry
        store.update(ws)

    # Determine the tmux session we're running in
    tmux_session = os.environ.get("TMUX_SESSION", "orch")

    cmd = [
        "tmux", "new-window", "-t", tmux_session,
        "-n", f"\U0001f916{ws.name[:18]}",
        "-c", cwd,
        wrapper,
        "--ws-id", ws.id,
        "--ws-name", ws.name,
        "--ws-desc", ws.description or "",
        "--ws-status", ws.status.value,
        "--ws-category", ws.category.value,
        "--cwd", cwd,
    ]

    if ws.notes:
        cmd += ["--ws-notes", ws.notes[:500]]

    if session_id:
        cmd += ["--resume", session_id]
    elif prompt:
        cmd += ["--prompt", prompt]

    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        # Give tmux a moment to fail (it fails fast if session doesn't exist)
        try:
            proc.wait(timeout=2)
            if proc.returncode != 0:
                err = proc.stderr.read().decode().strip() if proc.stderr else "unknown error"
                return False, err
        except subprocess.TimeoutExpired:
            pass  # Still running = success (tmux window is up)
        return True, ""
    except Exception as e:
        return False, str(e)


def _ws_directories(ws: Workstream) -> list[str]:
    """Get all directory paths linked to a workstream (worktree or file)."""
    dirs = []
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value)
            if os.path.isdir(expanded):
                dirs.append(expanded)
    return dirs


def _ws_working_dir(ws: Workstream) -> str:
    dirs = _ws_directories(ws)
    return dirs[0] if dirs else os.getcwd()


def _find_sessions_for_ws(ws: Workstream, all_sessions: list[ClaudeSession]) -> list[ClaudeSession]:
    """Auto-discover Claude sessions matching a workstream's directories."""
    found = []
    seen = set()

    # 1. Explicit claude-session links (manual overrides, highest priority)
    for link in ws.links:
        if link.kind == "claude-session":
            for s in all_sessions:
                if (s.session_id == link.value or s.session_id.startswith(link.value)) \
                        and s.session_id not in seen:
                    found.append(s)
                    seen.add(s.session_id)

    # 2. Auto-match by directory — exact match only (no subdirectory matching,
    #    which would cause a monorepo root to vacuum up every worktree's sessions).
    #    The synthesizer + thread_ids is the proper grouping mechanism now.
    ws_dirs = set()
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value).rstrip("/")
            if os.path.isdir(expanded):
                ws_dirs.add(expanded)

    if ws_dirs:
        for s in all_sessions:
            if s.session_id in seen:
                continue
            sp = s.project_path.rstrip("/")
            if sp in ws_dirs:
                found.append(s)
                seen.add(s.session_id)

    found.sort(key=lambda s: s.last_activity or "", reverse=True)
    return found


def _do_resume(ws: Workstream, app, sessions: list[ClaudeSession] | None = None):
    """Smart resume: auto-discover sessions, fall back to directory.

    With 1 matching session: resumes immediately.
    With 2+: opens a thread picker so the user can choose.
    """
    if not _has_tmux():
        app.notify("Not in a tmux session", severity="error", timeout=2)
        return

    if hasattr(app, '_sessions_for_ws'):
        matching = app._sessions_for_ws(ws)
    else:
        matching = _find_sessions_for_ws(ws, sessions or [])
    dirs = _ws_directories(ws)

    if matching:
        if len(matching) == 1:
            _resume_session_now(ws, matching[0], dirs, app)
        else:
            def on_pick(session: ClaudeSession | None):
                if session:
                    _resume_session_now(ws, session, dirs, app)
            app.push_screen(ThreadPickerScreen(ws, matching), callback=on_pick)
        return

    if dirs:
        _launch_orch_claude(ws, cwd=dirs[0])
        app.notify(f"New session in {dirs[0]}", timeout=2)
        return

    app.notify("No sessions or directories found", timeout=2)


def _resume_session_now(ws: Workstream, session: ClaudeSession, dirs: list[str], app):
    """Resume a specific session immediately.

    If the session is already running in a tmux window (detached), switches
    to that window instead of spawning a duplicate.
    """
    mark_thread_seen(session.session_id)

    # Check if this session is already running in a tmux window
    existing_wid = _find_tmux_window_for_session(session.session_id)
    if existing_wid and _switch_to_tmux_window(existing_wid):
        app.notify(f"Reattached: {session.display_name}", timeout=2)
        return

    cwd = session.project_path
    if not os.path.isdir(cwd):
        cwd = dirs[0] if dirs else os.getcwd()
    _launch_orch_claude(ws, session_id=session.session_id, cwd=cwd)
    app.notify(f"Resuming: {session.display_name}", timeout=2)


def _open_link(link: Link, ws: Workstream | None = None, app=None):
    value = os.path.expanduser(link.value)
    if link.kind == "url":
        subprocess.Popen(["xdg-open", link.value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "worktree":
        if _has_tmux():
            subprocess.Popen(["tmux", "new-window", "-n", link.label, "-c", value],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "file":
        if os.path.isdir(value):
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.path.isfile(value):
            editor = os.environ.get("EDITOR", "nvim")
            if _has_tmux():
                subprocess.Popen(["tmux", "new-window", "-n", link.label, editor, value],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "claude-session":
        if ws and app:
            _launch_orch_claude(ws, session_id=link.value)
        elif _has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"claude:{link.label}",
                 "claude", "--resume", link.value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


# ─── Main App ───────────────────────────────────────────────────────

class OrchestratorApp(App):
    """Claude Orchestrator — workstream & session dashboard."""

    CSS = f"""
    Screen {{
        background: {BG_BASE};
    }}

    /* ── All bars: flat, no background — text color only ── */
    #status-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_BASE};
        dock: top;
    }}
    #view-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_BASE};
        dock: top;
    }}
    #filter-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_BASE};
        dock: top;
    }}
    #summary-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_BASE};
        color: {C_DIM};
        dock: bottom;
    }}

    /* ── Main Content ── */
    #main-content {{
        height: 1fr;
    }}

    /* ── Tables ── */
    DataTable {{
        width: 1fr;
    }}

    /* ── Preview Pane ── */
    #preview-pane {{
        width: 1fr;
        min-width: 40;
        border-left: blank;
        padding: 1 2;
        background: {BG_BASE};
    }}
    #preview-content {{
        width: 100%;
    }}
    #preview-sessions {{
        height: auto;
        max-height: 16;
        width: 100%;
        margin: 0;
        padding: 0;
    }}

    /* ── Inline Inputs ── */
    #search-input, #command-input, #note-input, #rename-input {{
        dock: bottom;
        height: 1;
        display: none;
        border: none;
        background: {BG_BASE};
    }}
    #search-input:focus, #command-input:focus, #note-input:focus, #rename-input:focus {{
        border: none;
        background: {BG_BASE};
    }}
    """

    TITLE = "orchestrator"
    CSS_PATH = "orchestrator.tcss"
    theme = "mellow"

    BINDINGS = [
        # Navigation
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("ctrl+n", "cursor_down", "Down", show=False),
        Binding("ctrl+p", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("ctrl+d", "half_page_down", "\u00bdPgDn", show=False),
        Binding("ctrl+u", "half_page_up", "\u00bdPgUp", show=False),
        Binding("enter", "select_item", "Open", show=True),

        # View switching
        Binding("tab", "next_view", "Tab", show=True, priority=True),
        Binding("shift+tab", "prev_view", show=False, priority=True),

        # Actions
        Binding("a", "add", "Add", show=True),
        Binding("b", "brain_dump", "Brain", show=False),
        Binding("s", "cycle_status", "Status", show=True),
        Binding("S", "cycle_status_back", "Status\u2190", show=False),
        Binding("c", "spawn", "Spawn", show=True),
        Binding("r", "resume", "Resume", show=True),
        Binding("l", "link_action", "Link", show=True),
        Binding("n", "quick_note", show=False),
        Binding("e", "edit_notes", show=False),
        Binding("E", "rename", show=False),
        Binding("o", "open_links", show=False),
        Binding("x", "archive", show=False),
        Binding("d", "delete_item", show=False),
        Binding("u", "unarchive", show=False),

        # Filters
        Binding("1", "filter('all')", show=False),
        Binding("2", "filter('work')", show=False),
        Binding("3", "filter('personal')", show=False),
        Binding("4", "filter('active')", show=False),
        Binding("5", "filter('stale')", show=False),
        Binding("slash", "search", "/", show=True),

        # Sort
        Binding("f1", "sort('status')", show=False),
        Binding("f2", "sort('updated')", show=False),
        Binding("f3", "sort('created')", show=False),
        Binding("f4", "sort('category')", show=False),
        Binding("f5", "sort('name')", show=False),

        # Command palette
        Binding("colon", "command_palette", ":", show=True),

        # Other
        Binding("p", "toggle_preview", show=False),
        Binding("R", "refresh", show=False),
        Binding("question_mark", "help", "?", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.register_theme(Theme(
            name="mellow",
            primary="#87afaf",       # muted teal — structural/borders
            secondary="#af87ff",     # soft purple — headings
            background="#141414",    # true dark
            surface="#1a1a1a",       # barely lifted
            panel="#222222",         # subtle differentiation
            foreground="#a0a0a0",    # matches C_LIGHT — easy on the eyes
            accent="#5fd7ff",        # muted cyan — active states
            warning="#ffd75f",       # warm yellow
            error="#d75f5f",         # muted red
            success="#87d787",       # soft green
            dark=True,
            luminosity_spread=0.08,  # very tight shade ramp
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
        self.store = Store()
        self.view_mode: ViewMode = ViewMode.WORKSTREAMS
        self.filter_mode: str = "all"
        self.sort_mode: str = "updated"
        self.search_text: str = ""
        self.sessions: list[ClaudeSession] = []
        self.threads: list[Thread] = []
        self.discovered_ws: list[Workstream] = []
        self.preview_visible: bool = True
        self._tmux_paths: set[str] = set()
        self._tmux_names: set[str] = set()
        self._throbber_frame: int = 0
        self._throbber_timer = None
        self._preview_sessions: list[ClaudeSession] = []
        self._last_seen_cache: dict[str, str] = {}
        self._session_watcher: SessionWatcher | None = None

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
        # Workstreams table
        ws_table = self.query_one("#ws-table", DataTable)
        ws_table.cursor_type = "row"
        ws_table.zebra_stripes = False
        ws_table.add_columns("", "Name", "Sess", "Category", "Updated")

        # Sessions table (hidden initially)
        sessions_table = self.query_one("#sessions-table", DataTable)
        sessions_table.cursor_type = "row"
        sessions_table.zebra_stripes = False
        sessions_table.add_columns("Title", "Thread", "Model", "Tokens", "Age")
        sessions_table.display = False

        # Archived table (hidden initially)
        archived_table = self.query_one("#archived-table", DataTable)
        archived_table.cursor_type = "row"
        archived_table.zebra_stripes = False
        archived_table.add_columns("", "Name", "Sess", "Category", "Updated")
        archived_table.display = False

        # Load data
        self._refresh_ws_table()
        self._load_sessions()
        self._refresh_archived_table()

        # Update all bars
        self._update_all_bars()

        # Hide preview sessions list initially
        self.query_one("#preview-sessions", OptionList).display = False

        # Start tmux polling
        self._poll_tmux()
        self.set_interval(30, self._poll_tmux)

        # File watcher for real-time session discovery (inotify/FSEvents)
        self._session_watcher = SessionWatcher(
            on_change=lambda: self.call_from_thread(self._poll_sessions),
            debounce=1.0,
        )
        self._session_watcher.start()
        # Fallback poll in case watcher misses events (e.g. NFS, edge cases)
        self.set_interval(10, self._poll_sessions)

        # Throbber animation for thinking sessions
        self._throbber_timer = self.set_interval(0.1, self._tick_throbber)
        self.set_interval(3, self._refresh_session_liveness)

        # Focus main table
        ws_table.focus()

    def on_unmount(self):
        if self._session_watcher:
            self._session_watcher.stop()

    # ── Active table helper ──

    def _active_table(self) -> DataTable:
        if self.view_mode == ViewMode.SESSIONS:
            return self.query_one("#sessions-table", DataTable)
        elif self.view_mode == ViewMode.ARCHIVED:
            return self.query_one("#archived-table", DataTable)
        return self.query_one("#ws-table", DataTable)

    # ── View switching ──

    def action_next_view(self):
        modes = list(ViewMode)
        idx = modes.index(self.view_mode)
        self.view_mode = modes[(idx + 1) % len(modes)]
        self._apply_view()

    def action_prev_view(self):
        modes = list(ViewMode)
        idx = modes.index(self.view_mode)
        self.view_mode = modes[(idx - 1) % len(modes)]
        self._apply_view()

    def _apply_view(self):
        ws_table = self.query_one("#ws-table", DataTable)
        sessions_table = self.query_one("#sessions-table", DataTable)
        archived_table = self.query_one("#archived-table", DataTable)
        filter_bar = self.query_one("#filter-bar", Static)

        ws_table.display = self.view_mode == ViewMode.WORKSTREAMS
        sessions_table.display = self.view_mode == ViewMode.SESSIONS
        archived_table.display = self.view_mode == ViewMode.ARCHIVED
        filter_bar.display = self.view_mode == ViewMode.WORKSTREAMS

        if self.view_mode == ViewMode.SESSIONS:
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

    def action_toggle_preview(self):
        pane = self.query_one("#preview-pane")
        self.preview_visible = not self.preview_visible
        pane.display = self.preview_visible

    def _refresh_session_liveness(self):
        old_live = {s.session_id for s in self.sessions if s.is_live}
        _refresh_liveness(self.sessions)
        _refresh_liveness(self._preview_sessions)
        new_live = {s.session_id for s in self.sessions if s.is_live}

        # Tail-read metadata for live sessions + sessions that just died
        changed = old_live != new_live
        active_ids = new_live | (old_live - new_live)
        seen = set()
        for s in self.sessions:
            if s.session_id in active_ids and s.session_id not in seen:
                seen.add(s.session_id)
                if refresh_session_tail(s):
                    changed = True
        for s in self._preview_sessions:
            if s.session_id in active_ids and s.session_id not in seen:
                seen.add(s.session_id)
                refresh_session_tail(s)

        if changed:
            self._refresh_ws_table()
            if self.view_mode == ViewMode.SESSIONS:
                self._refresh_sessions_table()

    def _tick_throbber(self):
        """Animate throbbers in-place for thinking/awaiting sessions."""
        self._throbber_frame += 1
        olist = self.query_one("#preview-sessions", OptionList)
        for i, s in enumerate(self._preview_sessions):
            act = session_activity(s, self._last_seen_cache)
            if act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
                prompt = _render_session_option(s, act, self._throbber_frame, title_width=35)
                olist.replace_option_prompt_at_index(i, prompt)

    def _update_preview(self):
        if not self.preview_visible:
            return
        if self.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
            self._render_ws_preview(ws)
        elif self.view_mode == ViewMode.SESSIONS:
            session = self._selected_session()
            self._render_session_preview(session)
        elif self.view_mode == ViewMode.ARCHIVED:
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
            content.update(f"[{C_DIM}]Select a thread[/{C_DIM}]\n\n{self._nav_hints()}")
            olist.display = False
            self._preview_sessions = []
            return

        lines = []
        lines.append(f"[bold {C_PURPLE}]{ws.name}[/bold {C_PURPLE}]")
        lines.append(f"{_status_markup(ws.status)}  {_category_markup(ws.category)}")
        if archived:
            lines.append(f"[{C_DIM}]Archived[/{C_DIM}]")
        lines.append("")

        # Description
        if ws.description:
            lines.append(ws.description)
            lines.append("")

        # Auto-discovered Claude sessions — the brain threads
        thread_sessions = self._sessions_for_ws(ws)
        if thread_sessions:
            total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in thread_sessions)
            total_msgs = sum(s.message_count for s in thread_sessions)
            _tk = f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000 else str(total_tokens)
            last_active = thread_sessions[0].age  # already sorted most recent first

            lines.append(f"[bold {C_BLUE}]Activity[/bold {C_BLUE}]")
            lines.append(
                f"  [{C_CYAN}]{len(thread_sessions)}[/{C_CYAN}] sessions  "
                f"[{C_DIM}]\u00b7[/{C_DIM}]  {total_msgs} messages  "
                f"[{C_DIM}]\u00b7[/{C_DIM}]  {_token_color_markup(_tk, total_tokens)} tokens"
            )
            lines.append(f"  [{C_DIM}]Last active[/{C_DIM}] {last_active}")
            lines.append("")

            archived_count = len(ws.archived_session_ids)
            if archived_count:
                lines.append(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]  [{C_DIM}]({archived_count} archived)[/{C_DIM}]")
            else:
                lines.append(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]")
        else:
            lines.append(f"[{C_DIM}]No Claude sessions found[/{C_DIM}]")
            dirs = _ws_directories(ws)
            if not dirs:
                lines.append(f"[{C_DIM}]Link a directory to auto-discover sessions[/{C_DIM}]")
            lines.append("")

        # Context (directories, notes — collapsed, not the focus)
        dirs = _ws_directories(ws)
        if dirs:
            lines.append(f"[bold {C_BLUE}]Context[/bold {C_BLUE}]")
            for d in dirs:
                short = d.replace(str(Path.home()), "~")
                lines.append(f"  [{C_DIM}]{short}[/{C_DIM}]")
            lines.append("")

        # Notes (if any)
        if ws.notes:
            lines.append(f"[bold {C_BLUE}]Notes[/bold {C_BLUE}]")
            for line in ws.notes.split("\n")[:8]:
                lines.append(f"  {line}")
            if ws.notes.count("\n") > 8:
                lines.append(f"  [{C_DIM}]...[/{C_DIM}]")
            lines.append("")

        # Timeline (compact)
        lines.append(f"[{C_DIM}]Created {_relative_time(ws.created_at)} \u00b7 Updated {_relative_time(ws.updated_at)}[/{C_DIM}]")

        # Action hints
        lines.append("")
        if archived:
            lines.append(self._nav_hints())
        else:
            lines.append(self._hint_line([
                ("r", "resume"), ("c", "new session"), ("s", "status"),
                ("n", "note"), ("o", "open"),
            ]))

        content.update("\n".join(lines))

        # Populate the interactive session picker
        self._preview_sessions = thread_sessions
        self._last_seen_cache = load_last_seen()
        if thread_sessions:
            olist.display = True
            self._refresh_preview_sessions()
        else:
            olist.display = False

    def _refresh_preview_sessions(self):
        """Rebuild the preview session OptionList with activity indicators."""
        olist = self.query_one("#preview-sessions", OptionList)
        highlighted = olist.highlighted
        olist.clear_options()
        for i, s in enumerate(self._preview_sessions):
            act = session_activity(s, self._last_seen_cache)
            olist.add_option(Option(
                _render_session_option(s, act, self._throbber_frame, title_width=35),
                id=str(i),
            ))
        if highlighted is not None and highlighted < len(self._preview_sessions):
            olist.highlighted = highlighted

    def _render_session_preview(self, session: ClaudeSession | None):
        self.query_one("#preview-sessions", OptionList).display = False
        self._preview_sessions = []
        content = self.query_one("#preview-content", Static)
        if not session:
            content.update(f"[{C_DIM}]No session selected[/{C_DIM}]\n\n{self._nav_hints()}")
            return

        lines = []
        lines.append(f"[bold {C_PURPLE}]{session.display_name}[/bold {C_PURPLE}]")
        if session.is_live:
            lines.append(f"[bold {C_GREEN}]\u25cf LIVE[/bold {C_GREEN}]")
        lines.append("")

        # Model
        lines.append(f"[bold {C_BLUE}]Model[/bold {C_BLUE}]")
        lines.append(f"  {session.model or 'unknown'}")
        lines.append("")

        # Usage
        lines.append(f"[bold {C_BLUE}]Usage[/bold {C_BLUE}]")
        lines.append(f"  [{C_DIM}]Input[/{C_DIM}]    {session.total_input_tokens:,} tokens")
        lines.append(f"  [{C_DIM}]Output[/{C_DIM}]   {session.total_output_tokens:,} tokens")
        lines.append(f"  [{C_DIM}]Total[/{C_DIM}]    {session.tokens_display}")
        lines.append("")

        # Activity
        lines.append(f"[bold {C_BLUE}]Activity[/bold {C_BLUE}]")
        lines.append(f"  [{C_DIM}]Messages[/{C_DIM}]  {session.message_count}")
        lines.append(f"  [{C_DIM}]Last[/{C_DIM}]      {session.age}")
        lines.append("")

        # Project
        lines.append(f"[bold {C_BLUE}]Project[/bold {C_BLUE}]")
        project = session.project_path
        if project.startswith(str(Path.home())):
            project = project.replace(str(Path.home()), "~")
        lines.append(f"  {project}")
        lines.append("")

        # Session ID (small, for linking)
        lines.append(f"[{C_DIM}]Session: {session.session_id[:16]}...[/{C_DIM}]")

        # Action hints
        lines.append("")
        lines.append(self._hint_line([("r", "resume"), ("l", "link to workstream")]))

        content.update("\n".join(lines))

    def _render_thread_preview(self, thread: Thread):
        self.query_one("#preview-sessions", OptionList).display = False
        self._preview_sessions = []
        content = self.query_one("#preview-content", Static)
        lines = []

        lines.append(f"[bold {C_PURPLE}]{thread.display_name}[/bold {C_PURPLE}]")
        if thread.is_live:
            lines.append(f"[bold {C_GREEN}]\u25cf LIVE[/bold {C_GREEN}]")
        lines.append(f"[{C_DIM}]{thread.short_project}[/{C_DIM}]")
        lines.append("")

        # Aggregate stats
        lines.append(f"[bold {C_BLUE}]Activity[/bold {C_BLUE}]")
        lines.append(
            f"  [{C_CYAN}]{thread.session_count}[/{C_CYAN}] sessions  "
            f"[{C_DIM}]\u00b7[/{C_DIM}]  {thread.total_messages} messages  "
            f"[{C_DIM}]\u00b7[/{C_DIM}]  {_colored_tokens(thread)} tokens"
        )
        lines.append(f"  [{C_DIM}]Tokens[/{C_DIM}]  {_colored_tokens(thread)}")
        if thread.models:
            lines.append(f"  [{C_DIM}]Models[/{C_DIM}]  {', '.join(_short_model(m) for m in thread.models)}")
        lines.append(f"  [{C_DIM}]Last[/{C_DIM}]    {thread.age}")
        lines.append("")

        # Session list
        lines.append(f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]")
        sorted_sessions = sorted(thread.sessions,
                                 key=lambda s: s.last_activity or "", reverse=True)
        for s in sorted_sessions[:8]:
            live_mark = f"[{C_GREEN}]\u25cf[/{C_GREEN}] " if s.is_live else "  "
            title = s.display_name
            lines.append(f"  {live_mark}[{C_CYAN}]{title}[/{C_CYAN}]")
            lines.append(f"      {_short_model(s.model)} \u00b7 {s.message_count} msgs \u00b7 {_colored_tokens(s)} tokens \u00b7 {s.age}")
        if len(thread.sessions) > 8:
            lines.append(f"  [{C_DIM}]+ {len(thread.sessions) - 8} older[/{C_DIM}]")
        lines.append("")

        # Thread ID for reference
        lines.append(f"[{C_DIM}]Thread: {thread.thread_id[:16]}...[/{C_DIM}]")

        # Action hints
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
        self.query_one("#status-bar", Static).update(self._render_status_bar())
        self.query_one("#view-bar", Static).update(self._render_view_bar())
        self.query_one("#filter-bar", Static).update(self._render_filter_bar())
        self.query_one("#summary-bar", Static).update(self._render_summary_bar())

    def _render_status_bar(self) -> str:
        total = len(self.store.active)
        in_prog = len([w for w in self.store.active if w.status == Status.IN_PROGRESS])
        blocked = len([w for w in self.store.active if w.status == Status.BLOCKED])
        review = len([w for w in self.store.active if w.status == Status.AWAITING_REVIEW])
        done = len([w for w in self.store.active if w.status == Status.DONE])
        stale = len(self.store.stale())

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

        # Show total token usage if sessions loaded
        if self.sessions:
            total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in self.sessions)
            if total_tokens > 0:
                _tk = f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000 else str(total_tokens)
                parts.append(f"[{C_DIM}]\u2502[/{C_DIM}]")
                parts.append(f"{_token_color_markup(_tk, total_tokens)} tokens")

        return "  ".join(parts)

    def _render_view_bar(self) -> str:
        views = [
            (ViewMode.WORKSTREAMS, f"Workstreams ({len(self.store.active) + len(self.discovered_ws)})"),
            (ViewMode.SESSIONS, f"Sessions ({len(self.sessions)})"),
            (ViewMode.ARCHIVED, f"Archived ({len(self.store.archived)})"),
        ]
        parts = []
        for mode, label in views:
            if self.view_mode == mode:
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
            if self.filter_mode == key:
                parts.append(f"[bold {C_CYAN}] {label} [/bold {C_CYAN}]")
            else:
                parts.append(f"[{C_DIM}]{label}[/{C_DIM}]")

        sort_labels = {
            "status": "Status", "updated": "Updated", "created": "Created",
            "category": "Category", "name": "Name",
        }
        sort_label = sort_labels.get(self.sort_mode, self.sort_mode)
        parts.append(f"  [{C_DIM}]Sort:[/{C_DIM}][bold {C_BLUE}]{sort_label}[/bold {C_BLUE}]")

        if self.search_text:
            parts.append(f"  [{C_DIM}]Search:[/{C_DIM}][{C_YELLOW}]{self.search_text}[/{C_YELLOW}]")

        return " ".join(parts)

    def _render_summary_bar(self) -> str:
        if self.view_mode == ViewMode.WORKSTREAMS:
            count = self._active_table().row_count
            return (
                f"  {count} threads  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]r[/{C_DIM}] resume  "
                f"[{C_DIM}]c[/{C_DIM}] new session  "
                f"[{C_DIM}]n[/{C_DIM}] note  "
                f"[{C_DIM}]s[/{C_DIM}] status  "
                f"[{C_DIM}]/[/{C_DIM}] search  "
                f"[{C_DIM}]?[/{C_DIM}] help  "
                f"[{C_DIM}]Tab[/{C_DIM}] views"
            )
        elif self.view_mode == ViewMode.SESSIONS:
            count = len(self.sessions)
            return (
                f"  {count} sessions  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]r[/{C_DIM}] resume  "
                f"[{C_DIM}]l[/{C_DIM}] link to thread  "
                f"[{C_DIM}]Tab[/{C_DIM}] views  "
                f"[{C_DIM}]R[/{C_DIM}] refresh"
            )
        else:
            count = len(self.store.archived)
            return (
                f"  {count} archived  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]u[/{C_DIM}] unarchive  "
                f"[{C_DIM}]d[/{C_DIM}] delete  "
                f"[{C_DIM}]Tab[/{C_DIM}] views"
            )

    # ── Workstreams / Threads table ──

    def _get_unified_items(self) -> list[Workstream]:
        """Build unified list: manual workstreams + AI-discovered workstreams.

        Everything is a Workstream. Manual ones from data.json,
        discovered ones from the synthesizer cache.
        """
        manual = self._get_filtered_streams()
        discovered = list(self.discovered_ws)

        # Apply search filter to discovered
        if self.search_text:
            q = self.search_text.lower()
            discovered = [w for w in discovered
                          if q in w.name.lower() or q in w.description.lower()]

        # Apply category filter to discovered
        if self.filter_mode == "work":
            discovered = [w for w in discovered if w.category == Category.WORK]
        elif self.filter_mode == "personal":
            discovered = [w for w in discovered if w.category == Category.PERSONAL]

        # Sort discovered: unread responses float to top, then by last user message time.
        # This prevents thinking threads from constantly reordering the list.
        last_seen = load_last_seen()
        def _has_unread(ws: Workstream) -> bool:
            sessions = self._sessions_for_ws(ws)
            best = _best_activity(sessions, last_seen)
            return best in (
                ThreadActivity.RESPONSE_FRESH,
                ThreadActivity.RESPONSE_READY,
                ThreadActivity.AWAITING_INPUT,
            )
        # Stable sort chain: first by user activity (newest first), then by unread (top).
        discovered.sort(key=lambda w: w.last_user_activity or w.updated_at or "", reverse=True)
        discovered.sort(key=lambda w: 0 if _has_unread(w) else 1)

        return manual + discovered

    def _get_filtered_streams(self) -> list[Workstream]:
        if self.filter_mode == "all":
            streams = list(self.store.active)
        elif self.filter_mode == "work":
            streams = [w for w in self.store.active if w.category == Category.WORK]
        elif self.filter_mode == "personal":
            streams = [w for w in self.store.active if w.category == Category.PERSONAL]
        elif self.filter_mode == "active":
            streams = [w for w in self.store.active if w.is_active]
        elif self.filter_mode == "stale":
            streams = self.store.stale()
        else:
            streams = list(self.store.active)

        if self.search_text:
            q = self.search_text.lower()
            streams = [w for w in streams if q in w.name.lower() or q in w.description.lower()]

        return self.store.sorted(streams, self.sort_mode)

    def _refresh_ws_table(self):
        table = self.query_one("#ws-table", DataTable)
        old_key = self._get_cursor_key(table)
        table.clear()

        items = self._get_unified_items()

        last_seen = load_last_seen()

        for ws in items:
            is_discovered = ws.origin == Origin.DISCOVERED

            # Status column
            if is_discovered:
                # Use best activity state across sessions
                ws_sessions = self._sessions_for_ws(ws)
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

            # Name + indicators
            indicators = ""
            if not is_discovered:
                indicators = _ws_indicators(ws, tmux_check=self._ws_has_tmux)
            thread_sessions = self._sessions_for_ws(ws)

            name_str = ws.name
            if indicators:
                name_str += "  " + indicators
            name_cell = Text(name_str)

            sess_count = len(thread_sessions) if thread_sessions else 0
            sess_cell = Text(str(sess_count) if sess_count else "", style=C_DIM)

            cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
            updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)

            table.add_row(status_cell, name_cell, sess_cell, cat_cell, updated_cell, key=ws.id)

        self._restore_cursor(table, old_key)
        self._update_all_bars()
        self._update_preview()

    def _selected_ws(self) -> Workstream | None:
        """Get the selected workstream (manual or discovered)."""
        table = self.query_one("#ws-table", DataTable)
        key = self._get_cursor_key(table)
        if not key:
            return None
        ws = self.store.get(key)
        if ws:
            return ws
        return next((w for w in self.discovered_ws if w.id == key), None)

    def _sessions_for_ws(self, ws: Workstream, include_archived_threads: bool = False) -> list[ClaudeSession]:
        """Find sessions for a workstream via thread_ids or directory matching.

        By default, individually archived sessions (archived_session_ids) are excluded.
        Pass include_archived_threads=True to get everything.
        """
        hidden_sids = set(ws.archived_session_ids) if not include_archived_threads else set()

        # Build effective thread list — use explicit thread_ids if available,
        # otherwise derive from directory-matched threads (manual workstreams).
        effective_tids = ws.thread_ids
        if not effective_tids and self.threads:
            ws_dirs = set()
            for link in ws.links:
                if link.kind in ("worktree", "file"):
                    expanded = os.path.expanduser(link.value).rstrip("/")
                    if os.path.isdir(expanded):
                        ws_dirs.add(expanded)
            explicit_sids = {link.value for link in ws.links if link.kind == "claude-session"}
            matched = set()
            for t in self.threads:
                if t.project_path.rstrip("/") in ws_dirs:
                    matched.add(t.thread_id)
                elif explicit_sids:
                    for s in t.sessions:
                        if s.session_id in explicit_sids or any(
                            s.session_id.startswith(sid) for sid in explicit_sids
                        ):
                            matched.add(t.thread_id)
                            break
            effective_tids = list(matched)

        if effective_tids:
            thread_map = {t.thread_id: t for t in self.threads}
            sessions = []
            seen = set()
            for tid in effective_tids:
                t = thread_map.get(tid)
                if t:
                    for s in t.sessions:
                        if s.session_id not in seen and s.session_id not in hidden_sids:
                            sessions.append(s)
                            seen.add(s.session_id)
            sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
            return sessions

        # Fallback when no threads loaded yet
        return _find_sessions_for_ws(ws, self.sessions)

    # ── Sessions & threads loading ──

    def _load_sessions(self):
        self._do_load_sessions()

    def _poll_sessions(self):
        """Lightweight periodic check for new sessions (no AI calls)."""
        self._do_poll_sessions()

    @work(thread=True, exclusive=True, group="poll_sessions")
    def _do_poll_sessions(self):
        threads = discover_threads()
        apply_cached_names(threads)

        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)

        # Only update UI if session state actually changed
        def _fingerprint(sl):
            return {(s.session_id, s.is_live, s.last_message_role, s.last_activity)
                    for s in sl}
        old_ids = {s.session_id for s in self.sessions}
        new_ids = {s.session_id for s in sessions}
        if _fingerprint(self.sessions) == _fingerprint(sessions):
            return

        discovered = get_discovered_workstreams(threads)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

        # If there are genuinely new sessions, trigger full load for AI naming
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

        # Phase 1: show cached data immediately
        discovered = get_discovered_workstreams(threads)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

        # Phase 2: name uncached threads (Haiku)
        named = name_uncached_threads(threads)
        if named > 0:
            apply_cached_names(threads)

        # Phase 3: synthesize workstreams for unassigned threads (Haiku)
        new_count = synthesize_workstreams(threads, self.store.active)
        if new_count > 0 or named > 0:
            discovered = get_discovered_workstreams(threads)
            self.call_from_thread(self._apply_synthesis, threads, discovered)

    def _apply_sessions(self, sessions: list[ClaudeSession],
                        threads: list[Thread], discovered: list[Workstream]):
        self.sessions = sessions
        self.threads = threads
        self.discovered_ws = discovered
        self._refresh_ws_table()
        self._refresh_sessions_table()
        # Notify all screens (modals like DetailScreen need this)
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _apply_synthesis(self, threads: list[Thread], discovered: list[Workstream]):
        self.threads = threads
        self.discovered_ws = discovered
        self._refresh_ws_table()

    def _refresh_sessions_table(self):
        table = self.query_one("#sessions-table", DataTable)
        old_key = self._get_cursor_key(table)
        table.clear()

        # Build reverse lookup: session_id -> workstream name
        ws_lookup: dict[str, str] = {}
        for ws in self.store.active:
            ws_sessions = _find_sessions_for_ws(ws, self.sessions)
            for s in ws_sessions:
                if s.session_id not in ws_lookup:
                    ws_lookup[s.session_id] = ws.name

        for session in self.sessions:
            # Live indicator prefix
            live_prefix = "\u25cf " if session.is_live else "  "
            title_text = live_prefix + session.display_name
            title_style = C_GREEN if session.is_live else ""
            title_cell = Text(title_text, style=title_style)

            # Show linked workstream or project name
            linked_ws = ws_lookup.get(session.session_id)
            if linked_ws:
                thread_cell = Text(linked_ws, style=C_CYAN)
            else:
                thread_cell = Text(_short_project(session.project_path), style=C_DIM)

            model_cell = Text(_short_model(session.model), style=C_DIM)
            tokens_cell = Text(session.tokens_display, style=_token_color(session.total_input_tokens + session.total_output_tokens))
            age_cell = Text(session.age, style=C_DIM)

            table.add_row(title_cell, thread_cell, model_cell, tokens_cell, age_cell,
                          key=session.session_id)

        self._restore_cursor(table, old_key)
        self._update_all_bars()

    def _selected_session(self) -> ClaudeSession | None:
        table = self.query_one("#sessions-table", DataTable)
        key = self._get_cursor_key(table)
        if key:
            return next((s for s in self.sessions if s.session_id == key), None)
        return None

    # ── Archived table ──

    def _refresh_archived_table(self):
        table = self.query_one("#archived-table", DataTable)
        old_key = self._get_cursor_key(table)
        table.clear()

        for ws in self.store.archived:
            status_cell = Text(STATUS_ICONS[ws.status], style=STATUS_THEME[ws.status])
            name_cell = Text(ws.name)
            sess_cell = Text("", style=C_DIM)
            cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
            updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)
            table.add_row(status_cell, name_cell, sess_cell, cat_cell, updated_cell, key=ws.id)

        self._restore_cursor(table, old_key)

    def _selected_archived(self) -> Workstream | None:
        table = self.query_one("#archived-table", DataTable)
        key = self._get_cursor_key(table)
        if key:
            return next((w for w in self.store.workstreams if w.id == key), None)
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

    def _get_selected_item(self, table: DataTable, getter):
        if table.row_count == 0:
            return None
        key = self._get_cursor_key(table)
        if key:
            return getter(key)
        return None

    # ── Primary action (Enter) ──

    def action_select_item(self):
        if self.view_mode == ViewMode.WORKSTREAMS:
            self._open_detail()
        elif self.view_mode == ViewMode.SESSIONS:
            self._resume_session()
        elif self.view_mode == ViewMode.ARCHIVED:
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
        """Resume a session selected from the preview pane."""
        idx = int(event.option_id)
        if idx < len(self._preview_sessions):
            session = self._preview_sessions[idx]
            # Mark as seen
            mark_thread_seen(session.session_id)
            ws = self._selected_ws()
            if ws:
                dirs = _ws_directories(ws)
                _resume_session_now(ws, session, dirs, self)
            else:
                self._suspend_claude(
                    ["claude", "--resume", session.session_id],
                    cwd=session.project_path,
                )

    def _open_detail(self):
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                DetailScreen(ws, self.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def _open_archived_detail(self):
        ws = self._selected_archived()
        if ws:
            self.push_screen(
                DetailScreen(ws, self.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    # ── Workstream actions ──

    def action_add(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return

        def on_result(ws: Workstream | None):
            if ws:
                self.store.add(ws)
                self.notify(f"Created: {ws.name}", timeout=2)
            self._refresh_ws_table()

        self.push_screen(AddScreen(), callback=on_result)

    def action_cycle_status(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            statuses = list(Status)
            idx = statuses.index(ws.status)
            ws.set_status(statuses[(idx + 1) % len(statuses)])
            self.store.update(ws)
            self._refresh_ws_table()
            self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_cycle_status_back(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            statuses = list(Status)
            idx = statuses.index(ws.status)
            ws.set_status(statuses[(idx - 1) % len(statuses)])
            self.store.update(ws)
            self._refresh_ws_table()
            self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_quick_note(self):
        """Quick note via modal — press n, type, enter."""
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            return

        def on_note(text: str | None):
            if not text or not text.strip():
                return
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"[{timestamp}] {text.strip()}"
            ws.notes = (ws.notes + "\n" + entry) if ws.notes else entry
            self.store.update(ws)
            self._refresh_ws_table()
            self.notify("Note added", timeout=1)

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
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                entry = f"[{timestamp}] {text}"
                ws.notes = (ws.notes + "\n" + entry) if ws.notes else entry
                self.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"Note added", timeout=1)

    def action_rename(self):
        """Rename the selected workstream."""
        if self.view_mode != ViewMode.WORKSTREAMS:
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
                ws.name = new_name
                self.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"Renamed to: {new_name}", timeout=1)

    def action_edit_notes(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                NotesScreen(ws, self.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def action_open_links(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            return
        if ws.links:
            if len(ws.links) == 1:
                _open_link(ws.links[0], ws=ws, app=self)
                self.notify(f"Opening {ws.links[0].label}...", timeout=2)
            else:
                self.push_screen(LinksScreen(ws, self.store))
        else:
            self.notify("No links", timeout=1)

    def action_archive(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if ws:
            self.store.archive(ws.id)
            self.notify(f"Archived: {ws.name}", timeout=2)
            self._refresh_ws_table()
            self._refresh_archived_table()

    def action_delete_item(self):
        if self.view_mode == ViewMode.SESSIONS:
            return

        ws = None
        if self.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
        elif self.view_mode == ViewMode.ARCHIVED:
            ws = self._selected_archived()

        if ws:
            def on_confirm(confirmed: bool):
                if confirmed:
                    self.store.remove(ws.id)
                    self.notify(f"Deleted: {ws.name}", timeout=2)
                    self._refresh_ws_table()
                    self._refresh_archived_table()

            self.push_screen(
                ConfirmScreen(f"[bold {C_RED}]Delete[/bold {C_RED}] [bold]{ws.name}[/bold]?"),
                callback=on_confirm,
            )

    def action_unarchive(self):
        if self.view_mode != ViewMode.ARCHIVED:
            return
        ws = self._selected_archived()
        if ws:
            self.store.unarchive(ws.id)
            self.notify(f"Restored: {ws.name}", timeout=2)
            self._refresh_ws_table()
            self._refresh_archived_table()

    # ── Brain dump ──

    def action_brain_dump(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
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
                    self.store.add(ws)
                self.notify(f"Added {len(tasks)} workstreams", timeout=2)
                self._refresh_ws_table()

        self.push_screen(BrainPreviewScreen(tasks), callback=on_confirm)

    # ── Spawn & resume ──

    def action_spawn(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        ok, err = _launch_orch_claude(ws, store=self.store)
        if ok:
            self.notify("Session spawned", timeout=2)
        else:
            self.notify(f"Spawn failed: {err}", severity="error", timeout=4)

    def action_resume(self):
        if self.view_mode == ViewMode.WORKSTREAMS:
            ws = self._selected_ws()
            if ws:
                _do_resume(ws, self, self.sessions)
        elif self.view_mode == ViewMode.SESSIONS:
            self._resume_session()

    def _resume_session(self):
        session = self._selected_session()
        if not session:
            self.notify("No session selected", timeout=2)
            return

        ws = self._find_ws_for_session(session)
        if ws:
            _launch_orch_claude(ws, session_id=session.session_id, cwd=session.project_path)
        else:
            self._suspend_claude(
                ["claude", "--resume", session.session_id],
                cwd=session.project_path,
            )

    def _suspend_claude(self, cmd: list[str], cwd: str | None = None):
        """Suspend the TUI and run a claude command in the foreground."""
        with self.suspend():
            subprocess.run(cmd, cwd=cwd)

    def _find_ws_for_session(self, session: ClaudeSession) -> Workstream | None:
        """Reverse-lookup: find a workstream that owns this session."""
        for ws in self.store.active:
            # Check explicit session links
            for link in ws.links:
                if link.kind == "claude-session" and (
                    link.value == session.session_id or
                    session.session_id.startswith(link.value)
                ):
                    return ws
            # Check directory match
            for link in ws.links:
                if link.kind in ("worktree", "file"):
                    expanded = os.path.expanduser(link.value).rstrip("/")
                    if os.path.isdir(expanded) and session.project_path.rstrip("/") == expanded:
                        return ws
        return None

    # ── Link action (context-dependent) ──

    def action_link_action(self):
        if self.view_mode == ViewMode.WORKSTREAMS:
            self._add_link_to_ws()
        elif self.view_mode == ViewMode.SESSIONS:
            self._link_session_to_ws()

    def _add_link_to_ws(self):
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return

        def on_link(link: Link | None):
            if link:
                ws.links.append(link)
                ws.touch()
                self.store.update(ws)
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
                self.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"Linked session to {ws.name}", timeout=2)

        self.push_screen(LinkSessionScreen(self.store, session), callback=on_ws)

    # ── Filter & sort ──

    def action_filter(self, mode: str):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        self.filter_mode = mode
        self._refresh_ws_table()

    def action_sort(self, mode: str):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        self.sort_mode = mode
        self._refresh_ws_table()

    def action_search(self):
        if self.view_mode != ViewMode.WORKSTREAMS:
            return
        self.query_one("#command-input", CommandInput).display = False
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = True
        search_input.value = self.search_text
        search_input.focus()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted):
        self.search_text = event.value.strip()
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = False
        self._refresh_ws_table()
        self._active_table().focus()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed):
        self.search_text = event.value.strip()
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
        parts = cmd_text.strip().split(None, 1)
        if not parts:
            return

        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        ws = self._selected_ws() if self.view_mode == ViewMode.WORKSTREAMS else None

        # View switching
        if cmd in ("workstreams", "ws"):
            self.view_mode = ViewMode.WORKSTREAMS
            self._apply_view()
        elif cmd == "sessions":
            self.view_mode = ViewMode.SESSIONS
            self._apply_view()
        elif cmd == "archived":
            self.view_mode = ViewMode.ARCHIVED
            self._apply_view()

        # Status
        elif cmd in ("status", "st") and ws:
            if not arg:
                self.notify("Usage: status <queued|in-progress|awaiting-review|done|blocked>", timeout=3)
                return
            try:
                ws.set_status(Status(arg))
                self.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)
            except ValueError:
                self.notify(f"Invalid status: {arg}", severity="error", timeout=2)

        # Link
        elif cmd in ("link", "ln") and ws:
            if ":" not in arg:
                self.notify("Usage: link kind:value (e.g. ticket:UB-1234)", severity="error", timeout=2)
                return
            kind, value = arg.split(":", 1)
            if kind not in LINK_KINDS:
                self.notify(f"Unknown kind: {kind}", severity="error", timeout=2)
                return
            ws.add_link(kind=kind, value=value, label=kind)
            self.store.update(ws)
            self._refresh_ws_table()
            self.notify(f"Added {kind} link to {ws.name}", timeout=2)

        # Note
        elif cmd in ("note", "n") and ws:
            if not arg:
                self.notify("Usage: note <text>", timeout=2)
                return
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"[{timestamp}] {arg}"
            ws.notes = (ws.notes + "\n" + entry) if ws.notes else entry
            self.store.update(ws)
            self.notify(f"Note added to {ws.name}", timeout=2)

        # Archive
        elif cmd in ("archive", "a") and ws:
            self.store.archive(ws.id)
            self._refresh_ws_table()
            self._refresh_archived_table()
            self.notify(f"Archived: {ws.name}", timeout=2)

        # Unarchive
        elif cmd in ("unarchive", "ua"):
            if self.view_mode == ViewMode.ARCHIVED:
                self.action_unarchive()

        # Delete
        elif cmd in ("delete", "del"):
            self.action_delete_item()

        # Search
        elif cmd == "search":
            self.search_text = arg
            self._refresh_ws_table()

        # Sort
        elif cmd == "sort":
            valid = ("status", "updated", "created", "category", "name")
            if arg in valid:
                self.sort_mode = arg
                self._refresh_ws_table()
            else:
                self.notify(f"Sort by: {', '.join(valid)}", severity="error", timeout=2)

        # Filter
        elif cmd in ("filter", "f"):
            valid = ("all", "work", "personal", "active", "stale")
            if arg in valid:
                self.filter_mode = arg
                self._refresh_ws_table()
            else:
                self.notify(f"Filter: {', '.join(valid)}", severity="error", timeout=2)

        # Spawn
        elif cmd == "spawn":
            self.action_spawn()

        # Resume
        elif cmd == "resume":
            self.action_resume()

        # Export
        elif cmd == "export":
            self._do_export(arg)

        # Brain
        elif cmd == "brain":
            if arg:
                self._do_brain(arg)
            else:
                self.action_brain_dump()

        # Help
        elif cmd == "help":
            self.push_screen(HelpScreen())

        else:
            self.notify(f"Unknown command: {cmd}", severity="error", timeout=2)

    def _do_export(self, path: str = ""):
        streams = self.store.active
        output = path or os.path.expanduser("~/workstreams/active.md")

        lines = [
            "# Active Workstreams",
            f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]
        for cat in Category:
            cat_streams = [w for w in streams if w.category == cat]
            if not cat_streams:
                continue
            lines.append(f"## {cat.value.title()}")
            lines.append("")
            cat_streams = self.store.sorted(cat_streams, "status")
            for ws in cat_streams:
                ws_icon = STATUS_ICONS[ws.status]
                lines.append(f"### {ws_icon} {ws.name}")
                lines.append(f"**Status:** {ws.status.value} | **Updated:** {_relative_time(ws.updated_at)}")
                if ws.description:
                    lines.append(f"\n{ws.description}")
                if ws.links:
                    lines.append("\n**Links:**")
                    for lnk in ws.links:
                        if lnk.kind == "url":
                            lines.append(f"- [{lnk.label}]({lnk.value})")
                        else:
                            lines.append(f"- `{lnk.kind}`: {lnk.value}")
                if ws.notes:
                    lines.append(f"\n**Notes:**\n{ws.notes}")
                lines.append("")

        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text("\n".join(lines) + "\n")
        self.notify(f"Exported {len(streams)} workstreams to {output}", timeout=3)

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
        if paths != self._tmux_paths or names != self._tmux_names:
            self._tmux_paths = paths
            self._tmux_names = names
            self._refresh_ws_table()

    def _ws_has_tmux(self, ws: Workstream) -> bool:
        for link in ws.links:
            if link.kind == "worktree":
                expanded = os.path.expanduser(link.value).rstrip("/")
                for tmux_path in self._tmux_paths:
                    if tmux_path == expanded or tmux_path.startswith(expanded + "/"):
                        return True
        spawn_name = f"\U0001f916{ws.name[:18]}"
        if spawn_name in self._tmux_names:
            return True
        if ws.name[:20] in self._tmux_names:
            return True
        return False

    # ── Other ──

    def action_refresh(self):
        self.store.load()
        self._refresh_ws_table()
        self._refresh_archived_table()
        self._load_sessions()
        self._poll_tmux()
        self.notify("Refreshed", timeout=1)

    def action_help(self):
        self.push_screen(HelpScreen())

    def _on_return_from_modal(self):
        self.store.load()
        self._refresh_ws_table()
        self._refresh_archived_table()
        self._update_preview()


if __name__ == "__main__":
    OrchestratorApp().run()
