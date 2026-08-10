"""OrchApp — the orchestrator application shell on the tui engine (P3).

Owns AppState + TabManager, the staggered background pollers, and the
SessionBridge (Rust daemon pipe) / SessionWatcher (watchdog) wiring —
ports of app.py's on_mount workers. Pushes HomeView as the root view.

Selected via ORCH_ENGINE=tui in cli.cmd_tui; the Textual engine stays
the default until cutover (see MIGRATION.md).

Data isolation: pass ``store_path`` (tests) or set ORCH_STORE_PATH in
the environment (smoke runs) to point at a store copy instead of the
real data.json.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

from actions import ui_is_visible, ws_working_dir
from models import Store
from session_bridge import SessionBridge
from session_launch import auto_link_session, claude_jsonl_path
from sessions import ClaudeSession, parse_session
from state import AppState, TabManager
from term_host import TerminalHost

from .app import App
from .view import Timer
from .views.claude_session import ClaudeSessionView
from .views.current_sessions import CurrentSessionsView
from .views.detail import DetailView
from .views.home import HomeView

TOAST_SECS = 3.0


class OrchApp(App):
    def __init__(self, store_path: str | Path | None = None, pollers: bool = True) -> None:
        super().__init__(visibility_probe=ui_is_visible)
        if store_path is None:
            env_path = os.environ.get("ORCH_STORE_PATH")
            store_path = Path(env_path).expanduser() if env_path else None
        self.state = AppState(Store(path=Path(store_path) if store_path else None))
        self.tabs = TabManager()
        self._pollers_enabled = pollers
        self._bg_started = False
        self._session_bridge: SessionBridge | None = None
        self._session_watcher = None
        self._detail_cache: dict[str, DetailView] = {}  # ws_id -> cached view
        self._current_sessions_view: CurrentSessionsView | None = None
        self._tab_keymap = self._build_tab_keymap()

        # Claude-session bookkeeping (ports of app.py's on_mount dicts)
        self._detached_sessions: dict[str, dict] = {}  # sid -> {ws, start_time, jsonl}
        self._tab_active_session: dict[str, str] = {}  # ws_id -> sid (tab-switch resume)
        self._ws_pending_session: dict[str, str] = {}  # ws_id -> sid for reuse on "c"

        # Auto mode (coordinator/implementer loop)
        self._auto_modes: dict[str, object] = {}       # ws_id -> AutoMode
        self._auto_coord_sid: dict[str, str] = {}      # ws_id -> coordinator sid
        self._auto_impl_sids: dict[str, set] = {}      # ws_id -> implementer sids

        # Rate limiters (ports of app.py's rust-update / liveness debounce)
        self._last_rust_update = 0.0
        self._rust_update_pending = False
        self._last_liveness_check = 0.0
        self._liveness_deferred = False
        self._db_refresh_running = False

        self.toast_text = ""
        self._toast_timer: Timer | None = None

        self.home = HomeView(self.state, self.tabs)
        self.push(self.home)

    # ── toast / notify ────────────────────────────────────────────

    def notify(self, message: str, *, severity: str = "information",
               timeout: float = TOAST_SECS) -> None:
        """Transient one-line message on the footer (Textual notify shim)."""
        self.toast_text = str(message)
        if self._toast_timer is not None:
            self._toast_timer.cancel()
            self._toast_timer = None
        if self._loop is not None:  # pre-run notify: no loop for a timer yet
            self._toast_timer = self.set_timer(timeout, self._clear_toast)
        self.request_paint()

    toast = notify

    def _clear_toast(self) -> None:
        self.toast_text = ""
        self._toast_timer = None
        self.request_paint()

    # ── background workers (called from HomeView.on_show) ────────

    def ensure_background_started(self) -> None:
        if self._bg_started or not self._pollers_enabled:
            return
        self._bg_started = True

        # Startup sweep of orphan archived placeholders (port of on_mount).
        try:
            pruned = self.state.store.prune_orphan_archived()
        except Exception:
            pruned = 0
        if pruned:
            self.state.store.save()
            self.notify(f"Pruned {pruned} orphan archived workstreams")

        # Immediate first session load so data appears fast.
        self._run_in_thread("poll_sessions", self._do_poll_sessions)

        # Staggered 30s pollers (5/10/15/20s offsets, port of on_mount).
        self.every(30, self._do_tmux_check, thread=True)
        self.every(30, self._do_git_status_check, thread=True, jitter_start=5)
        self.every(30, self._do_worktree_check, thread=True, jitter_start=10)
        self.every(30, self._do_poll_sessions, thread=True, jitter_start=15)
        # Liveness backstop goes through the same rate limiter as watcher
        # events so bursts coalesce into one worker (runs on the loop; the
        # actual refresh happens in the "liveness" exclusive thread group).
        self.every(30, self._refresh_session_liveness, jitter_start=20)

        # Auto-cleanup of idle orch-launched Claude sessions (45s, then 10min).
        from cleanup import _idle_hours_from_env
        self._idle_cleanup_hours = _idle_hours_from_env()
        if self._idle_cleanup_hours > 0:
            self.every(600, self._auto_cleanup_idle_sessions, thread=True, jitter_start=45)

        # Session change notifications: Rust daemon pipe preferred,
        # Python watchdog fallback (port of on_mount ~458-476).
        self._session_bridge = SessionBridge()
        if self._session_bridge.available:
            self._session_bridge.start(
                callback=lambda: self.call_from_thread(self._on_rust_engine_update)
            )
        else:
            from sessions import ensure_rust_engine_running
            ensure_rust_engine_running()  # try to start the daemon for next time
            from watcher import SessionWatcher
            self._session_watcher = SessionWatcher(
                on_liveness=lambda: self.call_from_thread(self._on_liveness_file_change),
                on_content=lambda: self.call_from_thread(self._on_session_file_change),
                debounce=1.0,
                content_debounce=2.0,
            )
            self._session_watcher.start()

    def exit(self, result=None) -> None:
        # Detach/stop embedded PTY children while the loop still runs — a
        # live child deadlocks asyncio's executor shutdown. detach keeps
        # claude alive in tmux; tig children die with the app.
        for view, _ in self._stack:
            if isinstance(view, ClaudeSessionView):
                view.emergency_close()
        if self._session_bridge is not None:
            self._session_bridge.stop()
            self._session_bridge = None
        if self._session_watcher is not None:
            self._session_watcher.stop()
            self._session_watcher = None
        super().exit(result)

    # ── tabs (port of app.py's tab machinery) ─────────────────────
    #
    # Stack invariant: _stack[0] is always HomeView; _stack[1], when a
    # ws/sessions tab is active, is its cached DetailView or the
    # CurrentSessionsView (transient modals stack above). Tab cycling
    # swaps _stack[1] via replace_top (no on_result fires); dismissing
    # the surface (esc/^H) pops it, which fires _on_tab_view_dismissed
    # — the port of app._on_detail_dismissed (back to the home tab,
    # the ws tab itself stays open in the bar).

    def _build_tab_keymap(self) -> dict:
        """App-level tab keys (the Textual app intercepted ctrl+b/ctrl+x in
        App.on_key and bound close_tab app-wide): fire for any key the top
        view didn't consume, so tab switching works from every tab surface."""
        import config
        keymap: dict = {}
        for action, fn in (("next_tab", self.action_next_tab),
                           ("prev_tab", self.action_prev_tab),
                           ("close_tab", self.action_close_tab)):
            for key in config.get_key(action).split(","):
                key = key.strip()
                if key:
                    keymap.setdefault(key, fn)
        return keymap

    def on_unhandled_key(self, ev) -> None:
        fn = self._tab_keymap.get(ev.key)
        if fn is not None:
            fn()

    def _tab_surface(self):
        if len(self._stack) >= 2:
            view = self._stack[1][0]
            if isinstance(view, (DetailView, CurrentSessionsView)):
                return view
        return None

    def _active_detail_view(self) -> DetailView | None:
        surface = self._tab_surface()
        return surface if isinstance(surface, DetailView) else None

    def _ensure_detail_view(self, ws) -> DetailView:
        view = self._detail_cache.get(ws.id)
        if view is None:
            view = DetailView(self.state, self.tabs, ws)
            self._detail_cache[ws.id] = view
        return view

    def open_detail(self, ws, highlight_session_id: str | None = None) -> None:
        """Open a workstream in a tab and show its DetailView (port of
        app._open_detail_for_ws; home enter/l and the brain-dump launch)."""
        self.tabs.open_tab(ws.id, ws.name, "·")
        view = self._ensure_detail_view(ws)
        if highlight_session_id:
            view.request_session_highlight(highlight_session_id)
        self._show_tab_surface(view)

    def _show_tab_surface(self, target) -> None:
        """Make `target` the view above home (None = back to bare home)."""
        surface = self._tab_surface()
        if target is None:
            while len(self._stack) > 1:
                self.pop(None)  # surface pop fires _on_tab_view_dismissed
            return
        if surface is target:
            while self.top is not target:  # shed any modals above it
                self.pop(None)
            self.request_paint()
            return
        if surface is not None:
            while self.top is not surface:
                self.pop(None)
            self.replace_top(target)
        else:
            while len(self._stack) > 1:  # shed modals sitting over bare home
                self.pop(None)
            self.push(target, on_result=lambda _res: self._on_tab_view_dismissed())

    def _apply_tab_switch(self) -> None:
        # Leaving an embedded claude session: detach it (keeps running in
        # tmux) and remember the sid so returning to that tab auto-resumes
        # (port of app._push_detail_for_tab's ClaudeSessionScreen branch).
        top = self.top
        if isinstance(top, ClaudeSessionView):
            if top.ws is not None and top.ws.id:
                self._tab_active_session[top.ws.id] = top.session_id
            top.go_back()  # pops + fires _on_session_dismissed
        tab = self.tabs.active_tab
        if tab.ws_id:
            ws = self.state.get_ws(tab.ws_id)
            if ws is not None:  # archived/deleted ws: stay put (as app.py)
                self._show_tab_surface(self._ensure_detail_view(ws))
                self._maybe_resume_tab_session(ws)
        elif tab.id == "current_sessions":
            if self._current_sessions_view is None:
                self._current_sessions_view = CurrentSessionsView(self.state, self.tabs)
            self._show_tab_surface(self._current_sessions_view)
        else:
            self._show_tab_surface(None)
        self.request_paint()

    def _maybe_resume_tab_session(self, ws) -> None:
        """Auto-resume the session that was open when this tab was last
        left (port of app._finish_tab_switch's _tab_active_session pop)."""
        sid = self._tab_active_session.pop(ws.id, None)
        if sid:
            self._spawn(self._auto_resume_tab_session(ws, sid))

    async def _auto_resume_tab_session(self, ws, session_id: str) -> None:
        """Check the tmux session is still alive, then resume it. The
        ws-ID guard prevents a stale callback from pushing a session onto
        the wrong tab if the user switched again before this ran."""
        alive = await asyncio.to_thread(TerminalHost.tmux_session_alive, session_id)
        if alive and self.tabs.active_tab.ws_id == ws.id:
            self.launch_claude_session(ws, session_id=session_id)

    def action_next_tab(self) -> None:
        if self.tabs.next_tab():
            self._apply_tab_switch()

    def action_prev_tab(self) -> None:
        if self.tabs.prev_tab():
            self._apply_tab_switch()

    def action_close_tab(self) -> None:
        """Close the active tab (permanent tabs 0/1 can't close)."""
        closed = self.tabs.close_active_tab()
        if closed:
            self._tab_active_session.pop(closed, None)
            evicted = self._detail_cache.pop(closed, None)
            self._apply_tab_switch()
            if evicted is not None:
                evicted.cancel_timers()

    def _on_tab_view_dismissed(self) -> None:
        """Tab surface dismissed (esc/^H) — back to the home tab (port of
        app._on_detail_dismissed; the ws tab stays open in the bar)."""
        self.tabs.switch_to(0)
        self.home._on_return_from_modal()

    # ── command palette (port of app.action_command_palette) ─────

    def _context_ws(self):
        """The detail view's ws when one is active, else home's selection."""
        detail = self._active_detail_view()
        return detail.ws if detail else self.home._selected_ws()

    def open_command_palette(self) -> None:
        from state import get_command_items
        from .views.modals import FuzzyModalView

        view = FuzzyModalView(title="Command Palette")
        view._get_items = lambda: get_command_items(self._context_ws() is not None)

        def on_cmd(cmd_name) -> None:
            if cmd_name:
                self._execute_command(cmd_name)

        self.push(view, on_result=on_cmd)

    def _execute_command(self, cmd_text: str) -> None:
        """Port of app._execute_command: ws-specific commands delegate to
        the active DetailView when one is open."""
        ws = self._context_ws()
        result = self.state.execute_command(cmd_text, ws.id if ws else None)
        action = result.get("action", "noop")
        msg = result.get("msg", "")
        detail = self._active_detail_view()
        home = self.home

        if action == "refresh":
            home.refresh_rows()
            if detail:
                detail._refresh()
            if msg:
                self.notify(msg)
        elif action in ("notify", "error"):
            self.notify(msg, severity="error" if action == "error" else "information")
        elif action == "add":
            home._action_add()
        elif action == "rename":
            if detail:
                self.notify("Use 'E' to rename from detail view", timeout=2)
            else:
                self.notify("Rename lands in P4")
        elif action == "open":
            (detail._action_open_links if detail else home._action_open_links)()
        elif action == "spawn":
            (detail._action_spawn if detail else home._action_spawn)()
        elif action == "resume":
            (detail._action_resume if detail else home._action_resume)()
        elif action == "export":
            output, count = self.state.do_export(result.get("path", ""))
            self.notify(f"Exported {count} workstreams to {output}")
        elif action == "brain":
            text = result.get("text", "")
            if text:
                home._do_brain(text)
            else:
                home._action_brain_dump()
        elif action == "close":
            self.action_close_tab()
        elif action == "help":
            home._action_help()
        elif action == "delete":
            home._action_delete()
        elif action == "unarchive":
            home._action_toggle_archive()
        elif action == "trash":
            home._action_view_trash()
        elif action in ("ship", "ticket", "ticket-create", "branches",
                        "files", "git-action", "solve", "worktree", "rr"):
            self.notify("Dev-workflow actions land in P4")

    def kick_pollers(self) -> None:
        """User-initiated refresh ('R'): re-run the data pollers now."""
        if not self._bg_started:
            return
        self._run_in_thread("poll_sessions", self._do_poll_sessions)
        self._run_in_thread("tmux", self._do_tmux_check)
        self._run_in_thread("git_status", self._do_git_status_check)

    def _run_in_thread(self, group: str, fn, *args) -> None:
        """Spawn fn on a worker thread within an exclusive group."""

        async def runner():
            await asyncio.to_thread(fn, *args)

        self.exclusive(group, runner())

    def _data_changed(self) -> None:
        self.home.on_data_changed()
        # The active tab surface refreshes too (the engine's SessionsChanged);
        # hidden cached views catch up in on_show instead.
        surface = self._tab_surface()
        if surface is not None and hasattr(surface, "on_data_changed"):
            surface.on_data_changed()

    # ── session discovery ─────────────────────────────────────────

    @staticmethod
    def _session_fingerprint(sessions):
        return {(s.session_id, s.is_live, s.last_message_role, s.last_activity)
                for s in sessions}

    def _do_poll_sessions(self) -> None:
        """Thread worker: discover threads/sessions and apply if changed.

        AI enrichment (thread naming, session titling, description refresh)
        is not ported yet — cached names still apply; fresh naming lands
        with the P4 screens (noted in MIGRATION.md).
        """
        from threads import discover_threads
        from thread_namer import apply_cached_names

        threads = discover_threads()
        apply_cached_names(threads)
        sessions = []
        for t in threads:
            sessions.extend(t.sessions)
        sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
        if self._session_fingerprint(self.state.sessions) == self._session_fingerprint(sessions):
            return
        self.call_from_thread(self._apply_sessions, sessions, threads)

    def _apply_sessions(self, sessions, threads) -> None:
        self.state.update_sessions(sessions, threads)
        self._data_changed()

    # ── Rust engine bridge (rate-limited, 1/s trailing edge) ─────

    def _on_rust_engine_update(self) -> None:
        now = time.monotonic()
        if now - self._last_rust_update < 1.0:
            if not self._rust_update_pending:
                self._rust_update_pending = True
                self.set_timer(1.0, self._fire_deferred_rust_update)
            return
        self._last_rust_update = now
        self._rust_update_pending = False
        self._run_in_thread("rust_engine", self._do_refresh_from_db)

    def _fire_deferred_rust_update(self) -> None:
        self._rust_update_pending = False
        self._last_rust_update = time.monotonic()
        self._run_in_thread("rust_engine", self._do_refresh_from_db)

    def _do_refresh_from_db(self) -> None:
        # Guard against overlapping workers: exclusive() cancels the awaiting
        # task but cannot reach a body already inside to_thread.
        if self._db_refresh_running:
            return
        self._db_refresh_running = True
        try:
            self._do_poll_sessions()
        finally:
            self._db_refresh_running = False

    # ── liveness (watchdog fallback path, 2s rate limit) ─────────

    def _on_liveness_file_change(self) -> None:
        from sessions import invalidate_live_session_cache
        invalidate_live_session_cache()
        self._refresh_session_liveness()

    def _on_session_file_change(self) -> None:
        self._refresh_session_liveness()

    def _refresh_session_liveness(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_liveness_check
        if elapsed < 2.0:
            if not self._liveness_deferred:
                self._liveness_deferred = True
                self.set_timer(2.0 - elapsed, self._fire_deferred_liveness)
            return
        self._last_liveness_check = now
        self._liveness_deferred = False
        self._run_in_thread("liveness", self._do_refresh_liveness)

    def _fire_deferred_liveness(self) -> None:
        self._liveness_deferred = False
        self._last_liveness_check = time.monotonic()
        self._run_in_thread("liveness", self._do_refresh_liveness)

    def _do_refresh_liveness(self) -> None:
        changed = self.state.refresh_liveness()
        if changed:
            self.call_from_thread(self._apply_liveness_change)

    def _apply_liveness_change(self) -> None:
        self._data_changed()

    # ── tmux / git / worktree pollers ─────────────────────────────

    def _do_tmux_check(self) -> None:
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

    def _apply_tmux_status(self, paths: set[str], names: set[str]) -> None:
        if self.state.update_tmux_status(paths, names):
            self._data_changed()

    def _do_git_status_check(self) -> None:
        from actions import get_worktree_git_status

        repo_paths = {ws.repo_path for ws in self.state.store.active if ws.repo_path}
        new_cache = {path: get_worktree_git_status(path) for path in repo_paths}
        old = self.state.git_status_cache
        if set(old.keys()) != set(new_cache.keys()) or any(
            getattr(new_cache.get(k), attr, None) != getattr(old.get(k), attr, None)
            for k in new_cache
            for attr in ("is_dirty", "branch", "ahead", "behind")
        ):
            self.call_from_thread(self._apply_git_status, new_cache)

    def _apply_git_status(self, new_cache: dict) -> None:
        self.state.git_status_cache = new_cache
        self._data_changed()

    def _do_worktree_check(self) -> None:
        changed = self.state.discover_and_enrich_worktrees()
        changed = self.state.discover_non_repo_workstreams() or changed
        if changed:
            self.call_from_thread(self._data_changed)

    def _auto_cleanup_idle_sessions(self) -> None:
        from cleanup import cleanup_idle_orch_sessions
        try:
            killed = cleanup_idle_orch_sessions(self._idle_cleanup_hours)
        except Exception:
            return
        if killed:
            n = len(killed)
            self.call_from_thread(
                self.notify,
                f"Auto-killed {n} idle Claude session{'s' if n != 1 else ''} "
                f"(>{self._idle_cleanup_hours:g}h)",
            )

    # ── claude session launch (embedded ClaudeSessionView, P5) ───

    @staticmethod
    def _trace_spawn(msg: str) -> None:
        """Spawn diagnostics (port of app.launch_claude_session's _trace)."""
        try:
            import datetime
            log = Path.home() / ".cache" / "claude-orchestrator" / "spawn-debug.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as fh:
                fh.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
        except Exception:
            pass

    def launch_claude_session(self, ws, session_id: str | None = None,
                              prompt: str | None = None, cwd: str | None = None,
                              callback=None, reuse_pending: bool = True,
                              auto_role: str | None = None) -> None:
        """Push a ClaudeSessionView for the given workstream (port of
        app.launch_claude_session).

        reuse_pending: when True (default), a session_id=None call may be
        upgraded to a resume of a still-pending session for this ws (so
        pressing "c" twice returns to the same thread). Task-dispatch
        callers set this False — a todo is a new task, never a
        continuation of whatever fresh session happens to be cached.

        auto_role: when set ("coordinator" or "implementer"), tag the
        resulting session id under that role for this workstream so the
        detail view can style it.
        """
        self._spawn(self._launch_claude_session(
            ws, session_id=session_id, prompt=prompt, cwd=cwd,
            callback=callback, reuse_pending=reuse_pending, auto_role=auto_role))

    async def _launch_claude_session(self, ws, *, session_id, prompt, cwd,
                                     callback, reuse_pending, auto_role) -> None:
        reattach = False
        effective_sid = session_id

        self._trace_spawn(
            f"launch_claude_session ws={ws.id}/{ws.name!r} session_id={session_id!r} "
            f"reuse_pending={reuse_pending} prompt_len={len(prompt) if prompt else 0}"
        )

        # If no explicit session_id, reuse any pending new session for this
        # ws so pressing "c", going back, then "c" returns to the same
        # thread. Pending IDs whose JSONL never appeared are dropped —
        # resuming them would make claude print "can't find session".
        if reuse_pending and session_id is None and ws.id:
            pending = self._ws_pending_session.get(ws.id)
            if pending:
                cwd_for_check = cwd or ws_working_dir(ws)
                if claude_jsonl_path(cwd_for_check, pending).exists():
                    effective_sid = pending
                    self._trace_spawn(f"  reuse_pending hit: adopting pending={pending} (jsonl exists)")
                else:
                    del self._ws_pending_session[ws.id]
                    self._trace_spawn(f"  reuse_pending hit but pending={pending} had no jsonl — dropped")

        if effective_sid:
            self._detached_sessions.pop(effective_sid, None)
            # Off-loop: tmux has-session can hang for seconds.
            reattach = await asyncio.to_thread(
                TerminalHost.tmux_session_alive, effective_sid)
            self._trace_spawn(f"  effective_sid={effective_sid} tmux_alive={reattach}")

        view = ClaudeSessionView(self.state, self.tabs, ws,
                                 session_id=effective_sid, prompt=prompt,
                                 cwd=cwd, reattach_tmux=reattach)
        self._trace_spawn(
            f"  view created: is_new={view._is_new} session_id={view.session_id} "
            f"reattach_tmux={reattach} cmd_flag={'--session-id' if view._is_new else '--resume'}"
        )

        # Remember the session ID so "c" returns to the same thread next
        # time. Skipped for task-dispatch spawns (reuse_pending=False).
        if reuse_pending and session_id is None and ws.id:
            self._ws_pending_session[ws.id] = view.session_id

        if auto_role and ws.id:
            sid = view.session_id
            if auto_role == "coordinator":
                self._auto_coord_sid[ws.id] = sid
            elif auto_role == "implementer":
                self._auto_impl_sids.setdefault(ws.id, set()).add(sid)

        self.push(view, on_result=lambda result: self._on_session_dismissed(
            view, ws, result, callback))

    def _on_session_dismissed(self, view, ws, result, callback=None) -> None:
        """Port of app.py:2437's dismiss branches."""
        if isinstance(result, dict) and result.get("detached"):
            sid = result["session_id"]
            self._detached_sessions[sid] = {
                "ws": result["ws"],
                "start_time": result["start_time"],
                "jsonl": result["jsonl"],
            }
            # Parse the JSONL so we can inject the session immediately.
            # Mark is_live=True since the process is still running
            # (detached, not killed) — parse_session doesn't check PIDs.
            jsonl = result.get("jsonl")
            if jsonl:
                try:
                    s = parse_session(Path(jsonl))
                    if s:
                        s.is_live = True
                        self._inject_session(s)
                        if s.message_count and ws.id:
                            # Persist the session→ws link so directory-less
                            # workstreams can rediscover it after detach.
                            auto_link_session(self.state.store, ws.id, view.session_id)
                            # A real thread now — next "c" should be fresh
                            if self._ws_pending_session.get(ws.id) == view.session_id:
                                del self._ws_pending_session[ws.id]
                except Exception:
                    pass
            # Highlight the session we just left in its detail view.
            detail = self._detail_cache.get(ws.id) if ws.id else None
            if detail is not None:
                detail.request_session_highlight(sid)
        elif isinstance(result, ClaudeSession):
            self.notify(
                f"{result.model_short} | {result.message_count} msgs | "
                f"{result.tokens_display}",
                timeout=5,
            )
            self._inject_session(result)
            # Completed naturally — clear the pending slot, next "c" is fresh
            if ws.id and self._ws_pending_session.get(ws.id) == view.session_id:
                del self._ws_pending_session[ws.id]
        self._data_changed()
        self.request_paint()
        if callback:
            callback(result)

    def _inject_session(self, session: ClaudeSession) -> None:
        """Inject a session into state immediately so the detail view
        updates without polling (port of app._inject_session)."""
        existing = {s.session_id for s in self.state.sessions}
        if session.session_id in existing:
            for i, s in enumerate(self.state.sessions):
                if s.session_id == session.session_id:
                    self.state.sessions[i] = session
                    break
        else:
            self.state.sessions.insert(0, session)

        # Inject into the matching thread (sessions_for_ws uses threads)
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

        # No matching thread: create a minimal one so it's discoverable
        if not injected and sp:
            from threads import Thread
            self.state.threads.append(Thread(
                thread_id=session.session_id,
                name=sp.rsplit("/", 1)[-1],
                project_path=sp,
                sessions=[session],
            ))

        self.state.invalidate_caches()

    # ── auto mode (coordinator/implementer loop, port of app.py) ──

    def auto_role_for(self, ws_id: str, sid: str) -> str | None:
        """Return 'coordinator', 'implementer', or None for a session.

        Used by the detail view to badge sessions spawned by an auto-mode
        loop. Checks the in-memory dicts first (fast, authoritative for
        this process), then the persisted state on the workstream so
        sessions spawned by another orch instance are still badged.
        """
        if not ws_id or not sid:
            return None
        if self._auto_coord_sid.get(ws_id) == sid:
            return "coordinator"
        if sid in self._auto_impl_sids.get(ws_id, ()):
            return "implementer"
        ws = self.state.store.get(ws_id)
        if ws is None:
            return None
        if ws.auto_coord_sid == sid:
            return "coordinator"
        if sid in ws.auto_impl_sids:
            return "implementer"
        return None

    def toggle_auto_mode(self, ws_id: str, screen_session_id: str) -> None:
        """Cancel an existing auto-mode loop for this ws, or start a new
        one (port of app.toggle_auto_mode; reached via the SESSION_KEYS
        toggle_auto_mode key on a ClaudeSessionView).

        Starting is never one keypress: a non-empty crystallized-undone
        backlog opens AutoModeStartView to pick which todos to run (Esc
        backs out), and with no backlog a ConfirmView asks first.
        """
        if not ws_id:
            self.notify("[auto] workstream has no id", timeout=3)
            return

        running = self._auto_modes.get(ws_id)
        if running is not None:
            running.cancel()
            self.notify("[auto] canceling — exiting now (in-flight implementers keep running)", timeout=3)
            return

        ws = self.state.store.get(ws_id)
        if ws is None:
            self.notify("[auto] workstream not found", timeout=3)
            return

        # A loop owned by a DIFFERENT orch process? Set the cross-process
        # cancel flag instead of starting a duplicate; clear stale state
        # from a dead owner and proceed.
        if ws.auto_running and ws.auto_pid:
            if ws.auto_pid_alive:
                ws.auto_cancel_requested = True
                self.state.store.update(ws)
                self.notify(
                    f"[auto] cancel signal sent to pid {ws.auto_pid} (another orch owns this loop)",
                    timeout=4,
                )
                return
            else:
                dead_pid = ws.auto_pid
                ws.auto_running = False
                ws.auto_pid = 0
                ws.auto_cancel_requested = False
                self.state.store.update(ws)
                self.notify(
                    f"[auto] cleared stale state from pid {dead_pid} (process dead)",
                    timeout=3,
                )

        backlog = [t for t in ws.todos if not t.done and not t.archived]
        backlog_ids = {t.id for t in backlog}

        if not backlog_ids:
            # No backlog to pick from, so the picker never appears — ask
            # outright before spawning a coordinator loop.
            from rendering import C_DIM, _rich_escape

            from .views.confirm import ConfirmView

            def on_confirm(ok) -> None:
                if ok:
                    self._start_auto_mode(ws_id, screen_session_id, skip_ids=set())

            self.push(
                ConfirmView(
                    f"Start auto mode on [bold]{_rich_escape(ws.name)}[/bold]?\n"
                    f"\n"
                    f"[{C_DIM}]No pending todos — the coordinator[/{C_DIM}]\n"
                    f"[{C_DIM}]runs unattended until you stop it.[/{C_DIM}]"
                ),
                on_result=on_confirm,
            )
            return

        from .views.auto_mode_start import AutoModeStartView

        def on_choice(selected) -> None:
            # selected: set[str] of todo IDs to RUN, or None on cancel
            if selected is None:
                return
            skip_ids = backlog_ids - selected
            self._start_auto_mode(ws_id, screen_session_id, skip_ids=skip_ids)

        self.push(AutoModeStartView(ws.name, backlog), on_result=on_choice)

    def _start_auto_mode(self, ws_id: str, screen_session_id: str, skip_ids: set) -> None:
        coord_sid = screen_session_id
        prior = self._auto_coord_sid.get(ws_id)
        if prior and prior != screen_session_id:
            try:
                if TerminalHost.tmux_session_alive(prior):
                    coord_sid = prior
            except Exception:
                pass
        self._auto_coord_sid[ws_id] = coord_sid
        self.exclusive("auto_mode", self._run_auto_mode(ws_id, coord_sid, skip_ids))

    async def _run_auto_mode(self, ws_id: str, coord_sid: str,
                             skip_ids: set | None = None) -> None:
        from auto_mode import AutoMode
        from session_launch import log_session_exit, spawn_implementer_session

        def inject(text: str) -> None:
            # Write to the coordinator's tmux session directly so we don't
            # depend on its pane still being attached. Bracketed-paste
            # markers keep embedded newlines as paste content; Enter is
            # sent separately to submit.
            paste = f"\x1b[200~{text}\x1b[201~"
            try:
                subprocess.run(
                    ["tmux", "-L", TerminalHost.TMUX_SOCKET,
                     "send-keys", "-t", coord_sid, "-l", paste],
                    timeout=5, capture_output=True, check=True,
                )
                subprocess.run(
                    ["tmux", "-L", TerminalHost.TMUX_SOCKET,
                     "send-keys", "-t", coord_sid, "Enter"],
                    timeout=5, capture_output=True, check=True,
                )
            except Exception as e:
                self.notify(f"[auto] inject failed: {e}", timeout=4)

        async def spawn_implementer(todo, brief: str) -> None:
            """Spawn an implementer headlessly (tmux session, no UI view).

            Resolves when ANY of: the todo's report field is written, the
            tmux session dies, or auto-mode is canceled. Transient
            store-read failures don't count as a resolution.
            """
            ws = self.state.store.get(ws_id)
            if ws is None:
                return

            start_time = time.time()
            try:
                sid, _jsonl_path = await asyncio.to_thread(
                    spawn_implementer_session, ws, self.state.store, brief)
            except Exception as e:
                self.notify(f"[auto] implementer spawn failed: {e}", timeout=6)
                return

            # Tag for badging: in-memory (fast UI read) and persisted (so
            # other orch instances / CLI see who's running what). Persist
            # best-effort — never fail the spawn on a store hiccup.
            if ws_id:
                self._auto_impl_sids.setdefault(ws_id, set()).add(sid)
                try:
                    self.state.store.load(force=True)
                    cur_ws = self.state.store.get(ws_id)
                    if cur_ws is not None and sid not in cur_ws.auto_impl_sids:
                        cur_ws.auto_impl_sids.append(sid)
                        self.state.store.update(cur_ws)
                except Exception:
                    pass

            async def wait_for_report():
                while True:
                    try:
                        self.state.store.load(force=True)
                    except Exception:
                        await asyncio.sleep(2)
                        continue
                    cur_ws = self.state.store.get(ws_id)
                    if cur_ws is None:
                        await asyncio.sleep(2)
                        continue
                    cur = next((t for t in cur_ws.todos if t.id == todo.id), None)
                    if cur is None:
                        await asyncio.sleep(2)
                        continue
                    if cur.report:
                        return
                    await asyncio.sleep(2)

            async def wait_for_tmux_exit():
                while True:
                    alive = await asyncio.to_thread(
                        TerminalHost.tmux_session_alive, sid)
                    if not alive:
                        return
                    await asyncio.sleep(3)

            report_task = asyncio.create_task(wait_for_report())
            exit_task = asyncio.create_task(wait_for_tmux_exit())
            cancel_task = asyncio.create_task(mode.cancel_event.wait())
            try:
                await asyncio.wait(
                    [report_task, exit_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task.done():
                    return
                if exit_task.done() and not report_task.done():
                    # Claude exited without writing a report — do the same
                    # post-exit bookkeeping the session view does.
                    auto_link_session(self.state.store, ws_id, sid)
                    log_session_exit(sid, ws.name, start_time, exit_type="headless")
                if report_task.done() and not exit_task.done():
                    self.notify(
                        "[auto] report received — advancing while implementer wraps up",
                        timeout=4,
                    )
            finally:
                report_task.cancel()
                exit_task.cancel()
                cancel_task.cancel()

        def notify(msg: str) -> None:
            self.notify(f"[auto] {msg}", timeout=4)

        mode = AutoMode(
            store=self.state.store,
            ws_id=ws_id,
            spawn_implementer=spawn_implementer,
            inject_coordinator=inject,
            notify=notify,
            skip_todo_ids=skip_ids or set(),
            coord_sid=coord_sid,
        )
        self._auto_modes[ws_id] = mode
        if skip_ids:
            self.notify(
                f"[auto] auto mode started (skipping {len(skip_ids)} backlog todos)",
                timeout=4,
            )
        else:
            self.notify("[auto] auto mode started", timeout=3)

        # Watchdog: detect & auto-respond to claude usage-quota prompts.
        watchdog_cancel = asyncio.Event()
        watchdog_task = asyncio.create_task(
            self._watch_quota_stalls(ws_id, watchdog_cancel))

        try:
            result = await mode.run()
            self.notify(f"[auto] loop ended: {result}", timeout=6)
        except Exception as e:
            self.notify(f"[auto] loop error: {e}", timeout=6)
        finally:
            watchdog_cancel.set()
            try:
                await asyncio.wait_for(watchdog_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                watchdog_task.cancel()
            self._auto_modes.pop(ws_id, None)

    async def _watch_quota_stalls(self, ws_id: str, cancel) -> None:
        """Detect & auto-respond to quota stalls in any live session for
        this workstream while auto-mode runs (port of app.py). Requires
        two consecutive observations before injecting Enter; won't
        re-fire on the same session until the stall clears.
        """
        from auto_mode import detect_quota_stall

        INTERVAL = 15.0
        OBSERVATIONS_BEFORE_RESPOND = 2

        observed: dict[str, int] = {}   # sid → consecutive stall observations
        responded: set[str] = set()     # sids we've already injected for

        while not cancel.is_set():
            try:
                await asyncio.wait_for(cancel.wait(), timeout=INTERVAL)
                return  # canceled
            except asyncio.TimeoutError:
                pass  # interval elapsed; do a poll

            try:
                ws = self.state.store.get(ws_id)
                if ws is None:
                    continue
                sessions = self.state.sessions_for_ws(ws)
                live_sids = {s.session_id for s in sessions if s.is_live}

                for sid in list(observed):
                    if sid not in live_sids:
                        observed.pop(sid, None)
                        responded.discard(sid)

                for s in sessions:
                    if not s.is_live:
                        continue
                    sid = s.session_id
                    r = await asyncio.to_thread(
                        subprocess.run,
                        ["tmux", "-L", TerminalHost.TMUX_SOCKET,
                         "capture-pane", "-t", sid, "-p", "-S", "-100"],
                        timeout=3, capture_output=True, text=True,
                    )
                    if r.returncode != 0:
                        continue
                    if detect_quota_stall(r.stdout):
                        observed[sid] = observed.get(sid, 0) + 1
                        if (observed[sid] >= OBSERVATIONS_BEFORE_RESPOND
                                and sid not in responded):
                            self.notify(
                                f"[auto] quota stall in {sid[:8]} — sending Enter to wait",
                                timeout=8,
                            )
                            try:
                                subprocess.run(
                                    ["tmux", "-L", TerminalHost.TMUX_SOCKET,
                                     "send-keys", "-t", sid, "Enter"],
                                    timeout=5, capture_output=True, check=True,
                                )
                                responded.add(sid)
                            except Exception as e:
                                self.notify(f"[auto] watchdog inject failed: {e}", timeout=4)
                    else:
                        if sid in responded:
                            self.notify(f"[auto] {sid[:8]} resumed", timeout=4)
                        observed.pop(sid, None)
                        responded.discard(sid)
            except asyncio.CancelledError:
                return
            except Exception:
                # Best-effort detection — never nuke the loop.
                pass
