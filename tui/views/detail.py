"""DetailView — Detail-lite port of screens.DetailScreen (P4-C).

The workstream detail surface: tab bar, title + meta badges, sessions
BlockList (2fr) + archived BlockList (1fr, load-more row), body panel
(links + todo summary) and a help bar. Pane focus ring (ctrl+j/k) over
sessions/archived/body with a blue border on the focused pane.

Detail-lite deviations from the Textual original (see MIGRATION.md):
- No embedded tig panes (P6): when the ws has a repo the body panel is
  shown instead, plus a hint line; 't' runs tig fullscreen over
  App.suspend() — a new key (the original had no 't'; app-level 't'
  was toggle_trust).
- Session select/resume = the embedded ClaudeSessionView via
  app.launch_claude_session (suspend-attach until P5).
- Peek reads the focused pane's highlighted session (the original
  always peeked the sessions list even from the archived pane).

Timers (the original's discipline): 30s liveness in a thread worker on
a per-ws exclusive group whose generation is captured per run (stale
results dropped — to_thread bodies can't be cancelled), gated by the
options fingerprint so unchanged data never rebuilds rows; 0.3s
throbber re-rendering only animating rows in place (skipped while
hidden / peek / search); 0.12s loading spinner during first discovery.
View timers pause on on_hide and on_show fast-repaints then reloads.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone

from notifications import dismiss_all_for_dirs, dismiss_notification
from rendering import (
    BG_RAISED,
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_GREEN, C_LIGHT, C_PURPLE, C_YELLOW,
    QUIET_SEPARATOR_LABEL, SHELVED_SEPARATOR_LABEL, THINKING_SEPARATOR_LABEL,
    THROBBER_FRAMES,
    _is_session_seen, _render_content_search_result,
    _render_notified_session_option, _render_session_option, _rich_escape,
    _session_title, render_peek_header, render_ws_body_lines, render_ws_meta,
    tool_bar_legend,
)
from state import (
    AppState, auto_unshelve_sessions, content_search, fuzzy_match,
    fuzzy_match_positions, group_detail_sessions, map_notifications_to_sessions,
)
from threads import ThreadActivity, load_last_seen, mark_thread_seen, save_last_seen, session_activity

from ..keys import KeyEvent
from ..layout import Rect, split_cols, split_rows
from ..view import View
from ..widgets import BlockList, LineEdit, strip_markup
from .add_link import AddLinkView
from .help import HelpView
from .home import render_tab_bar
from .links import LinksView
from .modals import draw_border
from .quick_note import QuickNoteView
from .todo import TodoView
from .trash import TrashView

_ANIM_ACTS = (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT)
_ELEVATED_ACTS = (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY)
_ARCHIVED_PAGE_SIZE = 30


class DetailView(View):
    opaque = True

    def __init__(self, state, tabs, ws) -> None:
        super().__init__()
        self.state = state
        self.tabs = tabs
        self.ws = ws
        self.store = state.store
        self._detail_sessions: list = []
        self._archived_sessions: list = []
        self._all_sessions: list = []
        self._all_archived: list = []
        self._shelved_set: set[str] = set(ws.shelved_sessions)
        self._feed_notifications: list = []
        self._session_notifications: dict[str, object] = {}
        self._throbber_frame = 0
        self._loading_frame = 0
        self._last_seen_cache: dict[str, str] = {}
        self._active_pane = "sessions"
        self._animating_sids: list[str] = []
        self._animating_archived_sids: list[str] = []
        self._last_build_fp: tuple | None = None
        self._last_arch_fp: tuple | None = None
        self._last_session_fp = None
        self._archived_show_count = _ARCHIVED_PAGE_SIZE
        # Peek + content search
        self._peek_mode = False
        self._peek_count = 0
        self._content_cache: dict[str, list] = {}
        self._content_ready = False
        self._content_results: list = []
        self._content_search_active = False
        self._title_only_search = False
        self._title_highlights: dict[str, list[int]] = {}
        self._pending_highlight_sid: str | None = None
        self._help_override: str | None = None
        self._body_scroll = 0
        self._mounted = False
        self._loading_timer = None
        self._rect: Rect | None = None
        self.sessions_list = BlockList()
        self.archived_list = BlockList()
        self.search = LineEdit()
        self.search.on_change = self._on_search_changed
        self._search_active = False
        self._search_focus = False
        self._cwd = None  # computed lazily (ws_working_dir shells out)
        self._keymap = self._build_keymap()

    # per-ws exclusive groups: cached DetailViews must not cancel each other
    @property
    def _liveness_group(self) -> str:
        return f"detail_liveness:{self.ws.id}"

    @property
    def _content_group(self) -> str:
        return f"detail_content:{self.ws.id}"

    def request_session_highlight(self, session_id: str) -> None:
        """Queue a session highlight to apply once the list is built."""
        self._pending_highlight_sid = session_id

    # ── lifecycle ─────────────────────────────────────────────────

    def on_resize(self, rect) -> None:
        old_w = self._rect.w if self._rect else -1
        self._rect = rect
        if self._mounted and rect.w != old_w and not self._peek_mode:
            self._last_build_fp = None  # row layout depends on line width
            self._last_arch_fp = None
            self._build_session_rows()
            self._build_archived_rows()

    def on_show(self) -> None:
        super().on_show()
        if not self._timers:  # first show, or timers cancelled by a pop
            self.set_interval(30, self._periodic_refresh)
            self.set_interval(0.3, self._tick_throbber)
            if self._sessions_loading():
                self._loading_timer = self.set_interval(0.12, self._tick_loading)
        if not self._mounted:
            self._mounted = True
            self._initial_load()
        else:
            self._resume_refresh()

    def _initial_load(self) -> None:
        self._last_seen_cache = load_last_seen()
        self._mark_all_seen()
        self._load_feed()  # before _load_detail_sessions: notif map must be ready
        self._load_detail_sessions()
        self._apply_pending_highlight()

    def _resume_refresh(self) -> None:
        """Port of on_screen_resume/_resume_refresh: fast repaint happens on
        show; the reload runs immediately after (all sub-ms operations)."""
        self.ws = self.store.get(self.ws.id) or self.ws
        self._last_seen_cache = load_last_seen()
        self._mark_all_seen()
        self._load_feed()
        self._load_detail_sessions()
        self._sync_refresh_live()
        self._apply_pending_highlight()
        self.request_paint()

    def _sessions_loading(self) -> bool:
        return not self.state.sessions_loaded

    def _mark_all_seen(self) -> None:
        sessions = self.state.sessions_for_ws(self.ws, include_archived_sessions=True)
        if not sessions:
            return
        data = load_last_seen()
        now = datetime.now(timezone.utc).isoformat()
        for s in sessions:
            data[s.session_id] = now
        save_last_seen(data)
        self._last_seen_cache = data

    def _sync_refresh_live(self) -> None:
        from sessions import refresh_session_tail
        for s in self._detail_sessions:
            if s.is_live:
                refresh_session_tail(s)

    def _apply_pending_highlight(self) -> None:
        sid = self._pending_highlight_sid
        if not sid:
            return
        self._pending_highlight_sid = None
        for i, (rid, _m, disabled) in enumerate(self.sessions_list.rows):
            if not disabled and rid == sid:
                self.sessions_list.highlighted = i
                return

    # ── keys ──────────────────────────────────────────────────────

    def _build_keymap(self) -> dict:
        import config
        m: dict = {}

        def bind(keys: str, fn) -> None:
            for k in keys.split(","):
                k = k.strip()
                if k:
                    m.setdefault(k, fn)

        # Screen-local bindings first (they shadow app-level keys, as the
        # original's DetailScreen BINDINGS did)
        bind("escape,ctrl+h,backspace,h", self._go_back)
        bind("ctrl+l", self._action_resume)
        bind("ctrl+j", lambda: self._cycle_panel(1))
        bind("ctrl+k", lambda: self._cycle_panel(-1))
        bind("p", self._action_peek)
        bind("f", self._action_file_picker)
        bind("y", self._action_yank)
        bind("z", self._action_shelve)
        bind("d", self._action_dismiss_notification)
        bind("D", self._action_dismiss_all_notifications)
        bind("A", self._action_archive_all)
        bind("X", self._action_trash_session)
        bind("T", self._action_view_trash)
        bind("space", self._action_archive_session)
        bind("backslash", self._action_search_titles)
        bind("t", self._action_tig)  # Detail-lite substitute for embedded tig
        # Config-driven keys (respect user overrides via get_key)
        g = config.get_key
        bind(g("cursor_down"), lambda: self._nav("j"))
        bind(g("cursor_up"), lambda: self._nav("k"))
        bind(g("cursor_top"), lambda: self._nav("g"))
        bind(g("cursor_bottom"), lambda: self._nav("G"))
        bind(g("half_page_down"), lambda: self._nav("ctrl+d"))
        bind(g("half_page_up"), lambda: self._nav("ctrl+u"))
        bind(g("select_item"), self._action_select)
        bind(g("next_tab"), lambda: self.app.action_next_tab())
        bind(g("prev_tab"), lambda: self.app.action_prev_tab())
        bind(g("close_tab"), lambda: self.app.action_close_tab())
        bind(g("spawn"), self._action_spawn)
        bind(g("resume"), self._action_resume)
        bind(g("quick_note"), self._action_quick_note)
        bind(g("edit_notes"), self._action_open_todos)
        bind(g("link_action"), self._action_add_link)
        bind(g("open_links"), self._action_open_links)
        bind(g("toggle_archive"), self._action_archive_ws)
        bind(g("search"), self._action_search)
        bind(g("command_palette"), lambda: self.app.open_command_palette())
        bind(g("help"), lambda: self.app.push(HelpView(context="detail")))
        return m

    def on_key(self, ev) -> bool:
        if self._search_active and self._search_focus:
            if ev.key in ("enter", "down"):
                self._focus_search_results()
            elif ev.key == "escape":
                self._go_back()
            else:
                self.search.handle_key(ev)
            self.request_paint()
            return True
        fn = self._keymap.get(ev.key)
        if fn is None:
            return False
        fn()
        self.request_paint()
        return True

    def _nav(self, canonical: str) -> None:
        if self._active_pane == "body":
            self._scroll_body(canonical)
            return
        self._focused_list().handle_key(KeyEvent(canonical, None))

    def _scroll_body(self, key: str) -> None:
        n = len(self._body_lines())
        page = max(1, (self._layout(self._rect)["lower"].h - 2) if self._rect else 10)
        if key == "j":
            self._body_scroll += 1
        elif key == "k":
            self._body_scroll -= 1
        elif key == "g":
            self._body_scroll = 0
        elif key == "G":
            self._body_scroll = n
        elif key == "ctrl+d":
            self._body_scroll += page // 2
        elif key == "ctrl+u":
            self._body_scroll -= page // 2
        self._body_scroll = max(0, min(self._body_scroll, max(0, n - page)))

    # ── panels (ctrl+j/k) ─────────────────────────────────────────

    def _panels(self) -> list[str]:
        panels = ["sessions"]
        if self._archived_sessions:
            panels.append("archived")
        panels.append("body")
        return panels

    def _cycle_panel(self, step: int) -> None:
        panels = self._panels()
        idx = panels.index(self._active_pane) if self._active_pane in panels else 0
        self._active_pane = panels[(idx + step) % len(panels)]

    def _focused_list(self) -> BlockList:
        return self.archived_list if self._active_pane == "archived" else self.sessions_list

    # ── selection ─────────────────────────────────────────────────

    def _highlighted_key(self, lst: BlockList | None = None):
        lst = lst or self._focused_list()
        hid = lst.highlighted_id
        return BlockList.block_key(hid) if hid is not None else None

    def _highlighted_sid(self, lst: BlockList | None = None) -> str | None:
        key = self._highlighted_key(lst)
        if not isinstance(key, str) or key.startswith("__") or key.startswith("peek"):
            return None
        return key

    def _find_session_by_id(self, sid: str):
        for s in self._detail_sessions:
            if s.session_id == sid:
                return s
        for s in self._archived_sessions:
            if s.session_id == sid:
                return s
        return None

    def _action_select(self) -> None:
        """Enter/l — port of on_option_list_option_selected."""
        if self._peek_mode or self._active_pane == "body":
            return
        key = self._highlighted_key()
        if key == "__load_more_archived__":
            self._archived_show_count += _ARCHIVED_PAGE_SIZE
            self._last_arch_fp = None
            self._build_archived_rows()
            return
        sid = self._highlighted_sid()
        session = self._find_session_by_id(sid) if sid else None
        if not session:
            return
        mark_thread_seen(session.session_id)
        self._last_seen_cache = load_last_seen()
        # Auto-dismiss notifications matching this session (by sid AND cwd,
        # so the cwd fallback doesn't resurface old ones next poll).
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
            self.ws, session_id=session.session_id, cwd=session.project_path)

    def _action_resume(self) -> None:
        """r / ctrl+l — resume the highlighted session (no notif dismissal)."""
        if self._peek_mode or self._active_pane not in ("sessions", "archived"):
            return
        sid = self._highlighted_sid()
        session = self._find_session_by_id(sid) if sid else None
        if session:
            mark_thread_seen(session.session_id)
            self._last_seen_cache = load_last_seen()
            self.app.launch_claude_session(
                self.ws, session_id=session.session_id, cwd=session.project_path)

    def _action_spawn(self) -> None:
        self.app.launch_claude_session(self.ws)

    def _action_yank(self) -> None:
        if self._active_pane not in ("sessions", "archived"):
            return
        sid = self._highlighted_sid()
        if not sid:
            return
        cmd = f"claude --resume {sid}"
        self.app.copy_to_clipboard(cmd)
        self._help_override = f"[{C_GREEN}]Copied:[/{C_GREEN}] [{C_LIGHT}]{cmd}[/{C_LIGHT}]"
        self.set_timer(5, self._clear_help_override)

    def _clear_help_override(self) -> None:
        self._help_override = None
        self.request_paint()

    # ── session state mutations ───────────────────────────────────

    def _action_archive_session(self) -> None:
        """space — archive (sessions pane) / restore (archived pane)."""
        sid = self._highlighted_sid()
        if not sid:
            return
        if self._active_pane == "archived":
            self.ws.archived_sessions.pop(sid, None)
            self.store.update(self.ws)
        elif self._active_pane == "sessions":
            if sid not in self.ws.archived_sessions:
                self.ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
                self.store.update(self.ws)
        else:
            return
        self._refresh()

    def _action_archive_all(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for s in list(self._detail_sessions):
            if s.session_id not in self.ws.archived_sessions:
                self.ws.archived_sessions[s.session_id] = now
                changed = True
        if changed:
            self.store.update(self.ws)
            n = len(self._detail_sessions)
            self.app.notify(f"Archived {n} session{'s' if n != 1 else ''}", timeout=2)
            self._refresh()

    def _action_shelve(self) -> None:
        if self._active_pane != "sessions":
            return
        sid = self._highlighted_sid()
        if not sid:
            return
        if sid in self.ws.shelved_sessions:
            del self.ws.shelved_sessions[sid]
            self._shelved_set.discard(sid)
            self.store.update(self.ws)
            self.app.notify("Unshelved", timeout=1)
        else:
            self.ws.shelved_sessions[sid] = datetime.now(timezone.utc).isoformat()
            self._shelved_set.add(sid)
            self.store.update(self.ws)
            self.app.notify("Shelved", timeout=1)
        self._refresh()

    def _action_trash_session(self) -> None:
        sid = self._highlighted_sid()
        if not sid:
            return
        if sid not in self.ws.deleted_sessions:
            self.ws.deleted_sessions[sid] = datetime.now(timezone.utc).isoformat()
            self.ws.archived_sessions.pop(sid, None)
            self.ws.shelved_sessions.pop(sid, None)
            self.store.update(self.ws)
            self.app.notify("Moved to trash  (T to view)", timeout=2)
        self._refresh()

    def _action_view_trash(self) -> None:
        self.app.push(TrashView(self.state))

    def _action_archive_ws(self) -> None:
        self.ws.archived = True
        self.store.update(self.ws)
        self.app.notify(f"Archived: {self.ws.name}", timeout=2)
        self.dismiss(None)

    # ── notifications ─────────────────────────────────────────────

    def _action_dismiss_notification(self) -> None:
        if self._active_pane != "sessions":
            return
        sid = self._highlighted_sid(self.sessions_list)
        notif = self._session_notifications.get(sid) if sid else None
        if not notif:
            return
        dismiss_notification(notif.id)
        notif.dismissed = True
        del self._session_notifications[sid]
        self._load_detail_sessions()

    def _action_dismiss_all_notifications(self) -> None:
        dirs = self.state._ws_dirs(self.ws)
        if dirs:
            dismiss_all_for_dirs(self._feed_notifications, dirs)
        for n in self._feed_notifications:
            n.dismissed = True
        self._session_notifications.clear()
        self._load_detail_sessions()

    # ── workstream sub-screens ────────────────────────────────────

    def _action_quick_note(self) -> None:
        def on_note(text) -> None:
            if not text or not text.strip():
                return
            self.state.add_todo(self.ws.id, text.strip())
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()
            self.app.notify("Todo added", timeout=1)

        self.app.push(QuickNoteView(self.ws), on_result=on_note)

    def _action_open_todos(self) -> None:
        self.store.load()  # CLI-created todos (e.g. crystallized) appear
        self.ws = self.store.get(self.ws.id) or self.ws

        def on_close(_res) -> None:
            self.store.load()
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()

        self.app.push(TodoView(self.ws, self.store), on_result=on_close)

    def _action_open_links(self) -> None:
        if self.ws.links:
            self.app.push(LinksView(self.ws, self.store))
        else:
            self.app.notify("No links to open", timeout=2)

    def _action_add_link(self) -> None:
        def on_link(link) -> None:
            if link:
                self.ws.links.append(link)
                self.ws.touch()
                self.store.update(self.ws)
                self._refresh()
                self.app.notify(f"Added {link.kind} link", timeout=2)

        self.app.push(AddLinkView(self.ws.name), on_result=on_link)

    def _working_dir(self) -> str:
        if self._cwd is None:
            from actions import ws_working_dir
            self._cwd = ws_working_dir(self.ws)
        return self._cwd

    def _action_file_picker(self) -> None:
        from actions import open_file_picker, ws_directories
        cwd = self._working_dir()
        if cwd == os.getcwd() and not self.ws.repo_path and not ws_directories(self.ws):
            self.app.notify("No directory linked to this workstream", timeout=2)
            return
        if not shutil.which("fzedit"):
            self.app.notify("fzedit not found on PATH", timeout=2)
            return
        with self.app.suspend():
            open_file_picker(cwd)

    def _has_repo(self) -> bool:
        from actions import ws_directories
        return bool(self.ws.repo_path or ws_directories(self.ws))

    def _action_tig(self) -> None:
        """Detail-lite substitute: fullscreen tig over suspend() (embedded
        tig panes land in P6). Same gate as the original's
        _detect_git_sidebar: tig on PATH + a directory that git recognizes."""
        if not self._has_repo():
            self.app.notify("No git repo linked to this workstream", timeout=2)
            return
        if not shutil.which("tig"):
            self.app.notify("tig not found on PATH", timeout=2)
            return
        cwd = self._working_dir()
        try:
            ok = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"],
                                capture_output=True, timeout=3).returncode == 0
        except Exception:
            ok = False
        if not ok:
            self.app.notify(f"Not a git repo: {cwd}", timeout=2)
            return
        with self.app.suspend():
            subprocess.run(["tig"], cwd=cwd)

    # ── back cascade (port of action_dismiss/action_go_back) ─────

    def _go_back(self) -> None:
        if self._peek_mode:
            self._close_peek()
        elif self._search_active:
            self._cancel_search()
        else:
            self.dismiss(None)

    # ── data loading ──────────────────────────────────────────────

    def _refresh(self) -> None:
        self._load_detail_sessions()
        self._load_feed()
        self.request_paint()

    def on_data_changed(self) -> None:
        """Sessions/liveness data changed (called by OrchApp; the engine's
        SessionsChanged. Fingerprint-gated, so unchanged data is a no-op)."""
        if self._mounted:
            self._refresh()

    def _load_detail_sessions(self) -> None:
        all_sessions = self.state.sessions_for_ws(self.ws, include_archived_sessions=True)
        # Auto-unshelf: a new human message after shelving wakes the session
        if auto_unshelve_sessions(self.ws, all_sessions):
            self.store.update(self.ws)
        hidden = set(self.ws.archived_sessions)
        self._all_sessions = [s for s in all_sessions if s.session_id not in hidden]
        self._all_archived = [s for s in all_sessions if s.session_id in hidden]
        self._shelved_set = set(self.ws.shelved_sessions)

        if self._search_active and self.search.text:
            return  # backing data updated silently; next keystroke re-searches
        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)
        if self._peek_mode:
            return  # visible list stays on the conversation
        self._build_session_rows()
        self._build_archived_rows()
        if not self._archived_sessions and self._active_pane == "archived":
            self._active_pane = "sessions"

    def _load_feed(self) -> None:
        self._feed_notifications = self.state.notifications_for_ws(self.ws)
        self._session_notifications = map_notifications_to_sessions(
            self._feed_notifications, self._detail_sessions)

    # ── fingerprints (the rebuild gates) ──────────────────────────

    def _session_fingerprint(self) -> frozenset:
        return frozenset(
            (s.session_id, s.is_live, s.last_message_role, s.message_count)
            for s in self._detail_sessions
        )

    def _detail_options_fingerprint(self) -> tuple:
        notif_map = self._session_notifications
        seen_cache = self._last_seen_cache
        shelved = self._shelved_set
        return (
            tuple(
                (
                    s.session_id,
                    s.is_live,
                    s.last_message_role,
                    s.message_count,
                    s.assistant_message_count,
                    s.last_activity,
                    s.last_commit_sha,
                    s.tokens_display,
                    s.context_tokens,
                    s.session_id in shelved,
                    seen_cache.get(s.session_id, ""),
                    (notif_map[s.session_id].id, notif_map[s.session_id].dt)
                        if s.session_id in notif_map else None,
                )
                for s in self._detail_sessions
            ),
        )

    # ── 30s liveness worker ───────────────────────────────────────

    def _periodic_refresh(self) -> None:
        app = self.app
        app.exclusive(self._liveness_group,
                      self._liveness_runner(app.gen(self._liveness_group) + 1))

    async def _liveness_runner(self, g: int) -> None:
        def work():
            from actions import refresh_liveness
            from sessions import refresh_session_tail
            refresh_liveness(self._detail_sessions)
            for s in self._detail_sessions:
                if s.is_live:
                    refresh_session_tail(s)
            return self._session_fingerprint()

        fp = await asyncio.to_thread(work)
        if self.app.gen(self._liveness_group) != g:
            return  # superseded while off-loop — drop the stale result
        self._apply_liveness_result(fp)

    def _apply_liveness_result(self, new_fp) -> None:
        old_fp = self._last_session_fp
        self._last_session_fp = new_fp
        if old_fp != new_fp and not (self._content_search_active or self._peek_mode):
            self._build_session_rows()
            self._build_archived_rows()
        # Also poll the feed (single merged timer, as the original)
        old_notif_sids = set(self._session_notifications)
        self._load_feed()
        if old_notif_sids != set(self._session_notifications):
            self._load_detail_sessions()
        self.request_paint()

    # ── row building (port of _build_session_list) ────────────────

    def _session_line_width(self, archived: bool = False) -> int:
        if self._rect is None:
            return 0
        lay = self._layout(self._rect)
        r = lay["archived"] if archived else lay["sessions"]
        if r is None:
            return 0
        w = r.w - 4 - 2  # border inset + margins, minus scroll padding
        return w if w > 20 else 0

    def _auto_role(self, sid: str) -> str | None:
        fn = getattr(self.app, "auto_role_for", None)
        return fn(self.ws.id, sid) if fn else None

    def _session_lines(self, s, lw: int, *, archived: bool = False) -> list[str]:
        """Markup lines for one session block, choosing the notified /
        elevated / quiet renderer exactly as the original build does."""
        act = session_activity(s, self._last_seen_cache)
        seen = _is_session_seen(s, self._last_seen_cache)
        role = self._auto_role(s.session_id)
        hl = self._title_highlights.get(s.session_id)
        if not archived and s.session_id in self._session_notifications:
            prompt = _render_notified_session_option(
                s, act, self._session_notifications[s.session_id],
                self._throbber_frame, ws_repo_path=self.ws.repo_path,
                seen=seen, line_width=lw, auto_role=role, title_highlights=hl)
        elif not archived and not seen and act in _ELEVATED_ACTS:
            prompt = _render_notified_session_option(
                s, act, None, self._throbber_frame,
                ws_repo_path=self.ws.repo_path, seen=seen, line_width=lw,
                auto_role=role, title_highlights=hl)
        else:
            prompt = _render_session_option(
                s, act, self._throbber_frame, ws_repo_path=self.ws.repo_path,
                seen=seen, line_width=lw, archived=archived,
                shelved=(not archived and s.session_id in self._shelved_set),
                auto_role=role, title_highlights=hl)
        return str(prompt).split("\n")

    @staticmethod
    def _block_rows(sid: str, lines: list[str]) -> list[tuple]:
        rows = [(sid, lines[0], False)]
        rows.extend(((sid, j), line, True) for j, line in enumerate(lines[1:], 1))
        return rows

    def _build_session_rows(self, force: bool = False) -> None:
        if self._content_search_active:
            return  # rows hold search results
        new_fp = self._detail_options_fingerprint()
        if not force and new_fp == self._last_build_fp:
            return  # fingerprint gate: rows already show this state
        lw = self._session_line_width()
        (notified, elevated, quiet_today, quiet_thinking,
         quiet_earlier, quiet_shelved) = group_detail_sessions(
            self._detail_sessions, self._session_notifications,
            self._last_seen_cache, self._shelved_set)
        rows: list[tuple] = []
        animating: list[str] = []

        def add(s, shelved_group=False):
            act = session_activity(s, self._last_seen_cache)
            if act in _ANIM_ACTS and not shelved_group:
                animating.append(s.session_id)
            rows.extend(self._block_rows(s.session_id, self._session_lines(s, lw)))

        for s in notified:
            add(s)
        for s in elevated:
            add(s)
        if (notified or elevated) and (quiet_today or quiet_thinking
                                       or quiet_earlier or quiet_shelved):
            rows.append(("__separator__", QUIET_SEPARATOR_LABEL(lw or 60), True))
        for s in quiet_today:
            add(s)
        if quiet_thinking:
            rows.append(("__sep_thinking__", THINKING_SEPARATOR_LABEL(lw or 60), True))
            for s in quiet_thinking:
                add(s)
        if quiet_earlier:
            pad = max(1, ((lw or 60) - 10) // 2)
            rows.append(("__sep_earlier__",
                         f"[{C_FAINT}]{'─' * pad} earlier {'─' * pad}[/{C_FAINT}]", True))
            for s in quiet_earlier:
                add(s)
        if quiet_shelved:
            rows.append(("__sep_shelved__", SHELVED_SEPARATOR_LABEL(lw or 60), True))
            for s in quiet_shelved:
                add(s, shelved_group=True)
        self.sessions_list.set_rows(rows)  # keeps the highlight by session id
        self._animating_sids = animating
        self._last_build_fp = new_fp
        self.request_paint()

    def _build_archived_rows(self) -> None:
        if self._content_search_active:
            return
        limit = self._archived_show_count
        display = self._archived_sessions[:limit]
        arch_fp = tuple((s.session_id, s.is_live, s.last_message_role) for s in display)
        if arch_fp == self._last_arch_fp:
            return
        self._last_arch_fp = arch_fp
        alw = self._session_line_width(archived=True)
        rows: list[tuple] = []
        animating: list[str] = []
        for s in display:
            act = session_activity(s, self._last_seen_cache)
            if act in _ANIM_ACTS:
                animating.append(s.session_id)
            rows.extend(self._block_rows(
                s.session_id, self._session_lines(s, alw, archived=True)))
        remaining = len(self._archived_sessions) - limit
        if remaining > 0:
            rows.append((
                "__load_more_archived__",
                f"[{C_DIM}]  ↓ {remaining} more archived sessions — press Enter to load[/{C_DIM}]",
                False,
            ))
        self.archived_list.set_rows(rows)
        self._animating_archived_sids = animating
        self.request_paint()

    # ── throbber & loading spinner ────────────────────────────────

    def _tick_throbber(self) -> None:
        app = self.app
        if app is not None and not app.ui_visible:
            return
        if not (self._animating_sids or self._animating_archived_sids):
            return
        self._throbber_frame += 1
        if self._content_search_active or self._peek_mode:
            return  # don't overwrite search results or peek content
        lw = self._session_line_width()
        by_sid = {s.session_id: s for s in self._detail_sessions}
        ok = True
        for sid in self._animating_sids:
            s = by_sid.get(sid)
            if not s:
                ok = False
                continue
            for j, line in enumerate(self._session_lines(s, lw)):
                row_id = sid if j == 0 else (sid, j)
                ok = self.sessions_list.update_row(row_id, line) and ok
        if not ok:  # structure changed under us — rebuild via the gate
            self._last_build_fp = None
            self._build_session_rows()
        alw = self._session_line_width(archived=True)
        arch_by_sid = {s.session_id: s for s in self._archived_sessions}
        for sid in self._animating_archived_sids:
            s = arch_by_sid.get(sid)
            if not s:
                continue
            for j, line in enumerate(self._session_lines(s, alw, archived=True)):
                row_id = sid if j == 0 else (sid, j)
                self.archived_list.update_row(row_id, line)
        self.request_paint()

    def _tick_loading(self) -> None:
        if not self._sessions_loading():
            if self._loading_timer is not None:
                self._loading_timer.cancel()
                self._loading_timer = None
            self._refresh()
            return
        self._loading_frame += 1
        self.request_paint()

    # ── peek (port of _open_peek/_close_peek) ─────────────────────

    def _action_peek(self) -> None:
        if self._active_pane in ("sessions", "archived"):
            self._open_peek()

    def _open_peek(self) -> None:
        if self._peek_mode:
            self._close_peek()
            return
        sid = self._highlighted_sid()
        session = self._find_session_by_id(sid) if sid else None
        if not session:
            return
        from sessions import extract_session_content
        if session.session_id in self._content_cache:
            messages = self._content_cache[session.session_id]
        else:
            messages = (extract_session_content(session.jsonl_path)
                        if session.jsonl_path else [])
            self._content_cache[session.session_id] = messages
        if not messages:
            self.app.notify("No conversation content to peek", timeout=2)
            return
        rows: list[tuple] = []
        rows.extend(self._block_rows("peek-header",
                                     render_peek_header(session).split("\n")))
        for i, msg in enumerate(messages):
            role_fmt = (f"[bold {C_CYAN}]you[/bold {C_CYAN}]" if msg.role == "user"
                        else f"[bold {C_PURPLE}]claude[/bold {C_PURPLE}]")
            text = msg.text
            if len(text) > 2000:
                text = text[:2000] + "\n…(truncated)"
            lines = [role_fmt] + [
                f"[{C_LIGHT}]{_rich_escape(line)}[/{C_LIGHT}]"
                for line in text.split("\n")
            ]
            rows.extend(self._block_rows(f"peek-msg-{i}", lines))
        self._peek_count = len(messages)
        self.sessions_list.set_rows(rows, keep_id=False)
        # jump to the last message block
        for i in range(len(rows) - 1, -1, -1):
            if not rows[i][2]:
                self.sessions_list.highlighted = i
                break
        self._peek_mode = True
        self._last_build_fp = None  # rows no longer reflect session state

    def _close_peek(self) -> None:
        self._peek_mode = False
        self._last_build_fp = None
        self._build_session_rows()
        idx = self.sessions_list._scan(0, 1)
        self.sessions_list.highlighted = idx  # top, as the original

    # ── content search (port of action_search / '\\') ─────────────

    def _action_search(self) -> None:
        self._title_only_search = False
        self._search_active = True
        self._search_focus = True
        if not self._content_ready:
            self._warm_content_cache()

    def _action_search_titles(self) -> None:
        self._title_only_search = True
        self._search_active = True
        self._search_focus = True

    def _focus_search_results(self) -> None:
        self._search_focus = False
        idx = self.sessions_list._scan(0, 1)
        if idx != -1:
            self.sessions_list.highlighted = idx
        self._active_pane = "sessions"

    def _cancel_search(self) -> None:
        self.search.text = ""
        self.search.cursor = 0
        self._search_active = False
        self._search_focus = False
        self._content_search_active = False
        self._content_results = []
        self._title_only_search = False
        self._title_highlights = {}
        self._detail_sessions = list(self._all_sessions)
        self._archived_sessions = list(self._all_archived)
        self._last_build_fp = None
        self._last_arch_fp = None
        self._build_session_rows()
        self._build_archived_rows()
        self._active_pane = "sessions"

    def _on_search_changed(self, text: str) -> None:
        query = text
        if not query:
            self._content_search_active = False
            self._content_results = []
            self._title_highlights = {}
            self._detail_sessions = list(self._all_sessions)
            self._archived_sessions = list(self._all_archived)
            self._last_build_fp = None
            self._last_arch_fp = None
            self._build_session_rows()
            self._build_archived_rows()
            return
        if self._title_only_search:
            self._apply_title_only_filter()
        elif self._content_ready:
            self._run_content_search_sync()
        else:
            self._apply_title_filter()

    def _warm_content_cache(self) -> None:
        app = self.app
        app.exclusive(self._content_group,
                      self._warm_runner(app.gen(self._content_group) + 1))

    async def _warm_runner(self, g: int) -> None:
        def work():
            from sessions import extract_session_content
            for s in self._all_sessions + self._all_archived:
                if s.session_id not in self._content_cache and s.jsonl_path:
                    self._content_cache[s.session_id] = \
                        extract_session_content(s.jsonl_path)

        await asyncio.to_thread(work)
        if self.app.gen(self._content_group) != g:
            return  # superseded — a fresher warm owns _content_ready
        self._content_ready = True
        if self._search_active and self.search.text:
            self._run_content_search_sync()  # query typed while warming
            self.request_paint()

    def _run_content_search_sync(self) -> None:
        all_sessions = self._all_sessions + self._all_archived
        results = content_search(self.search.text, all_sessions, self._content_cache)
        if results:
            self._content_results = results
            self._content_search_active = True
            self._show_content_results()
        else:
            self._content_search_active = False
            self._content_results = []
            self._apply_title_filter()

    def _show_content_results(self) -> None:
        rows: list[tuple] = []
        for r in self._content_results:
            prompt = _render_content_search_result(r, ws_repo_path=self.ws.repo_path)
            rows.extend(self._block_rows(r.session.session_id, str(prompt).split("\n")))
        self.sessions_list.set_rows(rows, keep_id=False)
        idx = self.sessions_list._scan(0, 1)
        self.sessions_list.highlighted = idx
        self._detail_sessions = [r.session for r in self._content_results]
        self._archived_sessions = []
        self.archived_list.set_rows([])
        self._last_build_fp = None

    def _apply_title_filter(self) -> None:
        """Fallback fuzzy filter on titles while the content cache warms."""
        scored = []
        for s in self._all_sessions + self._all_archived:
            searchable = " ".join(filter(None, [s.display_name, s.last_message_text, s.model]))
            sc = fuzzy_match(self.search.text, searchable)
            if sc is not None:
                scored.append((s, sc))
        scored.sort(key=lambda t: t[1], reverse=True)
        self._apply_filtered([s for s, _ in scored])

    def _apply_title_only_filter(self) -> None:
        """'\\' search: strict fuzzy on the rendered session title, with
        matched-position highlights."""
        scored = []
        self._title_highlights = {}
        for s in self._all_sessions + self._all_archived:
            res = fuzzy_match_positions(self.search.text, _session_title(s) or "")
            if res is not None:
                score, positions = res
                scored.append((s, score))
                self._title_highlights[s.session_id] = positions
        scored.sort(key=lambda t: t[1], reverse=True)
        self._apply_filtered([s for s, _ in scored])

    def _apply_filtered(self, sessions: list) -> None:
        self._detail_sessions = sessions
        self._archived_sessions = []
        self._content_search_active = False
        self.archived_list.set_rows([])
        self._last_build_fp = None
        self._last_arch_fp = None
        self._build_session_rows(force=True)
        idx = self.sessions_list._scan(0, 1)
        self.sessions_list.highlighted = idx

    # ── header / body / help renderers (ports) ────────────────────

    def _render_title(self) -> str:
        return f"[bold {C_PURPLE}]{_rich_escape(self.ws.name)}[/bold {C_PURPLE}]"

    def _body_lines(self) -> list[str]:
        lines = render_ws_body_lines(self.ws, AppState.active_todos(self.ws))
        if self._has_repo():
            lines.append("")
            lines.append(f"[{C_YELLOW}]t[/{C_YELLOW}][{C_DIM}]: tig (fullscreen) — "
                         f"embedded panes land later[/{C_DIM}]")
        return lines

    def _render_help(self) -> str:
        if self._help_override:
            return self._help_override
        toast = getattr(self.app, "toast_text", "") if self.app is not None else ""
        if toast:
            return f"[{C_YELLOW}]{_rich_escape(toast)}[/{C_YELLOW}]"
        if self._search_active and not self._search_focus:
            pairs = [("j/k", "navigate"), ("^L", "resume"),
                     ("/", "refine"), ("^H", "clear/back")]
        else:
            pairs = [
                ("^L", "resume"), ("c", "spawn"),
                ("n", "+todo"), ("e", "todos"), ("W", "+link"),
                ("o", "open"), ("x", "close tab"), ("u", "archive ws"),
                ("space", "archive/restore"),
                ("z", "defer/undefer"),
                ("p", "peek"), ("y", "yank cmd"),
                # "\\\\" renders one backslash — a bare "\" would escape the
                # closing tag (the Textual original had that glitch)
                ("^j/^k", "panels"), ("/", "search"), ("\\\\", "titles"),
                ("^H", "back"),
            ]
        return "  ".join(f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in pairs)

    # ── layout & render ───────────────────────────────────────────

    def _layout(self, rect: Rect) -> dict:
        desc_h = 1 if self.ws.description else 0
        tab, title, desc, lists, lower, help_r = split_rows(
            rect, 1, 1, desc_h, 2.0, 1.0, 1)
        if self._search_active:  # results get the full width, as the original
            sess, arch = lists, None
        else:
            sess, arch = split_cols(lists, 2.0, 1.0)
        return {"tab": tab, "title": title, "desc": desc, "sessions": sess,
                "archived": arch, "lower": lower, "help": help_r}

    def _sessions_label(self) -> str:
        if self._peek_mode:
            return (f"[bold {C_BLUE}]Conversation[/bold {C_BLUE}] "
                    f"[{C_DIM}]({getattr(self, '_peek_count', 0)} messages)[/{C_DIM}]")
        if self._content_search_active:
            n = len(self._content_results)
            return (f"[bold {C_BLUE}]Search[/bold {C_BLUE}] "
                    f"[{C_DIM}]({n} result{'s' if n != 1 else ''})[/{C_DIM}]")
        n_active = len(self._detail_sessions)
        active_sids = {s.session_id for s in self._detail_sessions}
        n_notified = sum(1 for sid in self._session_notifications if sid in active_sids)
        notif_badge = f" [{C_GREEN}]({n_notified} new)[/{C_GREEN}]" if n_notified else ""
        loading = self._sessions_loading() and n_active == 0
        spinner = ""
        if loading:
            frame = THROBBER_FRAMES[self._loading_frame % len(THROBBER_FRAMES)]
            spinner = f" [{C_CYAN}]{frame}[/{C_CYAN}]"
        count_str = f"({n_active})" if not loading else ""
        if self._active_pane == "sessions":
            return (f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]{spinner} "
                    f"[{C_DIM}]{count_str}[/{C_DIM}]{notif_badge}")
        return f"[{C_DIM}]Sessions {count_str}[/{C_DIM}]{spinner}{notif_badge}"

    def _archived_label(self) -> str:
        n = len(self._archived_sessions)
        if self._active_pane == "archived":
            return f"[bold {C_BLUE}]Archived[/bold {C_BLUE}] [{C_DIM}]({n})[/{C_DIM}]"
        return f"[{C_DIM}]Archived ({n})[/{C_DIM}]"

    def _pane_content(self, frame, rect: Rect, focused: bool) -> Rect:
        if focused:
            draw_border(frame, rect, C_BLUE)
        return Rect(rect.x + 1, rect.y + 1, max(0, rect.w - 2), max(0, rect.h - 2))

    def render(self, frame, rect) -> None:
        self._rect = rect
        lay = self._layout(rect)
        frame.write_markup(lay["tab"].x + 1, lay["tab"].y, lay["tab"].w - 1,
                           render_tab_bar(self.state, self.tabs))
        title_line = (self._render_title() + "  "
                      + render_ws_meta(self.ws, self._detail_sessions))
        self._raised_line(frame, lay["title"], f"  {title_line}")
        if lay["desc"].h > 0:
            self._raised_line(frame, lay["desc"],
                              f"  [{C_DIM}]{_rich_escape(self.ws.description)}[/{C_DIM}]")

        self._render_sessions_pane(frame, lay["sessions"])
        if lay["archived"] is not None and lay["archived"].w > 0:
            self._render_archived_pane(frame, lay["archived"])
        self._render_body_pane(frame, lay["lower"])

        help_r = lay["help"]
        if help_r.h > 0:
            frame.write_markup(help_r.x + 2, help_r.y, help_r.w - 2, self._render_help())

    def _raised_line(self, frame, r: Rect, markup: str) -> None:
        pad = " " * max(0, r.w - len(strip_markup(markup)))
        frame.write_markup(r.x, r.y, r.w,
                           f"[on {BG_RAISED}]{markup}{pad}[/on {BG_RAISED}]")

    def _render_sessions_pane(self, frame, rect: Rect) -> None:
        c = self._pane_content(frame, rect, self._active_pane == "sessions")
        if c.w <= 0 or c.h <= 0:
            return
        # label + right-aligned tool-bar legend (port of _label_with_legend)
        label = self._sessions_label()
        legend = tool_bar_legend()
        gap = c.w - 4 - len(strip_markup(label)) - len(strip_markup(legend))
        line = f"{label}{' ' * gap}{legend}" if gap > 2 else label
        frame.write_markup(c.x + 2, c.y, c.w - 2, line)
        y = c.y + 1
        if self._search_active:
            prompt = "search titles..." if self._title_only_search else "search..."
            input_w = max(1, c.w - 6)
            if self.search.text or self._search_focus:
                q = self.search.render(input_w)
            else:
                q = f"[{C_DIM}]{prompt}[/{C_DIM}]"
            frame.write_markup(c.x + 2, y, c.w - 4, f"[{C_YELLOW}]/[/{C_YELLOW}] {q}")
            if self._search_focus:
                frame.cursor = (c.x + 4 + self.search.cursor_col(input_w), y)
            y += 1
        list_h = c.bottom - y
        if list_h <= 0:
            return
        if not self.sessions_list.rows:
            if self._sessions_loading():
                f_ = THROBBER_FRAMES[self._loading_frame % len(THROBBER_FRAMES)]
                msg = f"[{C_CYAN}]{f_}[/{C_CYAN}] [{C_DIM}]Discovering sessions...[/{C_DIM}]"
            elif self._search_active and self.search.text:
                msg = f"[{C_DIM}]No matches[/{C_DIM}]"
            else:
                msg = f"[{C_DIM}]No sessions[/{C_DIM}]"
            frame.write_markup(c.x + 3, y + 1, c.w - 4, msg)
            return
        self.sessions_list.page_size = max(1, list_h)
        for i, line in enumerate(self.sessions_list.render(c.w - 2, list_h)):
            frame.write_markup(c.x + 1, y + i, c.w - 2, line)

    def _render_archived_pane(self, frame, rect: Rect) -> None:
        c = self._pane_content(frame, rect, self._active_pane == "archived")
        if c.w <= 0 or c.h <= 0:
            return
        frame.write_markup(c.x + 2, c.y, c.w - 2, self._archived_label())
        list_h = c.h - 1
        if list_h <= 0:
            return
        if not self.archived_list.rows:
            frame.write_markup(c.x + 3, c.y + 2, c.w - 4, f"[{C_DIM}]Empty[/{C_DIM}]")
            return
        self.archived_list.page_size = max(1, list_h)
        for i, line in enumerate(self.archived_list.render(c.w - 2, list_h)):
            frame.write_markup(c.x + 1, c.y + 1 + i, c.w - 2, line)

    def _render_body_pane(self, frame, rect: Rect) -> None:
        c = self._pane_content(frame, rect, self._active_pane == "body")
        if c.w <= 0 or c.h <= 0:
            return
        lines = self._body_lines()
        max_scroll = max(0, len(lines) - c.h)
        self._body_scroll = max(0, min(self._body_scroll, max_scroll))
        window = lines[self._body_scroll:self._body_scroll + c.h]
        for i, line in enumerate(window):
            frame.write_markup(c.x + 2, c.y + i, c.w - 4, line)
