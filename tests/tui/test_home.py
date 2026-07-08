"""HomeView + OrchApp tests on the tui engine (port of test_app.py's
home-screen classes: navigation, filters, preview toggle, tab bar,
search, quit).

OrchApp is constructed against a temp store (store_path=...) with
pollers=False so no discovery/watchers touch real Claude data. Launch
tests (spawn/resume argv) are actions-level and already covered in
test_app_logic.py — not re-ported here.
"""

import pytest

from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.views.home import STUB_DETAIL


@pytest.fixture
def home_app(populated_store):
    """OrchApp on the populated temp store, background workers off."""
    return OrchApp(store_path=populated_store.path, pollers=False)


def ws_ids(app):
    """Enabled (main) row ids in display order."""
    return [rid for rid, _, disabled in app.home.ws_list.rows if not disabled]


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

    async def test_search_narrows_rows_live(self, home_app):
        async with Headless(home_app) as h:
            assert len(ws_ids(h.app)) == 6
            await h.press("slash")
            await h.feed_bytes(b"personal")
            assert h.app.state.search_text == "personal"
            assert ws_ids(h.app) != []
            assert len(ws_ids(h.app)) < 6
            assert "Personal project" in h.screen_text()

    async def test_escape_cancels_search_and_restores(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"personal")
            await h.press("escape")
            assert h.app.home.search_active is False
            assert h.app.state.search_text == ""
            assert len(ws_ids(h.app)) == 6

    async def test_enter_keeps_filter(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"personal")
            await h.press("enter")
            assert h.app.home.search_active is False
            assert h.app.state.search_text == "personal"
            assert len(ws_ids(h.app)) < 6

    async def test_search_swallows_action_keys(self, home_app):
        async with Headless(home_app) as h:
            await h.press("slash")
            await h.feed_bytes(b"q3")  # would otherwise quit + filter
            assert h.app._done.done() is False
            assert h.app.state.filter_mode == "all"
            assert h.app.state.search_text == "q3"


# ─── stubs & quit ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStubsAndQuit:
    async def test_enter_shows_detail_stub_toast(self, home_app):
        async with Headless(home_app) as h:
            await h.press("enter")
            assert h.app.toast_text == STUB_DETAIL
            assert STUB_DETAIL.split(" — ")[0] in h.screen_text()

    async def test_tab_switch_is_stubbed(self, home_app):
        async with Headless(home_app) as h:
            await h.press("ctrl+b")
            assert "P4" in h.app.toast_text
            assert h.app.tabs.active_idx == 0

    async def test_q_quits_cleanly(self, home_app):
        async with Headless(home_app) as h:
            await h.press("q")
            assert h.app._done.done() is True
