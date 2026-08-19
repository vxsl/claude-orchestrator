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


@pytest.mark.asyncio
class TestSessionRestore:
    """The claude session a tab had open comes back with the tab."""

    @staticmethod
    def _stub_resume(app, monkeypatch, alive=True):
        """Record launch_claude_session calls; fake tmux liveness."""
        from term_host import TerminalHost
        monkeypatch.setattr(TerminalHost, "tmux_session_alive",
                            staticmethod(lambda sid: alive))
        calls = []
        monkeypatch.setattr(app, "launch_claude_session",
                            lambda ws, session_id=None: calls.append((ws.id, session_id)))
        return calls

    async def test_saved_session_reattaches_with_its_tab(
        self, populated_store, ui_file, monkeypatch
    ):
        a = populated_store.active[0]
        save_ui_state(UiState(tab_ws_ids=[a.id], active_tab_id=a.id,
                              tab_sessions={a.id: "sid-live"}), ui_file)
        app = _app(populated_store)
        calls = self._stub_resume(app, monkeypatch)
        async with Headless(app) as h:
            await h.pause()
            await h.pause()
            assert calls == [(a.id, "sid-live")]

    async def test_dead_session_is_not_reattached(
        self, populated_store, ui_file, monkeypatch
    ):
        a = populated_store.active[0]
        save_ui_state(UiState(tab_ws_ids=[a.id], active_tab_id=a.id,
                              tab_sessions={a.id: "sid-gone"}), ui_file)
        app = _app(populated_store)
        calls = self._stub_resume(app, monkeypatch, alive=False)
        async with Headless(app) as h:
            await h.pause()
            await h.pause()
            assert calls == []
            assert isinstance(h.app.top, DetailView)  # tab still reopens

    async def test_session_waits_until_its_tab_is_activated(
        self, populated_store, ui_file, monkeypatch
    ):
        """A saved session on a non-active tab resumes on the tab switch, not
        at startup — the same laziness as an in-session tab switch."""
        a, b = populated_store.active[0], populated_store.active[1]
        save_ui_state(UiState(tab_ws_ids=[a.id, b.id], active_tab_id=a.id,
                              tab_sessions={b.id: "sid-b"}), ui_file)
        app = _app(populated_store)
        calls = self._stub_resume(app, monkeypatch)
        async with Headless(app) as h:
            await h.pause()
            assert calls == []
            h.app.tabs.switch_to_id(b.id)
            h.app._apply_tab_switch()
            await h.pause()
            await h.pause()
            assert calls == [(b.id, "sid-b")]

    async def test_session_for_a_deleted_workstream_is_dropped(
        self, populated_store, ui_file, monkeypatch
    ):
        save_ui_state(UiState(tab_ws_ids=["gone"], tab_sessions={"gone": "sid"}), ui_file)
        app = _app(populated_store)
        calls = self._stub_resume(app, monkeypatch)
        async with Headless(app) as h:
            await h.pause()
            assert calls == []
            assert h.app._tab_active_session == {}

    async def test_session_on_screen_at_quit_is_remembered(self, populated_store, ui_file):
        """The session showing at quit time is what gets saved —
        _tab_active_session only holds tabs the user navigated away from, so
        quitting straight out of a session has to read the view stack."""
        from unittest.mock import MagicMock
        from tui.views.claude_session import ClaudeSessionView
        a = populated_store.active[0]
        app = _app(populated_store)
        async with Headless(app) as h:
            h.app.tabs.open_tab(a.id, a.name, "·")
            fake = MagicMock(spec=ClaudeSessionView)
            fake.ws = a
            fake.session_id = "sid-on-screen"
            h.app._stack.append((fake, None))
            assert h.app._open_tab_sessions() == {a.id: "sid-on-screen"}
        assert load_ui_state(ui_file).tab_sessions == {a.id: "sid-on-screen"}

    async def test_detached_session_is_remembered(self, populated_store, ui_file):
        a = populated_store.active[0]
        app = _app(populated_store)
        async with Headless(app) as h:
            h.app.tabs.open_tab(a.id, a.name, "·")
            h.app._tab_active_session[a.id] = "sid-detached"
        saved = load_ui_state(ui_file)
        assert saved.tab_sessions == {a.id: "sid-detached"}
        assert saved.active_tab_id == a.id
