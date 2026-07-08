"""DetailView tests (P4-C Detail-lite): rows from a real temp store,
the ported key walk, the back cascade (peek → search → dismiss), the
options-fingerprint rebuild gate, the animating-rows-only throbber,
peek swap/restore, and the '/' + '\\' search modes. AI titling is
mocked (autouse) and the last-seen cache is redirected to tmp_path so
no test touches real user state.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import thread_namer
import threads
from sessions import ClaudeSession
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.views.add_link import AddLinkView
from tui.views.current_sessions import CurrentSessionsView
from tui.views.detail import DetailView
from tui.views.help import HelpView
from tui.views.links import LinksView
from tui.views.quick_note import QuickNoteView
from tui.views.todo import TodoView
from tui.views.trash import TrashView


@pytest.fixture(autouse=True)
def no_ai_titles(monkeypatch, tmp_path):
    monkeypatch.setattr(thread_namer, "title_sessions", lambda sessions: {})
    # never touch the real ~/.cache last-seen file
    monkeypatch.setattr(threads, "LAST_SEEN_FILE", tmp_path / "last-seen.json")


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def make_session(n: int = 1, **kw) -> ClaudeSession:
    defaults = dict(
        session_id=f"aaaabbb{n}-0000-0000-0000-00000000000{n}",
        project_dir="-home-u-dev-proj",
        project_path="/home/u/dev/proj",
        title=f"Fake session {n}",
        started_at=_iso(60 + n),
        last_activity=_iso(n),  # s1 newest → deterministic display order
        message_count=2,
        assistant_message_count=1,
        model="claude-sonnet-4",
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


@pytest.fixture
def detail_app(populated_store):
    """OrchApp whose first workstream has two active + one archived session."""
    app = OrchApp(store_path=populated_store.path, pollers=False)
    ws = app.state.store.active[0]
    s1, s2, s3 = make_session(1), make_session(2), make_session(3)
    ws.archived_sessions[s3.session_id] = _iso(0)
    app.state.store.update(ws)
    sessions = [s1, s2, s3]
    app.state.sessions = sessions
    app.state.sessions_loaded = True
    app.state.sessions_for_ws = (
        lambda w, include_archived_sessions=False:
        list(sessions) if include_archived_sessions else [s1, s2]
    )
    app.state.notifications_for_ws = lambda w: []
    launched = []
    app.launch_claude_session = lambda w, **kw: launched.append((w, kw))
    app.launched = launched
    app._ws, app._s1, app._s2, app._s3 = ws, s1, s2, s3
    return app


def detail_headless(app):
    """Headless at 160×48: the archived pane (1fr ≈ 53 cols) clears the
    session renderer's ~40-col row floor, as on real terminals. At narrower
    widths rows crop on the right (the Textual original wrapped instead)."""
    return Headless(app, size=(160, 48))


async def push_detail(h):
    view = DetailView(h.app.state, h.app.tabs, h.app._ws)
    h.app.push(view)
    await h.pause()
    return view


def _write_jsonl(tmp_path, session, texts):
    path = tmp_path / f"{session.session_id}.jsonl"
    lines = []
    for i, (role, text) in enumerate(texts):
        lines.append(json.dumps({
            "type": role,
            "message": {"content": text},
            "timestamp": f"2026-07-08T10:0{i}:00Z",
        }))
    path.write_text("\n".join(lines) + "\n")
    session.jsonl_path = str(path)
    return path


# ─── rendering ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDetailRenders:
    async def test_opens_with_sessions_archived_and_body(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            text = h.screen_text()
            assert detail_app._ws.name in text
            assert "aaaabbb1" in text and "aaaabbb2" in text  # active blocks
            assert "aaaabbb3" in text  # archived block
            assert "Archived (1)" in text
            assert "Created" in text and "Updated" in text  # body panel
            assert "Workstreams" in text  # tab bar line
            assert "archive/restore" in text  # help bar

    async def test_no_repo_hides_tig_hint(self, detail_app):
        detail_app._ws.repo_path = ""
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            assert "tig (fullscreen)" not in h.screen_text()

    async def test_repo_shows_body_plus_tig_hint(self, detail_app, tmp_path):
        detail_app._ws.repo_path = str(tmp_path)
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            text = h.screen_text()
            assert "Created" in text  # body still shown (no embedded tig)
            assert "tig (fullscreen)" in text

    async def test_nav_moves_between_session_blocks(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            first = view.sessions_list.highlighted_id
            await h.press("j")
            assert view.sessions_list.highlighted_id != first
            await h.press("k")
            assert view.sessions_list.highlighted_id == first


# ─── pane focus ring ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPaneFocus:
    async def test_ctrl_j_cycles_sessions_archived_body(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            assert view._active_pane == "sessions"
            await h.press("ctrl+j")
            assert view._active_pane == "archived"
            await h.press("ctrl+j")
            assert view._active_pane == "body"
            await h.press("ctrl+j")
            assert view._active_pane == "sessions"
            await h.press("ctrl+k")
            assert view._active_pane == "body"

    async def test_archived_skipped_when_empty(self, detail_app):
        detail_app._ws.archived_sessions.clear()
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("ctrl+j")
            assert view._active_pane == "body"

    async def test_focused_pane_draws_border(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            before = h.screen_text().count("╭")
            await h.press("ctrl+j")  # focus moves; border follows
            assert h.screen_text().count("╭") == before  # still exactly one ring
            assert "╭" in h.screen_text()


# ─── select / resume / spawn ─────────────────────────────────────────


@pytest.mark.asyncio
class TestLaunchKeys:
    async def test_enter_launches_highlighted(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            sid = view.sessions_list.highlighted_id
            await h.press("enter")
            assert len(detail_app.launched) == 1
            ws, kw = detail_app.launched[0]
            assert kw["session_id"] == sid
            assert kw["cwd"] == "/home/u/dev/proj"

    async def test_l_ctrl_l_and_r_also_launch(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("l", "ctrl+l", "r")
            assert len(detail_app.launched) == 3

    async def test_c_spawns_new_session(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("c")
            ws, kw = detail_app.launched[0]
            assert ws is detail_app._ws
            assert "session_id" not in kw

    async def test_enter_in_archived_pane_launches_archived(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("ctrl+j", "enter")
            ws, kw = detail_app.launched[0]
            assert kw["session_id"] == detail_app._s3.session_id

    async def test_y_yanks_resume_cmd(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            sid = view.sessions_list.highlighted_id
            await h.press("y")
            assert h.io.clipboard == [f"claude --resume {sid}"]
            assert f"claude --resume {sid[:8]}" in h.screen_text()


# ─── session state mutations ─────────────────────────────────────────


@pytest.mark.asyncio
class TestSessionMutations:
    async def test_space_archives_then_restores(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            sid = view.sessions_list.highlighted_id
            await h.press("space")
            assert sid in detail_app._ws.archived_sessions
            # restore from the archived pane
            await h.press("ctrl+j")
            view.archived_list.highlighted = next(
                i for i, r in enumerate(view.archived_list.rows)
                if r[0] == sid)
            await h.press("space")
            assert sid not in detail_app._ws.archived_sessions

    async def test_A_archives_all_visible(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("A")
            arch = detail_app._ws.archived_sessions
            assert detail_app._s1.session_id in arch
            assert detail_app._s2.session_id in arch
            assert "Archived 2 sessions" in detail_app.toast_text

    async def test_z_shelves_and_unshelves(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            sid = view.sessions_list.highlighted_id
            await h.press("z")
            assert sid in detail_app._ws.shelved_sessions
            assert detail_app.toast_text == "Shelved"
            assert "shelved" in h.screen_text()  # shelved separator renders
            # shelved block keeps its id — re-select it and unshelve
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == sid)
            await h.press("z")
            assert sid not in detail_app._ws.shelved_sessions
            assert detail_app.toast_text == "Unshelved"

    async def test_X_trashes_and_T_opens_trash(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            sid = view.sessions_list.highlighted_id
            await h.press("X")
            assert sid in detail_app._ws.deleted_sessions
            assert "Moved to trash" in detail_app.toast_text
            await h.press("T")
            assert isinstance(h.top, TrashView)

    async def test_u_archives_workstream_and_dismisses(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("u")
            assert detail_app._ws.archived is True
            assert h.top is detail_app.home


# ─── notifications (d / D) ───────────────────────────────────────────


def _notif(sid):
    return SimpleNamespace(
        id=f"notif-{sid[:8]}", session_id=sid, cwd=None, dismissed=False,
        dt=datetime.now(timezone.utc), freshness="fresh",
        message="Claude needs your input", timestamp=_iso(0),
    )


@pytest.mark.asyncio
class TestNotifications:
    async def test_d_dismisses_highlighted_notification(self, detail_app, monkeypatch):
        dismissed = []
        monkeypatch.setattr("tui.views.detail.dismiss_notification", dismissed.append)
        n = _notif(detail_app._s1.session_id)
        detail_app.state.notifications_for_ws = lambda w: [n]
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            assert "Claude needs your input" in h.screen_text()
            assert "(1 new)" in h.screen_text()
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == detail_app._s1.session_id)
            detail_app.state.notifications_for_ws = lambda w: []  # post-dismiss
            await h.press("d")
            assert dismissed == [n.id]
            assert view._session_notifications == {}

    async def test_D_dismisses_all(self, detail_app, monkeypatch):
        monkeypatch.setattr("tui.views.detail.dismiss_all_for_dirs",
                            lambda notifs, dirs: None)
        n1, n2 = _notif(detail_app._s1.session_id), _notif(detail_app._s2.session_id)
        detail_app.state.notifications_for_ws = lambda w: [n1, n2]
        detail_app.state._ws_dirs = lambda w: {"/home/u/dev/proj"}
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            assert len(view._session_notifications) == 2
            detail_app.state.notifications_for_ws = lambda w: []
            await h.press("D")
            assert n1.dismissed and n2.dismissed
            assert view._session_notifications == {}


# ─── workstream sub-screens ──────────────────────────────────────────


@pytest.mark.asyncio
class TestSubScreens:
    async def test_n_quick_note_adds_todo(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("n")
            assert isinstance(h.top, QuickNoteView)
            await h.feed_bytes(b"remember this")
            await h.press("ctrl+s")
            ws = detail_app.state.store.get(detail_app._ws.id)
            assert [t.text for t in ws.todos] == ["remember this"]
            assert detail_app.toast_text == "Todo added"

    async def test_e_opens_todos(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("e")
            assert isinstance(h.top, TodoView)
            await h.press("escape")
            assert isinstance(h.top, DetailView)

    async def test_W_opens_add_link(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("W")
            assert isinstance(h.top, AddLinkView)
            await h.press("escape")
            assert isinstance(h.top, DetailView)

    async def test_o_without_links_toasts(self, detail_app):
        detail_app._ws.links = []
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("o")
            assert detail_app.toast_text == "No links to open"

    async def test_o_with_one_link_opens_links_view(self, detail_app):
        detail_app._ws.add_link("url", "https://one.test", "one")
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("o")  # detail always lists (unlike home's direct-open)
            assert isinstance(h.top, LinksView)

    async def test_question_mark_opens_detail_help(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("question_mark")
            assert isinstance(h.top, HelpView)
            assert "Workstream Detail — Help" in h.screen_text()

    async def test_f_without_fzedit_toasts(self, detail_app, monkeypatch, tmp_path):
        detail_app._ws.repo_path = str(tmp_path)
        monkeypatch.setattr("tui.views.detail.shutil.which", lambda cmd: None)
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("f")
            assert "fzedit not found" in detail_app.toast_text

    async def test_t_runs_tig_fullscreen_over_suspend(self, detail_app, monkeypatch, tmp_path):
        detail_app._ws.repo_path = str(tmp_path)
        ran = []

        def fake_run(cmd, **kw):
            if cmd[0] != "git":  # the rev-parse gate passes
                ran.append((cmd, kw.get("cwd")))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("tui.views.detail.shutil.which", lambda cmd: "/usr/bin/tig")
        monkeypatch.setattr("tui.views.detail.subprocess.run", fake_run)
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            view._cwd = str(tmp_path)  # skip the ws_working_dir git shell-out
            await h.press("t")
            assert ran == [(["tig"], str(tmp_path))]
            assert "exit_alt_cooked" in h.io.calls  # ran inside suspend()

    async def test_t_without_repo_toasts(self, detail_app, monkeypatch):
        detail_app._ws.repo_path = ""
        detail_app._ws.links = []
        monkeypatch.setattr("actions.ws_directories", lambda ws: [])
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("t")
            assert "No git repo" in detail_app.toast_text


# ─── archived load-more ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestArchivedLoadMore:
    async def test_enter_on_load_more_extends_window(self, detail_app):
        many = [make_session(i + 10) for i in range(35)]
        for s in many:
            detail_app._ws.archived_sessions[s.session_id] = _iso(0)
        all_sessions = [detail_app._s1, detail_app._s2, detail_app._s3] + many
        detail_app.state.sessions_for_ws = (
            lambda w, include_archived_sessions=False:
            list(all_sessions) if include_archived_sessions
            else [detail_app._s1, detail_app._s2]
        )
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            ids = [r[0] for r in view.archived_list.rows]
            assert "__load_more_archived__" in ids
            n_blocks = len({r[0] for r in view.archived_list.rows
                            if isinstance(r[0], str) and not r[0].startswith("__")})
            assert n_blocks == 30
            await h.press("ctrl+j", "G")  # focus archived, jump to load-more
            assert view.archived_list.highlighted_id == "__load_more_archived__"
            assert "more archived sessions" in h.screen_text()
            await h.press("enter")
            n_blocks = len({r[0] for r in view.archived_list.rows
                            if isinstance(r[0], str) and not r[0].startswith("__")})
            assert n_blocks == 36
            assert detail_app.launched == []  # load-more never launches


# ─── fingerprint gate ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFingerprintGate:
    async def test_same_data_never_rebuilds(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            calls = []
            orig = view.sessions_list.set_rows
            view.sessions_list.set_rows = \
                lambda rows, **kw: calls.append(1) or orig(rows, **kw)
            view._refresh()
            view._refresh()
            assert calls == []  # fingerprint unchanged → no set_rows

    async def test_changed_tail_rebuilds(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            calls = []
            orig = view.sessions_list.set_rows
            view.sessions_list.set_rows = \
                lambda rows, **kw: calls.append(1) or orig(rows, **kw)
            detail_app._s1.message_count += 1  # tail moved
            view._refresh()
            assert calls == [1]

    async def test_liveness_result_gated_by_generation(self, detail_app, monkeypatch):
        monkeypatch.setattr("actions.refresh_liveness", lambda sessions: None)
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            applied = []
            view._apply_liveness_result = lambda fp: applied.append(fp)
            g = h.app.gen(view._liveness_group)
            await view._liveness_runner(g)
            assert len(applied) == 1

            async def noop():
                pass
            h.app.exclusive(view._liveness_group, noop())
            await view._liveness_runner(g)  # stale generation → dropped
            assert len(applied) == 1


# ─── throbber ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestThrobber:
    def _make_thinking(self, s):
        s.is_live = True
        s.last_message_role = "user"

    async def test_updates_only_animating_rows(self, detail_app):
        self._make_thinking(detail_app._s1)
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            updates = []
            orig = view.sessions_list.update_row
            view.sessions_list.update_row = \
                lambda rid, m: updates.append(rid) or orig(rid, m)
            frame = view._throbber_frame
            view._tick_throbber()
            assert view._throbber_frame == frame + 1
            touched = {r[0] if isinstance(r, tuple) else r for r in updates}
            assert touched == {detail_app._s1.session_id}

    async def test_idle_sessions_never_tick(self, detail_app):
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            frame = view._throbber_frame
            view._tick_throbber()  # nothing animating
            assert view._throbber_frame == frame

    async def test_paused_during_peek_and_search(self, detail_app, tmp_path, monkeypatch):
        self._make_thinking(detail_app._s1)
        _write_jsonl(tmp_path, detail_app._s1, [("user", "hi"), ("assistant", "hello")])
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == detail_app._s1.session_id)
            await h.press("p")  # peek open
            rows_before = list(view.sessions_list.rows)
            view._tick_throbber()
            assert view.sessions_list.rows == rows_before  # untouched


# ─── peek ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPeek:
    async def test_p_swaps_to_conversation_and_restores(self, detail_app, tmp_path):
        _write_jsonl(tmp_path, detail_app._s1,
                     [("user", "how do I fix the flux capacitor"),
                      ("assistant", "route more gigawatts through it")])
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == detail_app._s1.session_id)
            before_ids = {r[0] for r in view.sessions_list.rows}
            await h.press("p")
            assert view._peek_mode is True
            text = h.screen_text()
            assert "flux capacitor" in text
            assert "gigawatts" in text
            assert "Conversation" in text and "(2 messages)" in text
            await h.press("p")  # toggle restores
            assert view._peek_mode is False
            assert {r[0] for r in view.sessions_list.rows} == before_ids

    async def test_peek_without_content_toasts(self, detail_app):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press("p")
            assert detail_app.toast_text == "No conversation content to peek"

    async def test_enter_ignored_while_peeking(self, detail_app, tmp_path):
        _write_jsonl(tmp_path, detail_app._s1, [("user", "q"), ("assistant", "a")])
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == detail_app._s1.session_id)
            await h.press("p", "enter", "r")
            assert detail_app.launched == []


# ─── search ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSearch:
    async def test_title_search_filters_rows(self, detail_app, monkeypatch):
        titles = {detail_app._s1.session_id: "Fix login page",
                  detail_app._s2.session_id: "Water the plants"}
        monkeypatch.setattr(thread_namer, "get_session_title",
                            lambda s: titles.get(s.session_id, ""))
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("backslash")
            assert view._search_active and view._title_only_search
            await h.feed_bytes(b"login")
            block_ids = {r[0] for r in view.sessions_list.rows
                         if isinstance(r[0], str)}
            assert block_ids == {detail_app._s1.session_id}
            assert view._title_highlights[detail_app._s1.session_id]

    async def test_content_search_narrows_to_matching_session(
            self, detail_app, tmp_path):
        _write_jsonl(tmp_path, detail_app._s1,
                     [("user", "the flux capacitor is broken")])
        _write_jsonl(tmp_path, detail_app._s2, [("user", "water the garden")])
        _write_jsonl(tmp_path, detail_app._s3, [("user", "irrelevant")])
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("slash")
            await h.pause(0.05)  # cache-warm worker finishes
            assert view._content_ready is True
            await h.feed_bytes(b"capacitor")
            assert view._content_search_active is True
            assert [s.session_id for s in view._detail_sessions] == \
                [detail_app._s1.session_id]
            assert "1 hit" in h.screen_text()
            assert "capacitor" in h.screen_text()

    async def test_title_fallback_while_cache_cold(self, detail_app, monkeypatch):
        detail_app._s1.last_message_text = "still fixing the login page"
        monkeypatch.setattr(OrchApp, "exclusive",
                            lambda self, group, coro: coro.close())  # never warm
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("slash")
            await h.feed_bytes(b"login")
            assert view._content_ready is False
            block_ids = {r[0] for r in view.sessions_list.rows
                         if isinstance(r[0], str)}
            assert block_ids == {detail_app._s1.session_id}

    async def test_empty_query_restores_full_lists(self, detail_app, monkeypatch):
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "distinct")
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("backslash")
            await h.feed_bytes(b"distinct")
            await h.press("backspace", "backspace", "backspace", "backspace",
                          "backspace", "backspace", "backspace", "backspace")
            assert len(view._detail_sessions) == 2
            assert len(view._archived_sessions) == 1

    async def test_enter_focuses_results_then_navigates(self, detail_app, monkeypatch):
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "same title")
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("backslash")
            await h.feed_bytes(b"same")
            await h.press("enter")
            assert view._search_focus is False
            first = view.sessions_list.highlighted_id
            await h.press("j")
            assert view.sessions_list.highlighted_id != first  # j navigates now


# ─── back cascade ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBackCascade:
    @pytest.mark.parametrize("key", ["escape", "ctrl+h", "backspace", "h"])
    async def test_plain_back_dismisses(self, detail_app, key):
        async with detail_headless(detail_app) as h:
            await push_detail(h)
            await h.press(key)
            assert h.top is detail_app.home

    async def test_search_cancelled_before_dismiss(self, detail_app, monkeypatch):
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "title")
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("backslash")
            await h.feed_bytes(b"title")
            await h.press("escape")  # 1st: cancels search, stays on detail
            assert isinstance(h.top, DetailView)
            assert view._search_active is False
            assert len(view._detail_sessions) == 2  # lists restored
            await h.press("escape")  # 2nd: dismisses
            assert h.top is detail_app.home

    async def test_peek_closed_before_search_before_dismiss(
            self, detail_app, tmp_path, monkeypatch):
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "title")
        _write_jsonl(tmp_path, detail_app._s1, [("user", "q"), ("assistant", "a")])
        _write_jsonl(tmp_path, detail_app._s2, [("user", "x")])
        _write_jsonl(tmp_path, detail_app._s3, [("user", "y")])
        async with detail_headless(detail_app) as h:
            view = await push_detail(h)
            await h.press("backslash")
            await h.feed_bytes(b"title")
            await h.press("enter")  # focus results
            view.sessions_list.highlighted = next(
                i for i, r in enumerate(view.sessions_list.rows)
                if r[0] == detail_app._s1.session_id)
            await h.press("p")
            assert view._peek_mode is True
            await h.press("ctrl+h")  # 1st: close peek
            assert view._peek_mode is False
            assert isinstance(h.top, DetailView)
            await h.press("ctrl+h")  # 2nd: cancel search
            assert view._search_active is False
            assert isinstance(h.top, DetailView)
            await h.press("ctrl+h")  # 3rd: dismiss
            assert h.top is detail_app.home


# ─── tabs (OrchApp machinery) ────────────────────────────────────────


@pytest.fixture
def tabs_app(populated_store):
    """OrchApp with per-ws fake sessions for the first two workstreams."""
    app = OrchApp(store_path=populated_store.path, pollers=False)
    ws1, ws2 = app.state.store.active[:2]
    s1, s2 = make_session(1), make_session(2)
    s3, s4 = make_session(3), make_session(4)
    by_ws = {ws1.id: [s1, s2], ws2.id: [s3, s4]}
    app.state.sessions = [s1, s2, s3, s4]
    app.state.sessions_loaded = True
    app.state.sessions_for_ws = (
        lambda w, include_archived_sessions=False: list(by_ws.get(w.id, []))
    )
    app.state.notifications_for_ws = lambda w: []
    app._ws1, app._ws2 = ws1, ws2
    return app


@pytest.mark.asyncio
class TestTabs:
    async def test_enter_opens_detail_esc_returns_but_tab_stays(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("enter")
            assert isinstance(h.top, DetailView)
            assert h.app.tabs.active_tab.ws_id == ws_id
            ws_name = h.app.state.get_ws(ws_id).name
            assert ws_name[:20] in h.screen_text()  # tab bar label
            await h.press("escape")
            assert h.top is h.app.home
            assert h.app.tabs.active_idx == 0  # back on the home tab
            assert any(t.ws_id == ws_id for t in h.app.tabs.tabs)  # tab open

    async def test_cycle_preserves_each_details_highlight(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            home = h.app.home
            # open detail for ws at row 0, move its highlight down one block
            await h.press("enter", "j")
            d1 = h.top
            d1_hl = d1.sessions_list.highlighted_id
            await h.press("escape")
            # open detail for the second workstream, leave highlight at top
            await h.press("j", "enter")
            d2 = h.top
            assert d2 is not d1
            d2_hl = d2.sessions_list.highlighted_id
            await h.press("escape")
            assert h.top is home

            # cycle: home → sessions → d1 → d2 → home
            await h.press("ctrl+b")
            assert isinstance(h.top, CurrentSessionsView)
            await h.press("ctrl+b")
            assert h.top is d1  # same cached instance
            assert d1.sessions_list.highlighted_id == d1_hl
            await h.press("ctrl+b")
            assert h.top is d2
            assert d2.sessions_list.highlighted_id == d2_hl
            await h.press("ctrl+b")
            assert h.top is home
            # and backwards
            await h.press("ctrl+x")
            assert h.top is d2

    async def test_close_tab_evicts_cache_and_falls_back(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            await h.press("enter", "escape")        # open d1, back home
            await h.press("j", "enter")             # open d2 (active)
            d2 = h.top
            ws2_id = d2.ws.id
            assert ws2_id in h.app._detail_cache
            await h.press("x")                      # close d2's tab
            assert ws2_id not in h.app._detail_cache
            assert isinstance(h.top, DetailView)    # previous tab: d1
            assert h.top.ws.id != ws2_id
            await h.press("x")                      # close d1's tab too
            assert isinstance(h.top, CurrentSessionsView)  # permanent tab 1
            await h.press("x")                      # permanent: no-op
            assert isinstance(h.top, CurrentSessionsView)

    async def test_reopening_closed_tab_builds_fresh_view(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            await h.press("enter")
            first = h.top
            await h.press("x")
            # closing the only ws tab falls back to the previous permanent
            # tab — Sessions (TabManager.close_tab: active = index - 1)
            assert isinstance(h.top, CurrentSessionsView)
            await h.press("ctrl+x")  # back to the home tab
            assert h.top is h.app.home
            await h.press("enter")
            assert isinstance(h.top, DetailView)
            assert h.top is not first  # cache was evicted

    async def test_open_detail_reuses_cached_view(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            await h.press("enter")
            first = h.top
            await h.press("escape", "enter")
            assert h.top is first

    async def test_tab_bar_lists_open_tabs_everywhere(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            await h.press("enter", "escape")
            name = next(t.label for t in h.app.tabs.tabs if t.ws_id)
            assert name[:20] in h.screen_text()  # home renders the open tab
            await h.press("ctrl+b")  # sessions tab renders it too
            assert isinstance(h.top, CurrentSessionsView)
            assert name[:20] in h.screen_text()


# ─── command palette (detail-scoped commands) ────────────────────────


@pytest.mark.asyncio
class TestDetailPalette:
    async def _open_detail_palette(self, h):
        await h.press("enter")
        assert isinstance(h.top, DetailView)
        detail = h.top
        await h.press("colon")
        return detail

    async def test_spawn_delegates_to_detail_ws(self, tabs_app):
        launched = []
        async with detail_headless(tabs_app) as h:
            h.app.launch_claude_session = lambda w, **kw: launched.append(w)
            detail = await self._open_detail_palette(h)
            await h.feed_bytes(b"spawn")
            await h.press("enter")
            assert launched == [detail.ws]

    async def test_resume_delegates_to_detail_highlight(self, tabs_app):
        launched = []
        async with detail_headless(tabs_app) as h:
            h.app.launch_claude_session = \
                lambda w, **kw: launched.append(kw.get("session_id"))
            detail = await self._open_detail_palette(h)
            sid = detail.sessions_list.highlighted_id
            await h.feed_bytes(b"resume")
            await h.press("enter")
            assert launched == [sid]

    async def test_rename_notifies_detail_hint(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            await self._open_detail_palette(h)
            await h.feed_bytes(b"rename")
            await h.press("enter")
            assert "Use 'E' to rename" in tabs_app.toast_text

    async def test_close_command_closes_tab(self, tabs_app):
        async with detail_headless(tabs_app) as h:
            detail = await self._open_detail_palette(h)
            await h.feed_bytes(b"close")
            await h.press("enter")
            assert not isinstance(h.top, DetailView)  # tab closed
            assert detail.ws.id not in h.app._detail_cache
