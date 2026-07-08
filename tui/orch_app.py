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
        if self._loop is not None:
            self._toast_timer = Timer(timeout, self._clear_toast, repeat=False)
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
                # Bare Timer (not set_timer): app timers are never removed
                # from the registry, and this path can fire once per second
                # for hours during heavy streaming.
                Timer(1.0, self._fire_deferred_rust_update, repeat=False)
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
                Timer(2.0 - elapsed, self._fire_deferred_liveness, repeat=False)
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
