"""Claude Orchestrator TUI — central hub for managing workstreams and Claude sessions.

This is the thin Textual shell. Business logic lives in state.py,
modal screens in screens.py, rendering helpers in rendering.py,
and external process actions in actions.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time as _time
from pathlib import Path

_perf_log = logging.getLogger("orch.perf")
_PERF_ENABLED = os.environ.get("ORCH_PERF_LOG", "")
if _PERF_ENABLED:
    _perf_handler = logging.FileHandler(
        Path.home() / ".cache" / "claude-orchestrator" / "perf.log"
    )
    _perf_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _perf_log.addHandler(_perf_handler)
    _perf_log.setLevel(logging.WARNING)

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.theme import Theme
from textual.widgets import (
    Input,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual._widget_navigation import find_next_enabled_no_wrap

# Monkeypatch OptionList to never wrap at top/bottom
def _option_list_cursor_up(self) -> None:
    result = find_next_enabled_no_wrap(
        self.options, anchor=self.highlighted, direction=-1,
    )
    if result is not None:
        self.highlighted = result

def _option_list_cursor_down(self) -> None:
    result = find_next_enabled_no_wrap(
        self.options, anchor=self.highlighted, direction=1,
    )
    if result is not None:
        self.highlighted = result

OptionList.action_cursor_up = _option_list_cursor_up
OptionList.action_cursor_down = _option_list_cursor_down

from config import build_app_bindings
from models import (
    Category, Link, Store, Workstream,
    _relative_time,
)
from sessions import ClaudeSession, invalidate_live_session_cache, ensure_rust_engine_running
from threads import Thread, ThreadActivity, session_activity, mark_thread_seen, discover_threads
from thread_namer import apply_cached_names, name_uncached_threads, title_sessions, get_session_title, refresh_thread_titles
from session_bridge import SessionBridge
from watcher import SessionWatcher
from workstream_synthesizer import (
    synthesize_workstreams, get_discovered_workstreams,
)
from description_refresher import refresh_descriptions

from rendering import (
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_GREEN, C_MID, C_PURPLE, C_RED, C_YELLOW,
    BG_BASE, BG_CHROME, BG_RAISED,
    _token_color, _token_color_markup,
    _category_markup,
    _is_session_seen, _is_today, _any_session_today,
    _render_session_option, _render_ws_option, _session_title,
    _rich_escape,
)
from state import AppState, TabManager
from actions import (
    ws_directories,
    do_resume, resume_session_now, open_link,
    refresh_liveness,
)
from screens import (
    SessionsChanged,
    HelpScreen, QuickNoteScreen, TodoScreen, LinksScreen,
    AddScreen, DetailScreen, BrainDumpScreen, BrainPreviewScreen,
    AddLinkScreen, LinkSessionScreen, ConfirmScreen,
    RepoPickerScreen, WorkstreamPickerScreen, CurrentSessionsScreen, _SENTINEL_NEW,
)


def _divider_option(width: int = 40) -> Option:
    """A disabled Option that renders as a dim 'earlier' divider line."""
    pad = max(1, (width - 10) // 2)
    line = f"[{C_FAINT}]{'─' * pad} earlier {'─' * pad}[/{C_FAINT}]"
    return Option(line, id="__sep__", disabled=True)


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
    #top-bar {{
        height: auto; max-height: 3; padding: 0 2; background: {BG_RAISED}; dock: top;
    }}
    #filter-bar {{
        height: 1; padding: 0 1; background: {BG_CHROME}; dock: top;
    }}
    #summary-bar {{
        height: 1; padding: 0 1; background: {BG_CHROME}; color: {C_DIM}; dock: bottom;
    }}
    #main-content {{ height: 1fr; }}
    #ws-table {{
        width: 3fr; height: 1fr; margin: 0; padding: 0;
        border: none; background: {BG_BASE};
    }}
    #preview-pane {{
        width: 2fr; min-width: 36; border-left: blank;
        padding: 1 2; background: {BG_BASE};
    }}
    #preview-content {{ width: 100%; }}
    #preview-sessions {{
        height: auto; max-height: 16; width: 100%; margin: 0; padding: 0;
    }}
    #search-input, #note-input, #rename-input {{
        dock: bottom; height: 1; display: none; border: none; background: {BG_BASE};
    }}
    #search-input:focus, #note-input:focus, #rename-input:focus {{
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
            primary="#58a6ff",
            secondary="#d2a8ff",
            background="#000000",
            surface="#000000",
            panel="#000000",
            foreground="#e6edf3",
            accent="#56d4dd",
            warning="#e3b341",
            error="#ffa198",
            success="#56d364",
            dark=True,
            luminosity_spread=0.0,
            text_alpha=1.0,
            variables={
                "scrollbar": "#161b22",
                "scrollbar-hover": "#484f58",
                "scrollbar-active": "#58a6ff",
                "scrollbar-background": "#000000",
                "scrollbar-background-hover": "#0d1117",
                "scrollbar-background-active": "#0d1117",
                "scrollbar-corner-color": "#000000",
                "footer-background": BG_CHROME,
                "footer-foreground": "#6e7681",
                "block-cursor-text-style": "bold",
                "border": "#161b22",
                "border-blurred": "#161b22",
                "input-cursor-background": "#58a6ff",
                "input-cursor-foreground": "#000000",
                "input-selection-background": "#58a6ff 30%",
            },
        ))
        self.theme = "mellow"
        self.state = AppState(Store())
        self.tabs = TabManager()
        self._throbber_timer = None
        self._session_watcher: SessionWatcher | None = None
        self._session_bridge: SessionBridge | None = None
        self._refresh_pending = False  # debounce flag for _refresh_ws_table
        self._preview_ws_id: str | None = None  # track current preview to skip redundant updates
        self._detached_sessions: dict[str, dict] = {}  # session_id -> {ws, start_time, jsonl}
        self._detail_screen_active: bool = False  # True when a DetailScreen or CurrentSessionsScreen is pushed
        self._detail_screen_cache: dict[str, DetailScreen] = {}  # ws_id -> cached screen
        self._current_sessions_screen: CurrentSessionsScreen | None = None
        self._tab_active_session: dict[str, str] = {}  # ws_id -> session_id (for tab-switch resume)
        self._tab_switch_in_progress: bool = False  # suppress _on_detail_dismissed during tab switch
        self._last_liveness_check: float = 0.0  # rate limiter for liveness checks
        self._liveness_deferred: bool = False  # trailing-edge pending flag
        self._ws_pending_session: dict[str, str] = {}  # ws_id -> session_id for reuse on "c"

    def on_key(self, event) -> None:
        if event.key in ("ctrl+b", "ctrl+x"):
            event.prevent_default()
            event.stop()
            if event.key == "ctrl+b":
                self.action_next_tab()
            else:
                self.action_prev_tab()
            return
        if event.key in ("ctrl+j", "ctrl+k"):
            if not self._detail_screen_active:
                return
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
        elif event.key == "ctrl+z":
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, 'action_zoom_panel'):
                screen.action_zoom_panel()
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
        yield Static("", id="top-bar")
        yield Static("", id="filter-bar")
        with Horizontal(id="main-content"):
            yield OptionList(id="ws-table")
            with VerticalScroll(id="preview-pane"):
                yield Static("", id="preview-content")
                yield OptionList(id="preview-sessions")
        yield SearchInput(placeholder="Search...", id="search-input")
        yield QuickNoteInput(placeholder="note: ", id="note-input")
        yield RenameInput(placeholder="rename: ", id="rename-input")
        yield Static("", id="summary-bar")

    def on_mount(self):
        ws_table = self.query_one("#ws-table", OptionList)

        self._refresh_ws_table()
        self._load_sessions()

        self.query_one("#preview-sessions", OptionList).display = False

        # ── Staggered timers ──
        # Spread 30s polling timers across time to prevent burst every 30s.
        # Each timer fires once immediately at its offset, then every 30s.
        self._poll_tmux()
        self.set_interval(30, self._poll_tmux)

        self.set_timer(5, self._start_git_polling)
        self.set_timer(10, self._start_worktree_polling)

        # ── Session change notifications ──
        # Try Rust daemon's pipe first (instant, no debouncing needed).
        # Fall back to Python watchdog if daemon isn't running.
        self._session_bridge = SessionBridge()
        if self._session_bridge.available:
            self._session_bridge.start(
                callback=lambda: self.call_from_thread(self._on_rust_engine_update)
            )
        else:
            # Try to start the Rust daemon for next time
            ensure_rust_engine_running()
            # Fall back to Python watchdog
            self._session_watcher = SessionWatcher(
                on_liveness=lambda: self.call_from_thread(self._on_liveness_file_change),
                on_content=lambda: self.call_from_thread(self._on_session_file_change),
                debounce=1.0,
                content_debounce=2.0,
            )
            self._session_watcher.start()

        self.set_timer(15, self._start_session_polling)
        self.set_timer(20, self._start_liveness_backstop)

        self._throbber_timer = self.set_interval(0.15, self._tick_throbber)

        ws_table.focus()

    def on_unmount(self):
        if self._session_bridge:
            self._session_bridge.stop()
        if self._session_watcher:
            self._session_watcher.stop()

    def _tick_throbber(self):
        """Advance the throbber frame and refresh preview if any sessions are thinking."""
        preview = self.state.preview_sessions
        if preview and any(
            session_activity(s, self.state.last_seen_cache) == ThreadActivity.THINKING
            for s in preview
        ):
            self.state.throbber_frame += 1
            self._refresh_preview_sessions()

    def _start_git_polling(self):
        self._poll_git_status()
        self.set_interval(30, self._poll_git_status)

    def _start_worktree_polling(self):
        self._poll_worktrees()
        self.set_interval(30, self._poll_worktrees)

    def _start_session_polling(self):
        self._poll_sessions()
        self.set_interval(30, self._poll_sessions)

    def _start_liveness_backstop(self):
        self._refresh_session_liveness()
        self.set_interval(30, self._refresh_session_liveness)

    # ── Active table helper ──

    def _active_table(self) -> OptionList:
        return self.query_one("#ws-table", OptionList)

    # ── Tab switching (placeholder — tabs will be wired in a later step) ──

    def action_next_tab(self):
        """Ctrl+Tab: cycle to next tab."""
        if self.tabs.next_tab():
            self._apply_tab_switch()

    def action_prev_tab(self):
        """Ctrl+Shift+Tab: cycle to previous tab."""
        if self.tabs.prev_tab():
            self._apply_tab_switch()

    def action_close_tab(self):
        """Close the active tab (cannot close Home)."""
        closed = self.tabs.close_active_tab()
        if closed:
            # Evict cached screen for closed tab
            self._tab_active_session.pop(closed, None)
            if closed in self._detail_screen_cache:
                screen_name = f"detail:{closed}"
                try:
                    self.uninstall_screen(screen_name)
                except Exception:
                    pass
                del self._detail_screen_cache[closed]
            self._apply_tab_switch()

    def _apply_tab_switch(self):
        """Handle tab switch — open DetailScreen, CurrentSessionsScreen, or return to Home."""
        tab = self.tabs.active_tab
        self._sync_tab_bar()
        if tab.ws_id:
            ws = self.state.get_ws(tab.ws_id)
            if ws:
                self._push_detail_for_tab(ws)
        elif tab.id == "current_sessions":
            self._push_current_sessions_screen()
        else:
            # Workstreams tab: pop everything above Home (CSS + any stale Detail).
            # pop_screen() is synchronous so we can call dismiss() in a loop.
            while len(self.screen_stack) > 1:
                self.screen_stack[-1].dismiss()
            self._detail_screen_active = False

    def _push_detail_for_tab(self, ws: Workstream):
        """Switch to ws's DetailScreen, keeping the stack clean.

        When CSS is on top, we dismiss it and defer a second dismiss for the
        now-exposed stale Detail before pushing the new tab's Detail.  This
        keeps the Textual stack at exactly [Home, Detail] at all times.
        """
        from claude_session_screen import ClaudeSessionScreen as CSS
        self._tab_switch_in_progress = True

        if self._detail_screen_active:
            if isinstance(self.screen, CSS):
                # Leaving a session: record the session ID for auto-resume on return.
                # TerminalWidget.on_unmount calls detach_persistent() so tmux stays alive.
                self._tab_active_session[self.screen._ws.id] = self.screen._session_id
                self.screen.dismiss()  # pops CSS; stale Detail is now on top
                # Defer: once CSS is visually settled, dismiss stale Detail then push new one.
                target_ws_id = ws.id
                self.call_after_refresh(
                    lambda w=ws, tid=target_ws_id: self._finish_tab_switch_after_css(w, tid)
                )
                return
            else:
                self.screen.dismiss()

        self._finish_tab_switch(ws)

    def _finish_tab_switch_after_css(self, ws: Workstream, target_ws_id: str) -> None:
        """Deferred: dismiss the stale Detail left by CSS, then push the new tab's Detail.

        The target_ws_id guard handles the case where the user switched tabs again
        before this callback fired — in that case the new switch already cleaned up.
        """
        if self.tabs.active_tab.ws_id != target_ws_id:
            return  # user moved on; the new switch already handled cleanup
        if isinstance(self.screen, DetailScreen):
            self.screen.dismiss()  # dismiss stale Detail (_on_detail_dismissed suppressed)
        self._finish_tab_switch(ws)

    def _push_current_sessions_screen(self) -> None:
        """Push the CurrentSessionsScreen, dismissing any active overlay first."""
        from claude_session_screen import ClaudeSessionScreen as CSS
        self._tab_switch_in_progress = True
        if self._detail_screen_active:
            if isinstance(self.screen, CSS):
                self._tab_active_session[self.screen._ws.id] = self.screen._session_id
                self.screen.dismiss()
                self.call_after_refresh(self._finish_current_sessions_switch)
                return
            else:
                self.screen.dismiss()
        self._finish_current_sessions_switch()

    def _finish_current_sessions_switch(self) -> None:
        """Core: install/push the CurrentSessionsScreen."""
        self._detail_screen_active = True
        if self._current_sessions_screen is None:
            self._current_sessions_screen = CurrentSessionsScreen()
            self.install_screen(self._current_sessions_screen, "current_sessions_screen")
        self.push_screen("current_sessions_screen", callback=lambda _: self._on_detail_dismissed())
        self.call_after_refresh(lambda: setattr(self, "_tab_switch_in_progress", False))
        self._sync_tab_bar()

    def _finish_tab_switch(self, ws: Workstream) -> None:
        """Core: install/push the DetailScreen for ws and schedule optional session resume."""
        self._detail_screen_active = True
        screen_name = f"detail:{ws.id}"
        if ws.id not in self._detail_screen_cache:
            screen = DetailScreen(ws, self.state.store)
            self._detail_screen_cache[ws.id] = screen
            self.install_screen(screen, screen_name)
        self.push_screen(screen_name, callback=lambda _: self._on_detail_dismissed())
        self.call_after_refresh(lambda: setattr(self, '_tab_switch_in_progress', False))
        self._sync_tab_bar()
        sid = self._tab_active_session.pop(ws.id, None)
        if sid:
            self._auto_resume_tab_session(ws, sid)

    @work(thread=False)
    async def _auto_resume_tab_session(self, ws: Workstream, session_id: str) -> None:
        """Async worker: check if a tmux session is still alive, then resume it.

        The ws-ID guard prevents a stale callback from pushing a session screen
        onto the wrong tab if the user navigated away before this worker ran.
        """
        from terminal import TerminalWidget
        alive = await asyncio.get_running_loop().run_in_executor(
            None, TerminalWidget.tmux_session_alive, session_id
        )
        if alive and self.tabs.active_tab.ws_id == ws.id:
            await self.launch_claude_session(ws, session_id=session_id)

    def _on_detail_dismissed(self):
        """Called when a DetailScreen is dismissed (back to Home).

        Skips reset if a tab switch is in progress (the dismiss/push pair fires the
        callback between the pop and the push, when self.screen is temporarily Home).
        """
        if self._tab_switch_in_progress:
            return
        self._detail_screen_active = False
        self.tabs.switch_to(0)
        self._sync_tab_bar()
        self._on_return_from_modal()

    def _sync_tab_bar(self):
        """Re-render the top bar to reflect current tab state."""
        self._update_all_bars()
        # Also update tab bar on DetailScreen (if active)
        try:
            self.screen.query_one("#detail-tab-bar", Static).update(self._render_tab_bar())
        except Exception:
            pass

    def _render_tab_bar(self) -> str:
        """Render the tab bar line as Rich markup."""
        parts = []
        for i, tab in enumerate(self.tabs.tabs):
            prefix = f"{tab.icon} " if tab.icon else ""
            label = tab.label[:20] + "\u2026" if len(tab.label) > 20 else tab.label
            is_permanent = tab.ws_id is None
            is_active = i == self.tabs.active_idx
            if is_active and is_permanent:
                parts.append(f"[bold italic {C_MID} on {BG_BASE}] {prefix}{_rich_escape(label)} [/]")
            elif is_active:
                parts.append(f"[bold {C_BLUE} on {BG_BASE}] {prefix}{_rich_escape(label)} [/]")
            elif is_permanent:
                parts.append(f"[italic {C_FAINT} on {BG_RAISED}] {prefix}{_rich_escape(label)} [/]")
            else:
                parts.append(f"[{C_DIM} on {BG_RAISED}] {prefix}{_rich_escape(label)} [/{C_DIM} on {BG_RAISED}]")
            if i < len(self.tabs.tabs) - 1:
                parts.append(f"[{C_FAINT}]\u2502[/{C_FAINT}]")
        return "".join(parts)

    # ── Navigation ──

    def action_cursor_down(self):
        self._active_table().action_cursor_down()

    def action_cursor_up(self):
        self._active_table().action_cursor_up()

    def action_cursor_top(self):
        table = self._active_table()
        if table.option_count > 0:
            table.highlighted = 0

    def action_cursor_bottom(self):
        table = self._active_table()
        if table.option_count > 0:
            table.highlighted = table.option_count - 1

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
        """Widget IDs for focusable panels."""
        panels = ["ws-table"]
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

    def _on_rust_engine_update(self):
        """Callback from the Rust session engine's notification pipe.

        The daemon has already parsed the changed files and written to SQLite.
        We just need to re-read from the DB (3ms) and refresh the UI.
        Rate-limited to avoid flooding the UI thread during heavy activity.
        """
        if _PERF_ENABLED:
            _perf_log.warning("_on_rust_engine_update: bridge callback fired")
        now = _time.monotonic()
        if now - getattr(self, '_last_rust_update', 0) < 1.0:
            # Schedule a trailing-edge update if not already pending
            if not getattr(self, '_rust_update_pending', False):
                self._rust_update_pending = True
                self.set_timer(1.0, self._fire_deferred_rust_update)
            return
        self._last_rust_update = now
        self._rust_update_pending = False
        self._do_refresh_from_db()

    def _fire_deferred_rust_update(self):
        """Trailing-edge fire for rate-limited Rust engine updates."""
        self._rust_update_pending = False
        self._last_rust_update = _time.monotonic()
        self._do_refresh_from_db()

    @staticmethod
    def _session_fingerprint(sessions):
        return {(s.session_id, s.is_live, s.last_message_role, s.last_activity) for s in sessions}

    _db_refresh_running = False  # guard against overlapping workers

    @work(thread=True, exclusive=True, group="rust_engine")
    def _do_refresh_from_db(self):
        """Re-read sessions from SQLite and update the UI."""
        # Textual's exclusive=True cancels the *future* but not the thread.
        # Guard with a flag so overlapping workers skip rather than pile up
        # and fight for the GIL (which inflated 9ms work to 1700ms+).
        if self._db_refresh_running:
            if _PERF_ENABLED:
                _perf_log.warning("_do_refresh_from_db: skipped (already running)")
            return
        self._db_refresh_running = True
        try:
            self.__do_refresh_from_db_inner()
        finally:
            self._db_refresh_running = False

    def __do_refresh_from_db_inner(self):
        _t0 = _time.monotonic() if _PERF_ENABLED else 0
        threads = discover_threads()
        if _PERF_ENABLED:
            _perf_log.warning("_do_refresh_from_db: discover_threads %.1fms", (_time.monotonic() - _t0) * 1000)
        _t1 = _time.monotonic() if _PERF_ENABLED else 0
        apply_cached_names(threads)
        if _PERF_ENABLED:
            _perf_log.warning("_do_refresh_from_db: apply_cached_names %.1fms", (_time.monotonic() - _t1) * 1000)

        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)

        # Skip UI update if nothing actually changed
        if self._session_fingerprint(self.state.sessions) == self._session_fingerprint(sessions):
            if _PERF_ENABLED:
                _perf_log.warning("_do_refresh_from_db: %.1fms (skipped, no changes)",
                                  (_time.monotonic() - _t0) * 1000)
            return

        _t2 = _time.monotonic() if _PERF_ENABLED else 0
        from workstream_synthesizer import get_discovered_workstreams
        discovered = get_discovered_workstreams(threads)
        if _PERF_ENABLED:
            _perf_log.warning("_do_refresh_from_db: get_discovered_workstreams %.1fms", (_time.monotonic() - _t2) * 1000)
            _perf_log.warning("_do_refresh_from_db: %.1fms total (changed, updating UI)",
                              (_time.monotonic() - _t0) * 1000)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

    def _on_liveness_file_change(self):
        """Watcher callback for session .json changes (start/stop).

        Invalidates the live-session cache so the next check sees the
        new session immediately, then triggers a rate-limited liveness refresh.
        """
        invalidate_live_session_cache()
        self._refresh_session_liveness()

    def _on_session_file_change(self):
        """Watcher callback for JSONL content changes.

        Triggers a rate-limited liveness refresh which tail-reads active
        sessions to pick up new messages/status. Does NOT invalidate the
        live-session cache (content changes don't affect session start/stop).
        """
        self._refresh_session_liveness()

    def _refresh_session_liveness(self):
        """Rate-limited liveness refresh — at most once per 2 seconds.

        Prevents the watcher from flooding the main thread with liveness
        checks when multiple active sessions are writing concurrently.
        """
        now = _time.monotonic()
        elapsed = now - self._last_liveness_check
        if elapsed < 2.0:
            # Already checked recently; schedule trailing-edge fire
            if not self._liveness_deferred:
                self._liveness_deferred = True
                self.set_timer(2.0 - elapsed, self._fire_deferred_liveness)
            return
        self._last_liveness_check = now
        self._liveness_deferred = False
        self._do_refresh_liveness()

    def _fire_deferred_liveness(self):
        """Trailing-edge fire for rate-limited liveness."""
        self._liveness_deferred = False
        self._last_liveness_check = _time.monotonic()
        self._do_refresh_liveness()

    @work(thread=True, exclusive=True, group="liveness")
    def _do_refresh_liveness(self):
        changed = self.state.refresh_liveness()
        if changed:
            self.call_from_thread(self._apply_liveness_change)

    def _apply_liveness_change(self):
        # Skip main table rebuild when DetailScreen covers it — defer to return
        if not self._detail_screen_active:
            self._preview_ws_id = None  # force preview refresh on liveness change
            with self.batch_update():
                self._refresh_ws_table_debounced()
        # Always notify screens so DetailScreen picks up liveness changes
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _update_preview(self, force: bool = False):
        if not self.state.preview_visible:
            return
        ws = self._selected_ws()
        ws_id = ws.id if ws else None
        if not force and ws_id == self._preview_ws_id:
            return
        self._preview_ws_id = ws_id
        self._render_ws_preview(ws)

    @staticmethod
    def _hint_line(pairs: list[tuple[str, str]]) -> str:
        parts = [f"[{C_YELLOW}]{key}[/{C_YELLOW}] {label}" for key, label in pairs]
        return f"[{C_DIM}]{' \u00b7 '.join(parts)}[/{C_DIM}]"

    def _nav_hints(self) -> str:
        return self._hint_line([("j/k", "navigate"), ("Tab", "views"), ("?", "help")])

    def _render_ws_preview(self, ws: Workstream | None, archived: bool = False):
        try:
            content = self.query_one("#preview-content", Static)
            olist = self.query_one("#preview-sessions", OptionList)
        except Exception:
            return
        if not ws:
            content.update(f"[{C_DIM}]Select a workstream[/{C_DIM}]\n\n{self._nav_hints()}")
            olist.display = False
            self.state.preview_sessions = []
            return

        lines = []
        lines.append(f"[bold {C_PURPLE}]{_rich_escape(ws.name)}[/bold {C_PURPLE}]")
        lines.append(f"{_category_markup(ws.category)}")
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
                ("r", "resume"), ("c", "new session"),
                ("n", "note"), ("o", "open"),
            ]))

        content.update("\n".join(lines))

        self.state.preview_sessions = ws_sessions
        self.state.last_seen_cache = self.state.get_last_seen()
        if ws_sessions:
            olist.display = True
            self._refresh_preview_sessions()
        else:
            olist.display = False

    def _refresh_preview_sessions(self):
        olist = self.query_one("#preview-sessions", OptionList)
        highlighted = olist.highlighted
        options = []
        sep_inserted = False
        for i, s in enumerate(self.state.preview_sessions):
            act = session_activity(s, self.state.last_seen_cache)
            seen = _is_session_seen(s, self.state.last_seen_cache)
            if not sep_inserted and i > 0 and not _is_today(s.last_activity or s.started_at or ""):
                options.append(_divider_option(38))
                sep_inserted = True
            options.append(Option(
                _render_session_option(s, act, self.state.throbber_frame, title_width=35, seen=seen),
                id=str(i),
            ))
        # Use in-place updates when the session list structure is unchanged —
        # clear_options() + add_options() remounts every option widget and
        # triggers a full CSS matching pass, which is expensive at 10fps.
        if olist.option_count == len(options):
            for idx, opt in enumerate(options):
                try:
                    existing = olist.get_option_at_index(idx)
                    if existing.prompt != opt.prompt:
                        olist.replace_option_prompt_at_index(idx, opt.prompt)
                except Exception:
                    olist.replace_option_prompt_at_index(idx, opt.prompt)
        else:
            olist.clear_options()
            olist.add_options(options)
            if highlighted is not None and highlighted < len(options):
                olist.highlighted = highlighted

    @on(OptionList.OptionHighlighted, "#ws-table")
    def on_ws_highlighted(self, event: OptionList.OptionHighlighted):
        self._debounce_preview()

    def _debounce_preview(self):
        """Debounce preview updates during rapid cursor movement."""
        if hasattr(self, '_preview_timer') and self._preview_timer:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.05, self._update_preview)

    # ── Bar rendering ──

    def _update_all_bars(self):
        try:
            lines = [
                self._render_tab_bar(),
                self._render_status_bar(),
            ]
            top = "\n".join(lines)
            if top != getattr(self, '_last_top_bar', ''):
                self._last_top_bar = top
                self.query_one("#top-bar", Static).update(top)
            filters = self._render_filter_bar()
            if filters != getattr(self, '_last_filter_bar', ''):
                self._last_filter_bar = filters
                self.query_one("#filter-bar", Static).update(filters)
            summary = self._render_summary_bar()
            if summary != getattr(self, '_last_summary_bar', ''):
                self._last_summary_bar = summary
                self.query_one("#summary-bar", Static).update(summary)
        except Exception:
            pass

    def _render_status_bar(self) -> str:
        total = len(self.state.store.active)
        live_sessions = sum(1 for s in self.state.sessions if s.is_live)
        stale = len(self.state.store.stale())

        parts = [
            f"[bold {C_BLUE}] ORCH [/bold {C_BLUE}]",
            f"[bold]{total}[/bold] streams",
        ]
        if live_sessions:
            parts.append(f"[{C_CYAN}]{live_sessions} live[/{C_CYAN}]")
        if stale:
            parts.append(f"[{C_DIM}]{stale} stale[/{C_DIM}]")

        if self.state.sessions:
            total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in self.state.sessions)
            if total_tokens > 0:
                _tk = f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000 else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000 else str(total_tokens)
                parts.append(f"[{C_DIM}]\u2502[/{C_DIM}]")
                parts.append(f"{_token_color_markup(_tk, total_tokens)}")

        return "  ".join(parts)

    def _render_filter_bar(self) -> str:
        filters = [
            ("active", "Active"), ("work", "Work"), ("personal", "Personal"),
            ("all", "All"), ("stale", "Stale"), ("archived", "Archived"),
        ]
        SEP = f" [{C_FAINT}]·[/{C_FAINT}] "
        preset_parts = []
        for i, (key, label) in enumerate(filters):
            n = i + 1
            if self.state.filter_mode == key:
                preset_parts.append(f"[bold {C_MID} on #0d1f35][{n}:{label}][/bold {C_MID} on #0d1f35]")
            else:
                preset_parts.append(f"[{C_FAINT}]{n}:{label}[/{C_FAINT}]")
        presets = SEP.join(preset_parts)

        if self.state.search_text:
            presets += f"  [{C_DIM}]·[/{C_DIM}]  [{C_DIM}]search:[/{C_DIM}] [{C_YELLOW}]{_rich_escape(self.state.search_text)}[/{C_YELLOW}]"

        # All non-home tabs (Sessions + open workstreams) shown at right
        other_tabs = [t for t in self.tabs.tabs if t.id != "home"]
        if other_tabs:
            tab_parts = []
            for t in other_tabs:
                lbl = (t.label[:14] + "\u2026") if len(t.label) > 14 else t.label
                is_active = t.id == self.tabs.active_tab.id
                if is_active:
                    tab_parts.append(f"[bold {C_BLUE}]● {_rich_escape(lbl)}[/bold {C_BLUE}]")
                else:
                    tab_parts.append(f"[{C_DIM}]○ {_rich_escape(lbl)}[/{C_DIM}]")
            tabs_str = f"  [{C_FAINT}]│[/{C_FAINT}]  " + f"  [{C_FAINT}]│[/{C_FAINT}]  ".join(tab_parts)
            return f" {presets}{tabs_str}"

        return f" {presets}"

    def _render_summary_bar(self) -> str:
        count = self._active_table().option_count
        if self.state.filter_mode == "archived":
            return (
                f"  {count} archived  "
                f"[{C_DIM}]\u2502[/{C_DIM}]  "
                f"[{C_DIM}]u[/{C_DIM}] unarchive  "
                f"[{C_DIM}]d[/{C_DIM}] delete  "
                f"[{C_DIM}]1[/{C_DIM}] back to all  "
                f"[{C_DIM}]?[/{C_DIM}] help"
            )
        # Count active ticket-solve jobs across all workstreams
        solving = sum(
            1 for ws in self.state.store.active
            if getattr(ws, "ticket_solve_status", "").lower() in ("running", "active")
        )
        solve_part = f"  [{C_YELLOW}]{solving} solving[/{C_YELLOW}]" if solving else ""
        return (
            f"  {count} workstreams{solve_part}  "
            f"[{C_DIM}]\u2502[/{C_DIM}]  "
            f"[{C_DIM}]r[/{C_DIM}] resume  "
            f"[{C_DIM}]c[/{C_DIM}] new session  "
            f"[{C_DIM}]n[/{C_DIM}] note  "
            f"[{C_DIM}]/[/{C_DIM}] search  "
            f"[{C_DIM}]?[/{C_DIM}] help  "
            f"[{C_DIM}]Tab[/{C_DIM}] tabs"
        )

    # ── Workstreams table ──

    def _refresh_ws_table(self):
        """Immediate table refresh for user-initiated actions."""
        self._refresh_pending = False
        self._do_refresh_ws_table()

    def _refresh_ws_table_debounced(self):
        """Debounced refresh for background/timer-driven updates."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self.set_timer(0.05, self._do_refresh_ws_table)

    def _olist_line_width(self, olist: OptionList) -> int:
        """Get usable character width from an OptionList."""
        try:
            w = olist.size.width
            return w - 2 if w > 20 else 0
        except Exception:
            return 0

    def _olist_cursor_key(self, olist: OptionList) -> str | None:
        """Get the option ID at the current cursor position."""
        idx = olist.highlighted
        if idx is not None:
            try:
                return olist.get_option_at_index(idx).id
            except Exception:
                pass
        return None

    def _olist_restore_cursor(self, olist: OptionList, old_key: str | None, old_idx: int | None = None):
        """Restore cursor to the option with old_key, or clamp to old_idx."""
        if old_key:
            for i in range(olist.option_count):
                try:
                    if olist.get_option_at_index(i).id == old_key:
                        olist.highlighted = i
                        return
                except Exception:
                    pass
        if old_idx is not None and olist.option_count > 0:
            olist.highlighted = min(old_idx, olist.option_count - 1)
        elif olist.option_count > 0 and olist.highlighted is None:
            olist.highlighted = 0

    @staticmethod
    def _ws_fingerprint(ws, ws_sessions, has_tmux, git_st, lw) -> tuple:
        """Cheap fingerprint capturing all inputs to _render_ws_option."""
        sess_fp = tuple(
            (s.session_id, s.is_live, s.last_message_role, s.last_activity,
             s.last_commit_sha, s.message_count)
            for s in ws_sessions[:8]  # cap to avoid huge tuples
        )
        git_fp = (git_st.branch, git_st.is_dirty, git_st.ahead) if git_st else None
        from datetime import date as _date
        return (ws.id, ws.name, ws.archived, ws.category, len(ws_sessions),
                sess_fp, has_tmux, git_fp, lw, _date.today())

    def _do_refresh_ws_table(self):
        """Actually rebuild the workstreams table (called via debounce timer)."""
        _t0 = _time.monotonic() if _PERF_ENABLED else 0
        self._refresh_pending = False
        try:
            table = self.query_one("#ws-table", OptionList)
        except Exception:
            return
        old_key = self._olist_cursor_key(table)
        old_idx = table.highlighted

        items = self.state.get_unified_items()
        last_seen = self.state.get_last_seen()
        lw = self._olist_line_width(table)

        # Build all options, skipping expensive rendering for unchanged items.
        # Track which IDs were re-rendered so we don't need get_option_at_index
        # comparisons in the update loop — cache-hit items are guaranteed unchanged.
        render_cache = getattr(self, '_ws_render_cache', {})
        options = []
        today_flags = []
        new_cache = {}
        rerendered_ids: set[str] = set()
        for ws in items:
            ws_sessions = self.state.sessions_for_ws(ws)
            git_st = None
            repo = ws.repo_path
            if repo:
                git_st = self.state.git_status_cache.get(repo)
            has_tmux = self.state.ws_has_tmux(ws)
            fp = self._ws_fingerprint(ws, ws_sessions, has_tmux, git_st, lw)
            cached = render_cache.get(ws.id)
            if cached and cached[0] == fp:
                prompt = cached[1]
            else:
                prompt = _render_ws_option(
                    ws, ws_sessions, last_seen,
                    tmux_check=self.state.ws_has_tmux,
                    line_width=lw,
                    git_status=git_st,
                )
                rerendered_ids.add(ws.id)
            new_cache[ws.id] = (fp, prompt)
            options.append(Option(prompt, id=ws.id))
            today_flags.append(
                _any_session_today(ws_sessions) if ws_sessions else _is_today(ws.updated_at)
            )
        self._ws_render_cache = new_cache
        if _PERF_ENABLED:
            _perf_log.warning("_do_refresh_ws_table: build_options %.1fms (%d re-rendered/%d cached)",
                              (_time.monotonic() - _t0) * 1000,
                              len(rerendered_ids), len(items) - len(rerendered_ids))

        # Insert "earlier" divider between today and non-today items
        sep_idx = next((i for i, t in enumerate(today_flags) if not t), None)
        final_options: list = list(options)
        if sep_idx is not None and sep_idx > 0:
            final_options.insert(sep_idx, _divider_option(lw))

        # In-place update when item set unchanged (liveness/timer refreshes)
        # Avoids expensive clear+re-add cycle (~7ms savings on 65 items)
        def _opt_id(o):
            return getattr(o, 'id', '__sep__')
        new_ids = [_opt_id(o) for o in final_options]
        # Use cached IDs to avoid iterating get_option_at_index when structure is stable
        cached_ids = getattr(self, '_ws_table_ids', None)
        if cached_ids is None:
            try:
                cached_ids = [_opt_id(table.get_option_at_index(i))
                               for i in range(table.option_count)]
            except Exception:
                cached_ids = []
        _t1 = _time.monotonic() if _PERF_ENABLED else 0

        n_replaced = 0
        with self.batch_update():
            if new_ids == cached_ids and len(final_options) == table.option_count:
                # Structure unchanged — only replace options that were re-rendered.
                # Cache-hit items have the same prompt as before; no get_option_at_index needed.
                for i, opt in enumerate(final_options):
                    ws_id = getattr(opt, 'id', None)
                    if ws_id is None or ws_id not in rerendered_ids:
                        continue  # separator or fingerprint-unchanged — skip
                    table.replace_option_prompt_at_index(i, opt.prompt)
                    n_replaced += 1
                self._ws_table_ids = new_ids
            else:
                # Structure changed — full rebuild
                table.clear_options()
                table.add_options(final_options)
                self._olist_restore_cursor(table, old_key, old_idx)
                self._ws_table_ids = new_ids
            self._update_all_bars()
            new_key = self._olist_cursor_key(table)
            self._update_preview(force=(new_key != old_key))
        if _PERF_ENABLED:
            _perf_log.warning("_do_refresh_ws_table: id_check %.1fms, batch_update %.1fms (%d replaced), total %.1fms",
                              (_t1 - _t0) * 1000,
                              (_time.monotonic() - _t1) * 1000, n_replaced,
                              (_time.monotonic() - _t0) * 1000)

    def _selected_ws(self) -> Workstream | None:
        try:
            table = self.query_one("#ws-table", OptionList)
        except Exception:
            return None
        key = self._olist_cursor_key(table)
        if not key:
            return None
        # In archived filter mode, look in archived store
        if self.state.filter_mode == "archived":
            return self.state.get_archived(key) or self.state.get_ws(key)
        return self.state.get_ws(key)

    def _sessions_for_ws(self, ws: Workstream, include_archived_sessions: bool = False) -> list[ClaudeSession]:
        """Delegate to state — kept for backward compat with DetailScreen."""
        return self.state.sessions_for_ws(ws, include_archived_sessions)

    # ── Sessions loading ──

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

        # Chip away at untitled session backlog (one batch per poll cycle)
        untitled = [s for s in sessions if not get_session_title(s)]
        if untitled:
            title_sessions(untitled)

        if self._session_fingerprint(self.state.sessions) == self._session_fingerprint(sessions):
            # Sessions unchanged but titles may have updated
            if untitled:
                self.call_from_thread(self._notify_sessions_changed)
            return

        discovered = get_discovered_workstreams(threads)
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

    @work(thread=True, exclusive=True, group="sessions")
    def _do_load_sessions(self):
        threads = discover_threads()
        apply_cached_names(threads)

        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)

        discovered = get_discovered_workstreams(threads)
        # Phase 1: get data visible fast
        self.call_from_thread(self._apply_sessions, sessions, threads, discovered)

        # Phase 2: AI-powered naming/titling/synthesis (slow, runs in background)
        any_ai_changes = False

        named = name_uncached_threads(threads)
        if named > 0:
            apply_cached_names(threads)
            any_ai_changes = True

        untitled = [s for s in sessions if not get_session_title(s)]
        if untitled:
            title_sessions(untitled)
            any_ai_changes = True

        new_count = synthesize_workstreams(threads, self.state.store.active)
        if new_count > 0:
            any_ai_changes = True

        titles_updated = refresh_thread_titles(threads)
        if titles_updated > 0:
            apply_cached_names(threads)
            any_ai_changes = True

        desc_updated = refresh_descriptions(self.state.store, sessions)
        if desc_updated > 0:
            any_ai_changes = True

        # Single callback for all AI-powered updates (was 3-5 separate callbacks)
        if any_ai_changes:
            discovered = get_discovered_workstreams(threads)
            self.call_from_thread(self._apply_ai_updates, threads, discovered)

    def _apply_sessions(self, sessions: list[ClaudeSession],
                        threads: list[Thread], discovered: list[Workstream]):
        self.state.update_sessions(sessions, threads, discovered)
        if not self._detail_screen_active:
            self._preview_ws_id = None
            self._refresh_ws_table_debounced()
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _apply_synthesis(self, threads: list[Thread], discovered: list[Workstream]):
        self.state.threads = threads
        self.state.discovered_ws = discovered
        if not self._detail_screen_active:
            self._refresh_ws_table_debounced()

    def _apply_ai_updates(self, threads: list[Thread], discovered: list[Workstream]):
        """Single callback for all AI-powered session/thread updates.

        Replaces 3-5 separate call_from_thread callbacks that were flooding
        the main thread's message queue during initial load.
        """
        self.state.threads = threads
        self.state.discovered_ws = discovered
        if not self._detail_screen_active:
            self._refresh_ws_table_debounced()
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _notify_sessions_changed(self):
        """Thread-safe helper to notify all screens of session changes."""
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

    def _inject_session(self, session: ClaudeSession) -> None:
        """Inject a session into state immediately so DetailScreen updates without polling."""
        # 1. Inject into flat sessions list
        existing = {s.session_id for s in self.state.sessions}
        if session.session_id in existing:
            for i, s in enumerate(self.state.sessions):
                if s.session_id == session.session_id:
                    self.state.sessions[i] = session
                    break
        else:
            self.state.sessions.insert(0, session)

        # 2. Inject into matching thread (sessions_for_ws uses threads primarily)
        sp = session.project_path.rstrip("/")
        injected = False
        for t in self.state.threads:
            if t.project_path.rstrip("/") == sp:
                t_sids = {s.session_id for s in t.sessions}
                if session.session_id not in t_sids:
                    t.sessions.insert(0, session)
                else:
                    for i, s in enumerate(t.sessions):
                        if s.session_id == session.session_id:
                            t.sessions[i] = session
                            break
                injected = True
                break

        # 3. If no matching thread, create a minimal one so the session is discoverable
        if not injected and sp:
            new_thread = Thread(
                thread_id=session.session_id,
                name=sp.rsplit("/", 1)[-1],
                project_path=sp,
                sessions=[session],
            )
            self.state.threads.append(new_thread)

        self.state.invalidate_caches()
        for screen in self.screen_stack:
            screen.post_message(SessionsChanged())

        # 4. Trigger background poll for full consistency (proper thread naming, etc.)
        self._do_poll_sessions()

    # ── Primary action (Enter) ──

    def action_select_item(self):
        self._open_detail()

    @on(OptionList.OptionSelected, "#ws-table")
    def on_ws_row_selected(self, event: OptionList.OptionSelected):
        self._open_detail()

    @on(OptionList.OptionSelected, "#preview-sessions")
    def on_preview_session_selected(self, event: OptionList.OptionSelected):
        idx = int(event.option_id)
        if idx < len(self.state.preview_sessions):
            session = self.state.preview_sessions[idx]
            mark_thread_seen(session.session_id)
            self.state._last_seen_valid = False
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
            self._open_detail_for_ws(ws)

    def _open_detail_for_ws(self, ws: Workstream):
        """Open a workstream in a tab and push its DetailScreen.

        Used by thought-to-thread flows (brain dump launch, ticket pick, etc.)
        """
        self.tabs.open_tab(ws.id, ws.name, "\u00b7")
        self._sync_tab_bar()
        self._detail_screen_active = True
        screen_name = f"detail:{ws.id}"
        if ws.id not in self._detail_screen_cache:
            screen = DetailScreen(ws, self.state.store)
            self._detail_screen_cache[ws.id] = screen
            self.install_screen(screen, screen_name)
        self.push_screen(screen_name, callback=lambda _: self._on_detail_dismissed())
        # Update the detail screen's tab bar now that it's the active screen
        self._sync_tab_bar()

    # ── Workstream actions ──

    def action_add(self):
        def on_result(ws: Workstream | None):
            if ws:
                self.state.store.add(ws)
                self.notify(f"Created: {ws.name}", timeout=2)
            self._refresh_ws_table()

        self.push_screen(AddScreen(), callback=on_result)

    def action_quick_note(self):
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
        ws = self._selected_ws()
        if not ws:
            return
        self.query_one("#search-input").display = False
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
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                TodoScreen(ws, self.state.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def action_open_links(self):
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
        ws = self._selected_ws()
        if not ws:
            return
        if self.state.filter_mode == "archived":
            name = self.state.unarchive(ws.id)
            if name:
                self.notify(f"Restored: {name}", timeout=2)
                self._refresh_ws_table()
        else:
            name = self.state.archive(ws.id)
            if name:
                self.notify(f"Archived: {name}", timeout=2)
                self._refresh_ws_table()

    # Keep legacy action names so DetailScreen and other callers still work
    def action_archive(self):
        self.action_toggle_archive()

    def action_unarchive(self):
        self.action_toggle_archive()

    def action_delete_item(self):
        ws = self._selected_ws()
        if ws:
            def on_confirm(confirmed: bool):
                if confirmed:
                    self.state.delete(ws.id)
                    self.notify(f"Deleted: {ws.name}", timeout=2)
                    self._refresh_ws_table()

            self.push_screen(
                ConfirmScreen(f"[bold {C_RED}]Delete[/bold {C_RED}] [bold]{_rich_escape(ws.name)}[/bold]?"),
                callback=on_confirm,
            )

    # ── Brain dump ──

    def action_brain_dump(self):
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

        def on_result(mode: str):
            if not mode:
                return
            created = []
            for task in tasks:
                ws = Workstream(
                    name=task.name,
                    description=task.raw_text,
                    category=task.category,
                )
                self.state.store.add(ws)
                created.append(ws)
            self._refresh_ws_table()

            if mode == "launch" and created:
                # Launch Claude session on the first workstream
                self.notify(f"Added {len(created)} workstreams — launching session...", timeout=2)
                ws = created[0]
                self._open_detail_for_ws(ws)
            else:
                self.notify(f"Added {len(created)} workstreams", timeout=2)

        self.push_screen(BrainPreviewScreen(tasks), callback=on_result)

    # ── Spawn & resume ──

    @work(thread=False)
    async def launch_claude_session(
        self,
        ws: Workstream,
        session_id: str | None = None,
        prompt: str | None = None,
        cwd: str | None = None,
        callback=None,
    ) -> None:
        """Push a ClaudeSessionScreen for the given workstream."""
        from claude_session_screen import ClaudeSessionScreen
        from terminal import TerminalWidget

        # Check if this session is still alive in tmux — run in executor to avoid
        # blocking the event loop (tmux subprocess.run can hang for up to 3s).
        reattach = False
        effective_sid = session_id

        # If no explicit session_id, reuse any pending new session for this workstream
        # so that pressing "c", going back, then pressing "c" again returns to the same thread.
        if session_id is None and ws.id:
            effective_sid = self._ws_pending_session.get(ws.id)

        if effective_sid:
            self._detached_sessions.pop(effective_sid, None)
            reattach = await asyncio.get_running_loop().run_in_executor(
                None, TerminalWidget.tmux_session_alive, effective_sid
            )

        screen = ClaudeSessionScreen(
            ws=ws, store=self.state.store,
            session_id=effective_sid, prompt=prompt, cwd=cwd,
            reattach_tmux=reattach,
        )

        # Remember the session ID so "c" returns to the same thread next time
        if session_id is None and ws.id:
            self._ws_pending_session[ws.id] = screen._session_id

        def _on_dismiss(result):
            if isinstance(result, dict) and result.get("detached"):
                self._detached_sessions[result["session_id"]] = {
                    "ws": result["ws"],
                    "start_time": result["start_time"],
                    "jsonl": result["jsonl"],
                }
                # Parse the JSONL so we can inject the session immediately.
                # Mark is_live=True since the process is still running (detached,
                # not killed) — parse_session doesn't check PIDs.
                jsonl = result.get("jsonl")
                if jsonl:
                    from sessions import parse_session
                    from pathlib import Path
                    try:
                        s = parse_session(Path(jsonl))
                        if s:
                            s.is_live = True
                            self._inject_session(s)
                            # Session has messages — it's a real thread now, next "c" should be fresh
                            if s.message_count and ws.id and self._ws_pending_session.get(ws.id) == screen._session_id:
                                del self._ws_pending_session[ws.id]
                    except Exception:
                        pass
            elif isinstance(result, ClaudeSession):
                self.notify(
                    f"{result.model_short} | {result.message_count} msgs | {result.tokens_display}",
                    timeout=5,
                )
                self._inject_session(result)
                # Session completed naturally — clear the pending slot so next "c" is fresh
                if ws.id and self._ws_pending_session.get(ws.id) == screen._session_id:
                    del self._ws_pending_session[ws.id]
            self._refresh_ws_table()
            if callback:
                callback(result)
        self.push_screen(screen, callback=_on_dismiss)
        self._sync_tab_bar()



    def action_spawn(self):
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        self.launch_claude_session(ws)

    def action_repo_spawn(self):
        repos = self.state.discover_all_repos()
        # Build workstream count lookup for the picker
        ws_counts: dict[str, int] = {}
        for repo in repos:
            n = len(self.state.workstreams_for_repo(repo))
            if n > 0:
                ws_counts[repo] = n

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

        self.push_screen(RepoPickerScreen(repos, ws_counts), callback=on_repo_picked)

    def _spawn_in_ws(self, ws: Workstream):
        self.launch_claude_session(ws)

    def action_resume(self):
        ws = self._selected_ws()
        if ws:
            do_resume(ws, self, self.state.sessions,
                      sessions_for_ws_fn=lambda w: self.state.sessions_for_ws(w))

    def _suspend_claude(self, cmd: list[str], cwd: str | None = None):
        with self.suspend():
            subprocess.run(cmd, cwd=cwd)

    def _find_ws_for_session(self, session: ClaudeSession) -> Workstream | None:
        """Backward compat — delegate to state."""
        return self.state.find_ws_for_session(session)

    # ── Link action ──

    def action_link_action(self):
        self._add_link_to_ws()

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

    # ── Filter & sort ──

    def action_filter(self, mode: str):
        self.state.set_filter(mode)
        self._refresh_ws_table()

    def action_sort(self, mode: str):
        self.state.set_sort(mode)
        self._refresh_ws_table()

    def action_search(self):
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
        self._refresh_ws_table_debounced()

    # ── Command palette ──

    def _active_detail_screen(self) -> DetailScreen | None:
        """Return the active DetailScreen, or None if on home screen."""
        for screen in reversed(self.screen_stack):
            if isinstance(screen, DetailScreen):
                return screen
        return None

    def _context_ws(self) -> Workstream | None:
        """Get the contextually correct workstream.

        Returns the DetailScreen's ws when a detail view is active,
        otherwise falls back to the home-screen selection.
        """
        detail = self._active_detail_screen()
        if detail:
            return detail.ws
        return self._selected_ws()

    def action_command_palette(self):
        from state import get_command_items
        from widgets import FuzzyPickerScreen

        has_ws = self._context_ws() is not None
        items = get_command_items(has_ws)

        def on_cmd(cmd_name: str | None):
            if cmd_name:
                self._execute_command(cmd_name)

        screen = FuzzyPickerScreen(title="Command Palette")
        screen._get_items = lambda: items
        screen._on_selected = lambda item_id: (screen.dismiss(item_id),)
        self.push_screen(screen, callback=on_cmd)

    def _execute_command(self, cmd_text: str):
        ws = self._context_ws()
        result = self.state.execute_command(cmd_text, ws.id if ws else None)

        action = result.get("action", "noop")
        msg = result.get("msg", "")

        # When DetailScreen is active, delegate ws-specific commands to it
        detail = self._active_detail_screen()

        if action == "refresh":
            self._refresh_ws_table()
            if detail:
                detail._refresh()
            if msg:
                self.notify(msg, timeout=2)
        elif action == "notify":
            self.notify(msg, timeout=2)
        elif action == "error":
            self.notify(msg, severity="error", timeout=2)
        elif action == "add":
            self.action_add()
        elif action == "rename":
            if detail:
                self.notify("Use 'E' to rename from detail view", timeout=2)
            else:
                self.action_rename()
        elif action == "open":
            if detail:
                detail.action_open_links()
            else:
                self.action_open_links()
        elif action == "spawn":
            if detail:
                detail.action_spawn()
            else:
                self.action_spawn()
        elif action == "resume":
            if detail:
                detail.action_resume()
            else:
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
        elif action == "close":
            self.action_close_tab()
        elif action == "help":
            self.push_screen(HelpScreen())
        elif action == "delete":
            self.action_delete_item()
        elif action == "unarchive":
            self.action_unarchive()
        # Dev-workflow actions
        elif action == "ship":
            self.action_ship()
        elif action == "ticket":
            self.action_ticket(result.get("query", ""))
        elif action == "ticket-create":
            self.action_ticket_create(result.get("title", ""))
        elif action == "branches":
            self.action_branches()
        elif action == "files":
            self.action_files()
        elif action == "git-action":
            self._do_git_action(result.get("cmd", ""))
        elif action == "solve":
            self._do_solve(result.get("ticket", ""))
        elif action == "worktree":
            self.action_branches()

    # ── Dev-workflow actions ──

    def action_ship(self):
        """Ship staged changes — run oneshot or publish-changes in a terminal."""
        from actions import run_dev_tool, ws_working_dir, dev_tools_available
        if not dev_tools_available():
            self.notify("dev-workflow-tools not found at ~/bin/dev-workflow-tools", severity="error", timeout=3)
            return
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        cwd = ws_working_dir(ws)
        cmd = run_dev_tool("oneshot")
        if cmd:
            with self.suspend():
                subprocess.run(cmd, cwd=cwd)
            self._poll_git_status()

    def action_ticket(self, query: str = ""):
        """Open ticket picker — browse Jira tickets from cache."""
        from actions import get_jira_cache
        cache = get_jira_cache()
        if not cache:
            self.notify("No Jira tickets cached. Run jira-fzf to populate.", severity="warning", timeout=3)
            return

        items = []
        for key, info in cache.items():
            status_color = C_DIM
            if "progress" in info.status.lower():
                status_color = C_CYAN
            elif "done" in info.status.lower() or "closed" in info.status.lower():
                status_color = C_GREEN
            label = (
                f"[bold]{_rich_escape(key)}[/bold]  "
                f"{_rich_escape(info.summary[:60])}  "
                f"[{status_color}]{_rich_escape(info.status)}[/{status_color}]"
            )
            if info.assignee:
                label += f"  [{C_DIM}]{_rich_escape(info.assignee)}[/{C_DIM}]"
            items.append((key, label))

        def on_ticket(ticket_key: str | None):
            if not ticket_key:
                return
            ws = self._selected_ws()
            if ws:
                # Link ticket to existing workstream
                ws.add_link(kind="ticket", value=ticket_key, label=ticket_key)
                self.state.store.update(ws)
                self._refresh_ws_table()
                self.notify(f"Linked {ticket_key} to {ws.name}", timeout=2)
            else:
                # Create new workstream from ticket
                ticket_info = cache.get(ticket_key)
                name = ticket_info.summary if ticket_info else ticket_key
                new_ws = Workstream(name=name, category=Category.WORK)
                new_ws.add_link(kind="ticket", value=ticket_key, label=ticket_key)
                self.state.store.add(new_ws)
                self._refresh_ws_table()
                self.notify(f"Created workstream: {name}", timeout=2)
                # Open it immediately (thought to thread)
                self._open_detail_for_ws(new_ws)

        from widgets import FuzzyPickerScreen
        screen = FuzzyPickerScreen(title="Select Ticket")
        screen._get_items = lambda: items
        screen._on_selected = lambda item_id: (screen.dismiss(item_id),)
        self.push_screen(screen, callback=on_ticket)

    def action_ticket_create(self, title: str = ""):
        """Create a new Jira ticket via dev-workflow-tools."""
        from actions import run_dev_tool, dev_tools_available
        if not dev_tools_available():
            self.notify("dev-workflow-tools not found", severity="error", timeout=3)
            return
        cmd = run_dev_tool("create-jira-ticket")
        if title:
            cmd.extend(["--summary", title])
        if cmd:
            with self.suspend():
                subprocess.run(cmd)

    def action_branches(self):
        """Open branch/worktree picker for the selected workstream's repo."""
        from actions import get_worktree_list, get_recent_branches, ws_working_dir
        ws = self._selected_ws()
        repo = ws.repo_path if ws else None
        if not repo:
            self.notify("No repo linked to workstream", timeout=2)
            return

        worktrees = get_worktree_list(repo)
        branches = get_recent_branches(repo)

        items = []
        wt_branches = {wt.get("branch", ""): wt.get("path", "") for wt in worktrees}

        for wt in worktrees:
            branch = wt.get("branch", "unknown")
            path = wt.get("path", "")
            short_path = path.replace(str(Path.home()), "~")
            label = f"[bold {C_CYAN}]\u26a1 {_rich_escape(branch)}[/bold {C_CYAN}]  [{C_DIM}]{short_path}[/{C_DIM}]"
            items.append((path, label))

        for br in branches:
            branch = br["branch"]
            if branch not in wt_branches:
                label = f"[{C_DIM}]  {_rich_escape(branch)}[/{C_DIM}]"
                items.append((f"branch:{branch}", label))

        def on_branch(selection: str | None):
            if not selection:
                return
            if selection.startswith("branch:"):
                # Just a branch, no worktree — could create one
                self.notify(f"Branch: {selection[7:]}", timeout=2)
            else:
                # It's a worktree path — could cd there
                self.notify(f"Worktree: {selection}", timeout=2)

        from widgets import FuzzyPickerScreen
        screen = FuzzyPickerScreen(title="Branches & Worktrees")
        screen._get_items = lambda: items
        screen._on_selected = lambda item_id: (screen.dismiss(item_id),)
        self.push_screen(screen, callback=on_branch)

    def action_files(self):
        """Open file picker for the selected workstream's directory."""
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        from actions import ws_working_dir
        cwd = ws_working_dir(ws)
        editor = os.environ.get("EDITOR", "vim")
        # Use fzedit if available, otherwise fall back to basic fzf
        from actions import run_dev_tool, dev_tools_available
        if dev_tools_available():
            cmd = run_dev_tool("fzedit")
            if cmd:
                with self.suspend():
                    subprocess.run(cmd, cwd=cwd)
                return
        # Fallback: suspend to $EDITOR
        with self.suspend():
            subprocess.run([editor, "."], cwd=cwd)

    def _do_git_action(self, action_name: str):
        """Run a git action in the selected workstream's directory."""
        from actions import run_git_action, ws_working_dir
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        cwd = ws_working_dir(ws)
        success, msg = run_git_action(action_name, cwd)
        if success:
            self.notify(msg, timeout=2)
            self._poll_git_status()
        else:
            self.notify(msg, severity="error", timeout=3)

    def _do_solve(self, ticket: str = ""):
        """Run ticket-solve for a ticket."""
        from actions import run_dev_tool, dev_tools_available, ws_working_dir
        if not dev_tools_available():
            self.notify("dev-workflow-tools not found", severity="error", timeout=3)
            return
        if not ticket:
            # Try to get ticket from current workstream
            ws = self._selected_ws()
            if ws:
                for link in ws.links:
                    if link.kind == "ticket":
                        ticket = link.value
                        break
        if not ticket:
            self.notify("No ticket specified. Usage: :solve UB-1234", severity="error", timeout=3)
            return
        cmd = run_dev_tool("ticket-solve", [ticket])
        if cmd:
            with self.suspend():
                subprocess.run(cmd)

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
            self._refresh_ws_table_debounced()

    # ── Git status polling ──

    def _poll_git_status(self):
        self._do_git_status_check()

    @work(thread=True, exclusive=True, group="git_status")
    def _do_git_status_check(self):
        from actions import get_worktree_git_status
        # Collect all unique repo paths
        repo_paths: set[str] = set()
        for ws in list(self.state.store.active) + list(self.state.discovered_ws):
            if ws.repo_path:
                repo_paths.add(ws.repo_path)

        new_cache: dict[str, object] = {}
        for path in repo_paths:
            new_cache[path] = get_worktree_git_status(path)

        # Check if anything changed
        old_keys = set(self.state.git_status_cache.keys())
        if old_keys != set(new_cache.keys()) or any(
            getattr(new_cache.get(k), 'is_dirty', None) != getattr(self.state.git_status_cache.get(k), 'is_dirty', None)
            or getattr(new_cache.get(k), 'branch', None) != getattr(self.state.git_status_cache.get(k), 'branch', None)
            or getattr(new_cache.get(k), 'ahead', None) != getattr(self.state.git_status_cache.get(k), 'ahead', None)
            or getattr(new_cache.get(k), 'behind', None) != getattr(self.state.git_status_cache.get(k), 'behind', None)
            for k in new_cache
        ):
            self.call_from_thread(self._apply_git_status, new_cache)

    def _apply_git_status(self, new_cache: dict):
        self.state.git_status_cache = new_cache
        self._refresh_ws_table_debounced()

    # ── Worktree discovery polling ──

    def _poll_worktrees(self):
        self._do_worktree_check()

    @work(thread=True, exclusive=True, group="worktrees")
    def _do_worktree_check(self):
        changed = self.state.discover_and_enrich_worktrees()
        if changed:
            self.call_from_thread(self._refresh_ws_table_debounced)

    # ── Other ──

    def action_refresh(self):
        self.state.store.load()
        self._refresh_ws_table()
        self._load_sessions()
        self._poll_tmux()
        self.notify("Refreshed", timeout=1)

    def action_help(self):
        self.push_screen(HelpScreen())

    def _on_return_from_modal(self):
        self.state.store.load()
        self.state._last_seen_valid = False  # pick up marks from detail screen
        self._preview_ws_id = None  # force preview rebuild
        self._refresh_ws_table()

    def _focus_main_list(self):
        """Focus the main workstream list. Used by InlineInput on cancel."""
        self._active_table().focus()


if __name__ == "__main__":
    OrchestratorApp().run()
