"""OrchApp restores the persisted UI state on startup (ui_state.py).

The autouse `isolated_ui_state` fixture (tests/conftest.py) points
ORCH_UI_STATE_PATH at a temp file, so these tests write the "previous run"
state there and then boot an app against it.
"""

import os
from pathlib import Path

import pytest

from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.views.detail import DetailView
from ui_state import SESSIONS_TAB, UiState, load_ui_state, save_ui_state


@pytest.fixture
def ui_file():
    """The temp ui-state.json the app will read on startup."""
    return Path(os.environ["ORCH_UI_STATE_PATH"])


def _app(store):
    return OrchApp(store_path=store.path, pollers=False)


def ws_ids(app):
    return [rid for rid, _, disabled in app.home.ws_list.rows if not disabled]


@pytest.mark.asyncio
class TestRestore:
    async def test_cursor_lands_on_saved_workstream(self, populated_store, ui_file):
        target = populated_store.active[3]
        save_ui_state(UiState(home_ws_id=target.id), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert h.app.home.ws_list.highlighted_id == target.id

    async def test_cursor_falls_back_when_workstream_is_gone(self, populated_store, ui_file):
        save_ui_state(UiState(home_ws_id="deleted-since"), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert h.app.home.ws_list.highlighted_id == ws_ids(h.app)[0]

    async def test_sort_mode_restored_before_first_rows(self, populated_store, ui_file):
        save_ui_state(UiState(sort_mode="name"), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert h.app.state.sort_mode == "name"
            names = [h.app.state.get_ws(i).name for i in ws_ids(h.app)]
            assert names == sorted(names, key=str.lower)

    async def test_filter_mode_restored(self, populated_store, ui_file):
        save_ui_state(UiState(filter_mode="stale"), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert h.app.state.filter_mode == "stale"
            assert len(ws_ids(h.app)) < len(populated_store.active)

    async def test_tabs_reopen_and_active_detail_is_shown(self, populated_store, ui_file):
        a, b = populated_store.active[0], populated_store.active[1]
        save_ui_state(UiState(tab_ws_ids=[a.id, b.id], active_tab_id=a.id), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert [t.ws_id for t in h.app.tabs.tabs if t.ws_id] == [a.id, b.id]
            assert h.app.tabs.active_tab.ws_id == a.id
            assert isinstance(h.app.top, DetailView)
            assert h.app.top.ws.id == a.id

    async def test_home_active_reopens_tabs_without_leaving_home(self, populated_store, ui_file):
        a = populated_store.active[0]
        save_ui_state(UiState(tab_ws_ids=[a.id]), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert [t.ws_id for t in h.app.tabs.tabs if t.ws_id] == [a.id]
            assert h.app.tabs.is_home
            assert h.app.top is h.app.home

    async def test_sessions_tab_restored(self, populated_store, ui_file):
        save_ui_state(UiState(active_tab_id=SESSIONS_TAB), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert h.app.tabs.is_current_sessions

    async def test_deleted_tab_workstreams_are_dropped(self, populated_store, ui_file):
        a = populated_store.active[0]
        save_ui_state(UiState(tab_ws_ids=["gone", a.id], active_tab_id="gone"), ui_file)
        async with Headless(_app(populated_store)) as h:
            assert [t.ws_id for t in h.app.tabs.tabs if t.ws_id] == [a.id]
            assert h.app.tabs.is_home  # active tab vanished → home, not a blank tab

    async def test_no_saved_state_starts_clean(self, populated_store, ui_file):
        assert not ui_file.exists()
        async with Headless(_app(populated_store)) as h:
            assert len(h.app.tabs.tabs) == 2
            assert h.app.tabs.is_home

    async def test_corrupt_state_file_starts_clean(self, populated_store, ui_file):
        ui_file.parent.mkdir(parents=True, exist_ok=True)
        ui_file.write_text("}{ not json")
        async with Headless(_app(populated_store)) as h:
            assert len(h.app.tabs.tabs) == 2
            assert h.app.tabs.is_home


@pytest.mark.asyncio
class TestPersist:
    async def test_exit_writes_open_tab_and_cursor(self, populated_store, ui_file):
        async with Headless(_app(populated_store)) as h:
            await h.press("j")            # move the home cursor
            cursor = h.app.home.ws_list.highlighted_id
            await h.press("enter")        # open that workstream's tab
            assert isinstance(h.app.top, DetailView)
            opened = h.app.top.ws.id
        saved = load_ui_state(ui_file)
        assert saved.home_ws_id == cursor
        assert saved.tab_ws_ids == [opened]
        assert saved.active_tab_id == opened

    async def test_exit_writes_view_options(self, populated_store, ui_file):
        async with Headless(_app(populated_store)) as h:
            await h.press("2")   # filter: stale
            await h.press("f5")  # sort: name
            await h.press("p")   # preview off
            assert h.app.state.filter_mode == "stale"
        saved = load_ui_state(ui_file)
        assert saved.filter_mode == "stale"
        assert saved.sort_mode == "name"
        assert saved.preview_visible is False

    async def test_flush_is_a_no_op_when_nothing_changed(self, populated_store, ui_file):
        async with Headless(_app(populated_store)) as h:
            h.app._flush_ui_state()
            first = ui_file.stat().st_mtime_ns
            h.app._flush_ui_state()
            assert ui_file.stat().st_mtime_ns == first

    async def test_round_trip_reopens_where_it_left_off(self, populated_store, ui_file):
        async with Headless(_app(populated_store)) as h:
            await h.press("j")
            await h.press("enter")
            opened = h.app.top.ws.id
        async with Headless(_app(populated_store)) as h2:
            assert h2.app.tabs.active_tab.ws_id == opened
            assert isinstance(h2.app.top, DetailView)
            assert h2.app.top.ws.id == opened
