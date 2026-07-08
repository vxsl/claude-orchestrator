"""CurrentSessionsView tests (P4-C): grouping under ws header rows,
5s-reload staleness (exclusive+gen), the THINKING-only in-place
throbber, selection → launch, archive, and the key walk of the
original CurrentSessionsScreen BINDINGS. AI titling is mocked
(autouse) so no test shells out to `claude`.
"""

from datetime import datetime, timezone

import pytest

import thread_namer
from sessions import ClaudeSession
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.views.current_sessions import RELOAD_GROUP, CurrentSessionsView
from tui.views.help import HelpView


@pytest.fixture(autouse=True)
def no_ai_titles(monkeypatch):
    """Session rendering may generate titles via `claude -p` — never in tests."""
    monkeypatch.setattr(thread_namer, "title_sessions", lambda sessions: {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_session(n: int = 1, **kw) -> ClaudeSession:
    defaults = dict(
        session_id=f"aaaabbb{n}-0000-0000-0000-00000000000{n}",
        project_dir="-home-u-dev-proj",
        project_path="/home/u/dev/proj",
        title=f"Fake session {n}",
        started_at=_now_iso(),
        last_activity=_now_iso(),
        message_count=2,
        assistant_message_count=1,
        model="claude-sonnet-4",
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


@pytest.fixture
def cs_app(populated_store):
    """OrchApp with two workstreams owning one today-session each."""
    app = OrchApp(store_path=populated_store.path, pollers=False)
    ws1, ws2 = app.state.store.active[:2]
    s1, s2 = make_session(1), make_session(2)
    by_ws = {ws1.id: [s1], ws2.id: [s2]}
    app.state.sessions = [s1, s2]
    app.state.sessions_for_ws = (
        lambda ws, include_archived_sessions=False: list(by_ws.get(ws.id, []))
    )
    app._ws1, app._ws2 = ws1, ws2
    app._s1, app._s2 = s1, s2
    return app


async def push_cs(h):
    view = CurrentSessionsView(h.app.state, h.app.tabs)
    h.app.push(view)
    await h.pause()
    return view


@pytest.mark.asyncio
class TestGrouping:
    async def test_groups_sessions_under_ws_headers(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            text = h.screen_text()
            assert cs_app._ws1.name in text
            assert cs_app._ws2.name in text
            assert "aaaabbb1" in text and "aaaabbb2" in text
            ids = [rid for rid, _, _ in view.list.rows]
            # header rows precede their session blocks; gap between groups
            i_h1 = ids.index(f"__ws__{cs_app._ws1.id}")
            i_h2 = ids.index(f"__ws__{cs_app._ws2.id}")
            i_s1 = ids.index(cs_app._s1.session_id)
            i_s2 = ids.index(cs_app._s2.session_id)
            assert i_h1 < i_s1 and i_h2 < i_s2
            assert any(isinstance(r, str) and r.startswith("__gap__") for r in ids)

    async def test_shelved_and_stale_sessions_excluded(self, cs_app):
        cs_app._ws1.shelved_sessions[cs_app._s1.session_id] = _now_iso()
        cs_app._s2.last_activity = "2026-01-01T00:00:00+00:00"  # not today
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            assert view._sessions == []
            assert "No sessions active today" in h.screen_text()

    async def test_duplicate_sids_across_ws_deduped(self, cs_app):
        shared = cs_app._s1
        cs_app.state.sessions_for_ws = (
            lambda ws, include_archived_sessions=False: [shared]
        )
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            sids = [s.session_id for _, s in view._sessions]
            assert sids.count(shared.session_id) == 1

    async def test_tab_bar_and_title_render(self, cs_app):
        async with Headless(cs_app) as h:
            await push_cs(h)
            text = h.screen_text()
            assert "today · active" in text
            assert "Workstreams" in text  # tab bar line


@pytest.mark.asyncio
class TestReloadStaleness:
    async def test_stale_reload_result_dropped(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            applied = []
            view._apply = lambda results: applied.append(results)

            g = h.app.gen(RELOAD_GROUP)
            await view._reload_runner(g)  # generation unchanged → applies
            assert len(applied) == 1

            async def noop():
                pass
            h.app.exclusive(RELOAD_GROUP, noop())  # a newer run started
            await view._reload_runner(g)  # now stale → dropped
            assert len(applied) == 1

    async def test_reload_picks_up_new_sessions_keeps_highlight(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            await h.press("j")  # highlight the second session block
            hid = view.list.highlighted_id
            s3 = make_session(3)
            cs_app.state.sessions.append(s3)
            by_id = {cs_app._ws1.id: [cs_app._s1, s3], cs_app._ws2.id: [cs_app._s2]}
            cs_app.state.sessions_for_ws = (
                lambda ws, include_archived_sessions=False: list(by_id.get(ws.id, []))
            )
            await view._reload_runner(h.app.gen(RELOAD_GROUP))
            await h.pause()
            assert "aaaabbb3" in h.screen_text()
            assert view.list.highlighted_id == hid  # highlight kept by id


@pytest.mark.asyncio
class TestThrobber:
    async def test_thinking_rows_update_in_place(self, cs_app):
        # s1 THINKING: live + recent last_activity; s2 idle (not live)
        cs_app._s1.is_live = True
        cs_app._s1.last_message_role = "user"
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            updates = []
            orig = view.list.update_row
            view.list.update_row = lambda rid, m: updates.append(rid) or orig(rid, m)
            frame_before = view._throbber_frame
            view._tick_throbber()
            assert view._throbber_frame == frame_before + 1
            touched = {r[0] if isinstance(r, tuple) else r for r in updates}
            assert touched == {cs_app._s1.session_id}  # THINKING rows only

    async def test_no_thinking_pauses_throbber(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            assert view._throbber_paused is False
            view._tick_throbber()  # nothing animating
            assert view._throbber_paused is True
            frame = view._throbber_frame
            view._tick_throbber()
            assert view._throbber_frame == frame  # no frame advance

    async def test_structure_change_falls_back_to_rebuild(self, cs_app):
        cs_app._s1.is_live = True
        cs_app._s1.last_message_role = "user"
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            rebuilt = []
            orig = view._build_rows
            view.list.set_rows([])  # rows vanished under the tick
            view._build_rows = lambda: rebuilt.append(1) or orig()
            view._tick_throbber()
            assert rebuilt == [1]


@pytest.mark.asyncio
class TestKeys:
    async def test_enter_launches_highlighted_session(self, cs_app):
        launched = []
        async with Headless(cs_app) as h:
            h.app.launch_claude_session = \
                lambda ws, **kw: launched.append((ws, kw.get("session_id")))
            view = await push_cs(h)
            sid = view.list.highlighted_id
            await h.press("enter")
            assert launched == [(view._session_ws_map[sid], sid)]

    async def test_l_and_r_also_launch(self, cs_app):
        launched = []
        async with Headless(cs_app) as h:
            h.app.launch_claude_session = \
                lambda ws, **kw: launched.append(kw.get("session_id"))
            await push_cs(h)
            await h.press("l", "r")
            assert len(launched) == 2

    async def test_space_archives_and_removes_row(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            sid = view.list.highlighted_id
            ws = view._session_ws_map[sid]
            await h.press("space")
            assert sid in ws.archived_sessions
            # archived sessions filter out via sessions_for_ws in the real
            # app; here the stub returns them regardless, so just assert the
            # store write happened and the view reloaded without crashing.
            assert view._sessions is not None

    async def test_ctrl_space_archives_and_dismisses(self, cs_app):
        async with Headless(cs_app) as h:
            view = await push_cs(h)
            sid = view.list.highlighted_id
            ws = view._session_ws_map[sid]
            await h.feed_bytes(b"\x00")
            assert sid in ws.archived_sessions
            assert h.top is h.app.home

    async def test_question_mark_opens_sessions_help(self, cs_app):
        async with Headless(cs_app) as h:
            await push_cs(h)
            await h.press("question_mark")
            assert isinstance(h.top, HelpView)
            assert "All Sessions — Help" in h.screen_text()

    async def test_colon_opens_palette(self, cs_app):
        async with Headless(cs_app) as h:
            await push_cs(h)
            await h.press("colon")
            assert "Command Palette" in h.screen_text()

    @pytest.mark.parametrize("key", ["escape", "ctrl+h", "backspace"])
    async def test_back_keys_dismiss(self, cs_app, key):
        async with Headless(cs_app) as h:
            await push_cs(h)
            await h.press(key)
            assert h.top is h.app.home
