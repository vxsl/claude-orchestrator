"""Structural perf tests for the throbber tick / bar rendering path.

These tests protect against the "throbber storm" class of bug where every
150ms tick triggers redundant passes over tabs + sessions. They assert
STRUCTURAL call-count budgets rather than wall-clock time — stable across
hardware, and they catch architecture drift (e.g. someone re-introducing a
duplicate _update_all_bars call, or breaking the _tab_activity_cache
contract).

Background: fixed in commit 7a944b7 (perf: eliminate throbber-tick storm
in bar rendering). Without this test, the existing per-op benchmarks would
not catch a regression because the bug was interaction (many cheap ops run
too many times), not any single op being slow.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app import OrchestratorApp
from models import Category, Store, Workstream
from sessions import ClaudeSession
from state import AppState, TabManager
from threads import Thread


def _thinking_session(session_id: str, project_path: str) -> ClaudeSession:
    """A live, mid-turn session — classified as THINKING by session_activity."""
    return ClaudeSession(
        session_id=session_id,
        project_dir=project_path.replace("/", "-").lstrip("-"),
        project_path=project_path,
        jsonl_path=f"/tmp/fake/{session_id}.jsonl",
        started_at="2026-04-24T10:00:00Z",
        last_activity=datetime.now(timezone.utc).isoformat(),
        last_message_role="assistant",
        last_stop_reason="",       # empty = turn not yet terminated
        turn_complete=False,
        message_count=3,
        is_live=True,
    )


def _thread_for(session: ClaudeSession) -> Thread:
    return Thread(
        thread_id=session.session_id,
        name=session.title or "t",
        project_path=session.project_path,
        sessions=[session],
    )


def _make_test_app(tmp_path, num_tabs: int, num_thinking: int):
    """Build a minimal OrchestratorApp-like object for render-path testing.

    Skips Textual mount via __new__; stubs the few widget-tree touches the
    bar renderers make. Everything else (state, tabs, sessions) uses real
    production types so the render path runs for real."""
    store = Store(path=tmp_path / "data.json")
    workstreams: list[Workstream] = []
    for i in range(num_tabs):
        ws = Workstream(name=f"ws-{i}", category=Category.WORK)
        ws.add_link("worktree", f"/tmp/proj/ws-{i}", f"ws-{i}")
        store.add(ws)
        workstreams.append(ws)

    sessions = []
    threads = []
    for i in range(num_thinking):
        ws = workstreams[i % len(workstreams)]
        s = _thinking_session(f"sess-{i:08d}-0000-0000-0000-000000000000",
                              f"/tmp/proj/ws-{i % len(workstreams)}")
        sessions.append(s)
        t = _thread_for(s)
        threads.append(t)
        # Pin the session's thread to its workstream directly — avoids the
        # directory-based matching path, which requires real on-disk dirs.
        if t.thread_id not in ws.thread_ids:
            ws.thread_ids.append(t.thread_id)

    state = AppState(store)
    state.update_sessions(sessions, threads)

    tabs = TabManager()
    for ws in workstreams:
        tabs.open_tab(ws.id, ws.name)
    tabs.active_idx = 0  # home

    app = OrchestratorApp.__new__(OrchestratorApp)
    app.state = state
    app.tabs = tabs
    app._tab_activity_cache = None
    app._ws_activity_cache = {}
    app._throbber_timer = None
    app._throbber_paused = False
    app._last_top_bar = ""
    app._last_filter_bar = ""
    app._last_summary_bar = ""
    # Stub widget-tree accesses so render path runs end-to-end. We don't
    # mock `screen` — it's a property — and _sync_tab_bar's try/except
    # cleanly swallows the AttributeError raised on access, which just
    # skips the DetailScreen tab-bar update. Not relevant for these tests.
    app.query_one = MagicMock(return_value=MagicMock())
    app._active_table = MagicMock(return_value=MagicMock(option_count=len(workstreams)))
    return app, workstreams


class TestTabActivityCache:
    def test_cache_memoizes_within_pass(self, tmp_path):
        """Inside a pass, _tab_activity should compute once per ws_id."""
        app, wss = _make_test_app(tmp_path, num_tabs=5, num_thinking=3)

        compute_calls = []
        original = app._compute_tab_activity

        def counting(ws_id):
            compute_calls.append(ws_id)
            return original(ws_id)

        app._compute_tab_activity = counting

        app._tab_activity_cache = {}
        try:
            for _ in range(10):
                for ws in wss:
                    app._tab_activity(ws.id)
        finally:
            app._tab_activity_cache = None

        assert len(compute_calls) == len(wss), (
            f"expected 1 compute per tab (={len(wss)}), got {len(compute_calls)}"
        )

    def test_no_cache_means_uncached(self, tmp_path):
        """Sanity check: with the cache disabled every call recomputes."""
        app, wss = _make_test_app(tmp_path, num_tabs=2, num_thinking=1)

        compute_calls = []
        original = app._compute_tab_activity

        def counting(ws_id):
            compute_calls.append(ws_id)
            return original(ws_id)

        app._compute_tab_activity = counting

        app._tab_activity_cache = None
        for _ in range(5):
            app._tab_activity(wss[0].id)

        assert len(compute_calls) == 5


class TestThrobberTickBudget:
    """The critical structural assertion: one throbber tick should run
    each expensive operation at most a bounded number of times, regardless
    of how many tabs or thinking sessions are in play."""

    def test_update_all_bars_runs_once_per_tick(self, tmp_path):
        """Before commit 7a944b7 this was 2 (tick called it, then
        _sync_tab_bar called it again). Must stay at 1 going forward."""
        app, wss = _make_test_app(tmp_path, num_tabs=6, num_thinking=2)

        calls = {"update_all_bars": 0}
        original = app._update_all_bars

        def counting():
            calls["update_all_bars"] += 1
            return original()

        app._update_all_bars = counting
        app._tick_throbber()

        assert calls["update_all_bars"] == 1, (
            "_update_all_bars must run at most once per throbber tick; "
            f"got {calls['update_all_bars']}"
        )

    def test_compute_tab_activity_bounded_by_tab_count(self, tmp_path):
        """For N tabs, a single throbber tick should compute each tab's
        activity at most once (not N*k times as before caching)."""
        app, wss = _make_test_app(tmp_path, num_tabs=8, num_thinking=3)

        compute_calls = []
        original = app._compute_tab_activity

        def counting(ws_id):
            compute_calls.append(ws_id)
            return original(ws_id)

        app._compute_tab_activity = counting
        app._tick_throbber()

        per_tab = {}
        for wid in compute_calls:
            per_tab[wid] = per_tab.get(wid, 0) + 1
        worst = max(per_tab.values()) if per_tab else 0
        assert worst <= 1, (
            f"no tab should be recomputed more than once per tick; "
            f"worst={worst} distribution={per_tab}"
        )

    def test_activity_cache_skips_heavy_work_across_ticks(self, tmp_path):
        """When session attrs don't change between ticks, the per-ws fingerprint
        cache should short-circuit _best_activity / _is_session_seen — the
        actual cost we're saving."""
        import rendering as r

        app, wss = _make_test_app(tmp_path, num_tabs=6, num_thinking=2)

        best_calls = 0
        original_best = r._best_activity

        def counting(sessions, last_seen=None):
            nonlocal best_calls
            best_calls += 1
            return original_best(sessions, last_seen)

        r._best_activity = counting
        try:
            import app as app_mod
            original_in_app = app_mod._best_activity
            app_mod._best_activity = counting
            try:
                # Tick 1: cold cache — every tab recomputes.
                app._tick_throbber()
                cold = best_calls
                # Tick 2: warm cache, no session attrs changed — every tab
                # should hit the fingerprint cache and skip _best_activity.
                app._tick_throbber()
                warm = best_calls - cold
            finally:
                app_mod._best_activity = original_in_app
        finally:
            r._best_activity = original_best

        assert warm == 0, (
            f"second tick with unchanged sessions should hit the activity "
            f"fingerprint cache and skip _best_activity entirely; "
            f"got {warm} calls (cold tick: {cold})"
        )

    def test_status_bar_single_pass_over_sessions(self, tmp_path):
        """_render_status_bar should walk state.sessions exactly once.
        Previously it walked 2N (thinking count + your-turn count) plus a
        genexp for tokens."""
        import threads as threads_mod

        app, _ = _make_test_app(tmp_path, num_tabs=3, num_thinking=5)

        call_count = 0
        original_activity = threads_mod.session_activity

        def counting(session, last_seen=None):
            nonlocal call_count
            call_count += 1
            return original_activity(session, last_seen)

        threads_mod.session_activity = counting
        try:
            # Also reimport inside app's namespace since it was `from threads
            # import session_activity`, not `import threads`.
            import app as app_mod
            original_in_app = app_mod.session_activity
            app_mod.session_activity = counting
            try:
                app._render_status_bar()
            finally:
                app_mod.session_activity = original_in_app
        finally:
            threads_mod.session_activity = original_activity

        n_sessions = len(app.state.sessions)
        assert call_count == n_sessions, (
            f"_render_status_bar should call session_activity exactly once "
            f"per session (got {call_count}, expected {n_sessions})"
        )
