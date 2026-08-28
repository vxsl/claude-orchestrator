"""HomeView + OrchApp tests on the tui engine (port of test_app.py's
home-screen classes: navigation, filters, preview toggle, tab bar,
search, quit).

OrchApp is constructed against a temp store (store_path=...) with
pollers=False so no discovery/watchers touch real Claude data. Launch
tests (spawn/resume argv) are actions-level and already covered in
test_app_logic.py — not re-ported here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sessions import ClaudeSession
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.views.current_sessions import CurrentSessionsView
from tui.views.detail import DetailView


@pytest.fixture
def home_app(populated_store):
    """OrchApp on the populated temp store, background workers off."""
    return OrchApp(store_path=populated_store.path, pollers=False)


def ws_ids(app):
    """Enabled (main) row ids in display order."""
    return [rid for rid, _, disabled in app.home.ws_list.rows if not disabled]


def make_session(n: int = 1, **kw) -> ClaudeSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        session_id=f"ccccddd{n}-0000-0000-0000-00000000000{n}",
        project_dir="-home-u-dev-proj",
        project_path="/home/u/dev/proj",
        title=f"Fake session {n}",
        started_at=(now - timedelta(minutes=60 + n)).isoformat(),
        last_activity=(now - timedelta(minutes=n)).isoformat(),
        message_count=2,
        assistant_message_count=1,
        model="claude-sonnet-4",
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


async def finish_search(h, tries: int = 200) -> None:
    """Wait for the global-search worker to drain (it runs in a thread)."""
    for _ in range(tries):
        if not h.app.home._gs_running:
            await h.pause()  # let the scheduled repaint land
            return
        await h.pause(0.01)
    raise AssertionError("global search never finished")


# ─── startup / rendering ────────────────────────────────────────────


@pytest.mark.asyncio
class TestHomeRenders:
    async def test_app_runs_and_lists_workstreams(self, home_app):
        async with Headless(home_app) as h:
            text = h.screen_text()
            assert "Active work item" in text
            assert "Personal project" in text

    async def test_tab_bar_renders_permanent_tabs(self, home_app):
        async with Headless(home_app) as h:
            first_line = h.screen_text().splitlines()[0]
            assert "Workstreams" in first_line
            assert "Sessions" in first_line

    async def test_status_bar_counts_streams(self, home_app):
        async with Headless(home_app) as h:
            assert "6 streams" in h.screen_text()

    async def test_summary_bar_counts_rows(self, home_app):
        async with Headless(home_app) as h:
            assert "6 workstreams" in h.screen_text()

    async def test_row_count_matches_store(self, home_app):
        async with Headless(home_app) as h:
            assert len(ws_ids(h.app)) == 6

    async def test_pollers_stay_off(self, home_app):
        async with Headless(home_app) as h:
            assert h.app._bg_started is False
            assert h.app._session_bridge is None
            assert h.app._session_watcher is None


# ─── navigation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestNavigation:
    async def test_j_moves_down(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            assert h.app.home.ws_list.highlighted_id == ids[0]
            await h.press("j")
            assert h.app.home.ws_list.highlighted_id == ids[1]

    async def test_k_moves_up(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("j", "j", "k")
            assert h.app.home.ws_list.highlighted_id == ids[1]

    async def test_g_goes_to_top(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("j", "j", "g")
            assert h.app.home.ws_list.highlighted_id == ids[0]

    async def test_G_goes_to_bottom(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("G")
            assert h.app.home.ws_list.highlighted_id == ids[-1]

    async def test_ctrl_n_moves_down(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("ctrl+n")
            assert h.app.home.ws_list.highlighted_id == ids[1]

    async def test_ctrl_p_moves_up(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("j", "ctrl+p")
            assert h.app.home.ws_list.highlighted_id == ids[0]

    async def test_no_wrap_at_edges(self, home_app):
        async with Headless(home_app) as h:
            ids = ws_ids(h.app)
            await h.press("k")
            assert h.app.home.ws_list.highlighted_id == ids[0]
            await h.press("G", "j")
            assert h.app.home.ws_list.highlighted_id == ids[-1]


# ─── filters & sorts ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFilters:
    async def test_filter_all(self, home_app):
        async with Headless(home_app) as h:
            await h.press("2", "1")
            assert h.app.state.filter_mode == "all"

    async def test_filter_stale(self, home_app):
        async with Headless(home_app) as h:
            await h.press("2")
            assert h.app.state.filter_mode == "stale"
            # only ws3 is stale (>24h old) in the populated store
            assert len(ws_ids(h.app)) == 1
            assert "Personal project" in h.screen_text()

    async def test_filter_archived(self, home_app):
        async with Headless(home_app) as h:
            await h.press("3")
            assert h.app.state.filter_mode == "archived"
            assert "archived" in h.screen_text()

    async def test_sort_keys_set_sort_mode(self, home_app):
        async with Headless(home_app) as h:
            for key, mode in [("f1", "activity"), ("f2", "updated"),
                              ("f3", "created"), ("f4", "category"),
                              ("f5", "name")]:
                await h.press(key)
                assert h.app.state.sort_mode == mode


# ─── preview pane ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPreviewPane:
    async def test_preview_toggle(self, home_app):
        async with Headless(home_app) as h:
            assert h.app.state.preview_visible is True
            before = h.screen_text()
            await h.press("p")
            assert h.app.state.preview_visible is False
            assert h.screen_text() != before
            await h.press("p")
            assert h.app.state.preview_visible is True

    async def test_preview_label_shows_selected_ws(self, home_app):
        async with Headless(home_app) as h:
            name = next(
                w.name for w in h.app.state.store.active
                if w.id == h.app.home.ws_list.highlighted_id
            )
            assert h.app.home._preview_label.count(name) == 1

    async def test_preview_follows_cursor(self, home_app):
        async with Headless(home_app) as h:
            first = h.app.home._preview_ws_id
            await h.press("j")
            assert h.app.home._preview_ws_id == h.app.home.ws_list.highlighted_id
            assert h.app.home._preview_ws_id != first


# ─── search ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSearch:
    async def test_slash_opens_search(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash")
            assert h.app.home.search_active is True

    async def test_slash_defaults_to_session_mode(self, home_app):
        """'/' searches session content across all workstreams, as app.py."""
        async with Headless(home_app) as h:
            await h.press("slash")
            assert h.app.state.search_mode == "sessions"

    async def test_tab_toggles_search_mode(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash", "tab")
            assert h.app.state.search_mode == "ws"
            await h.press("tab")
            assert h.app.state.search_mode == "sessions"

    async def test_search_narrows_rows_live(self, home_app):
        async with Headless(home_app) as h:
            assert len(ws_ids(h.app)) == 6
            await h.press("slash", "tab")  # ws-name filter mode
            await h.feed_bytes(b"personal")
            assert h.app.state.search_text == "personal"
            assert ws_ids(h.app) != []
            assert len(ws_ids(h.app)) < 6
            assert "Personal project" in h.screen_text()

    async def test_escape_cancels_search_and_restores(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash", "tab")
            await h.feed_bytes(b"personal")
            await h.press("escape")
            assert h.app.home.search_active is False
            assert h.app.state.search_text == ""
            assert len(ws_ids(h.app)) == 6

    async def test_enter_keeps_filter(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash", "tab")
            await h.feed_bytes(b"personal")
            await h.press("enter")
            assert h.app.home.search_active is False
            assert h.app.state.search_text == "personal"
            assert len(ws_ids(h.app)) < 6

    async def test_search_swallows_action_keys(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash", "tab")
            await h.feed_bytes(b"q3")  # would otherwise quit + filter
            assert h.app._done.done() is False
            assert h.app.state.filter_mode == "all"
            assert h.app.state.search_text == "q3"


# ─── cross-workstream session content search ─────────────────────────


@pytest.mark.asyncio
class TestGlobalSessionSearch:
    async def test_no_sessions_shows_hint(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"widget")
            await finish_search(h)
            assert "Sessions still loading" in h.screen_text()

    async def test_no_match_shows_hint(self, home_app):
        home_app.state.sessions = [make_session(1, title="Fixing the parser")]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"nonesuchterm")
            await finish_search(h)
            assert "No session matches" in h.screen_text()

    async def test_matching_session_takes_over_the_list(self, home_app):
        s = make_session(1, title="Widget refactor")
        home_app.state.sessions = [s]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"widget")
            await finish_search(h)
            assert ws_ids(h.app) == [f"gs:{s.session_id}"]
            text = h.screen_text()
            assert "Widget refactor" in text
            assert "1 session match" in text
            assert "Active work item" not in text  # ws rows are gone

    async def test_tab_back_to_ws_mode_restores_rows(self, home_app):
        home_app.state.sessions = [make_session(1, title="Widget refactor")]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"personal")
            await finish_search(h)
            await h.press("tab")
            assert h.app.state.search_mode == "ws"
            assert "Personal project" in h.screen_text()
            assert all(
                not str(rid).startswith("gs:") for rid in ws_ids(h.app)
            )

    async def test_escape_clears_results(self, home_app):
        home_app.state.sessions = [make_session(1, title="Widget refactor")]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"widget")
            await finish_search(h)
            await h.press("escape")
            assert h.app.home._gs_results == []
            assert len(ws_ids(h.app)) == 6

    async def test_enter_on_hit_opens_owning_detail(self, home_app):
        s = make_session(1, title="Widget refactor")
        ws = home_app.state.store.active[2]
        ws.add_link("claude-session", s.session_id, "session")
        home_app.state.store.update(ws)
        home_app.state.sessions = [s]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"widget")
            await finish_search(h)
            await h.press("enter")   # closes the input, keeps results
            await h.press("enter")   # opens the hit
            assert isinstance(h.top, DetailView)
            assert h.top.ws.id == ws.id
            assert h.app.state.search_text == ""  # search mode left behind

    async def test_enter_on_orphan_hit_toasts(self, home_app):
        home_app.state.sessions = [make_session(1, title="Widget refactor")]
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"widget")
            await finish_search(h)
            await h.press("enter")
            await h.press("enter")
            assert not isinstance(h.top, DetailView)
            assert "No workstream owns" in h.app.toast_text


# ─── tabs & quit (full tab semantics live in test_detail.py) ─────────


@pytest.mark.asyncio
class TestTabsAndQuit:
    async def test_enter_opens_detail_tab(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("enter")
            assert isinstance(h.top, DetailView)
            assert h.top.ws.id == ws_id
            assert h.app.tabs.active_tab.ws_id == ws_id

    async def test_ctrl_b_switches_to_sessions_tab(self, home_app):
        async with Headless(home_app) as h:
            await h.press("ctrl+b")
            assert isinstance(h.top, CurrentSessionsView)
            assert h.app.tabs.active_idx == 1
            await h.press("ctrl+x")  # and back
            assert h.top is h.app.home
            assert h.app.tabs.active_idx == 0

    async def test_x_on_home_tab_is_noop(self, home_app):
        async with Headless(home_app) as h:
            await h.press("x")
            assert h.top is h.app.home
            assert len(h.app.tabs.tabs) == 2

    async def test_q_quits_cleanly(self, home_app):
        async with Headless(home_app) as h:
            await h.press("q")
            assert h.app._done.done() is True
