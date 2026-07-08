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
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from actions import ui_is_visible, ws_working_dir
from models import Store
from session_bridge import SessionBridge
from state import AppState, TabManager

from .app import App
from .view import Timer
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
        tab = self.tabs.active_tab
        if tab.ws_id:
            ws = self.state.get_ws(tab.ws_id)
            if ws is not None:  # archived/deleted ws: stay put (as app.py)
                self._show_tab_surface(self._ensure_detail_view(ws))
        elif tab.id == "current_sessions":
            if self._current_sessions_view is None:
                self._current_sessions_view = CurrentSessionsView(self.state, self.tabs)
            self._show_tab_surface(self._current_sessions_view)
        else:
            self._show_tab_surface(None)
        self.request_paint()

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

    # ── claude session launch (suspend-attach fallback) ──────────

    def launch_claude_session(self, ws, session_id: str | None = None,
                              prompt: str | None = None, cwd: str | None = None,
                              callback=None, **_kwargs) -> None:
        """Full-fidelity fallback until the embedded pane lands in P5:
        run/attach the session on the orch-sessions tmux socket and
        suspend the UI over a foreground ``tmux attach``.
        """
        from term_host import TerminalHost

        is_new = session_id is None
        sid = session_id or str(uuid.uuid4())
        cwd = cwd or ws_working_dir(ws)

        if not TerminalHost.tmux_session_alive(sid):
            # Create the persistent session (same command/env construction as
            # ClaudeSessionScreen; the tmux server owns the PTY).
            from claude_session_screen import (
                build_claude_command, build_session_context, build_session_env,
            )
            cmd = build_claude_command(
                session_id=sid, cwd=cwd,
                sys_prompt=build_session_context(ws),
                prompt=prompt, ws_name=ws.name, is_new=is_new,
            )
            env_vars = build_session_env(ws.id or "", sid)
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_vars.items())
            inner_cmd = f"env TERM=xterm-256color COLORTERM=truecolor {env_prefix} {cmd}"
            env = os.environ.copy()
            env.update(TERM="xterm-256color", COLORTERM="truecolor")
            env.pop("TMUX", None)
            w, h = self._size if self._size != (0, 0) else (200, 50)
            result = subprocess.run(
                ["tmux", "-L", TerminalHost.TMUX_SOCKET,
                 "-f", TerminalHost._tmux_conf_path(),
                 "new-session", "-d", "-s", sid,
                 "-x", str(max(80, w)), "-y", str(max(24, h)),
                 "-c", cwd, inner_cmd],
                env=env, capture_output=True, text=True, timeout=10,
            )
            err = (result.stderr or "").strip()
            if result.returncode != 0 and "duplicate session" not in err.lower():
                self.notify(f"Session launch failed: {err or '(no stderr)'}")
                if callback:
                    callback(None)
                return
            TerminalHost._reload_tmux_config(env)

        attach_env = os.environ.copy()
        attach_env.pop("TMUX", None)
        with self.suspend():
            subprocess.run(
                ["tmux", "-L", TerminalHost.TMUX_SOCKET, "attach", "-t", sid],
                env=attach_env,
            )

        # Back from the session: pick up whatever it changed.
        self.state.store.load()
        self.state._last_seen_valid = False
        self._data_changed()
        if self._bg_started:
            self._run_in_thread("poll_sessions", self._do_poll_sessions)
        if callback:
            callback(None)
