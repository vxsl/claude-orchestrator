"""Wave-B list screen tests (P4-B): Todo, Links, SessionPicker, Help,
AutoModeStart, Trash, and the command palette — key walks of the
Textual originals' BINDINGS plus the home wiring ('e', 'o', 'r'
multi-match, '?', ':') mutating a temp store through OrchApp. AI
titling is mocked (autouse) so no test shells out to `claude`.
"""

import asyncio

import pytest

import thread_namer
from models import TodoItem
from sessions import ClaudeSession
from tui.app import App
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.view import View
from tui.views.auto_mode_start import AutoModeStartView
from tui.views.confirm import ConfirmView
from tui.views.help import HelpView
from tui.views.links import LinksView
from tui.views.modals import FuzzyModalView
from tui.views.quick_note import QuickNoteView
from tui.views.session_picker import LIVENESS_GROUP, SessionPickerView
from tui.views.todo import TodoContextView, TodoView
from tui.views.todo_edit import TodoEditView
from tui.views.trash import TrashView


@pytest.fixture(autouse=True)
def no_ai_titles(monkeypatch):
    """SessionPicker generates missing titles via `claude -p` — never in tests."""
    monkeypatch.setattr(thread_namer, "title_sessions", lambda sessions: {})


class Backdrop(View):
    def render(self, frame, rect) -> None:
        frame.write_markup(0, 0, rect.w, "BACKDROP")


async def push_modal(h, view):
    results = []
    h.app.push(view, on_result=results.append)
    await h.pause()
    return results


@pytest.fixture
def app():
    a = App()
    a.toasts = []
    a.notify = lambda msg, **kw: a.toasts.append(str(msg))
    a.push(Backdrop())
    return a


@pytest.fixture
def home_app(populated_store):
    return OrchApp(store_path=populated_store.path, pollers=False)


def make_session(n: int = 1, **kw) -> ClaudeSession:
    defaults = dict(
        session_id=f"aaaabbb{n}-0000-0000-0000-00000000000{n}",
        project_dir="-home-u-dev-proj",
        project_path="/home/u/dev/proj",
        title=f"Fake session {n}",
        started_at="2026-07-08T10:00:00+00:00",
        last_activity="2026-07-08T11:00:00+00:00",
        message_count=2,
        assistant_message_count=1,
        model="claude-sonnet-4",
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


# ─── TodoView (TodoScreen key walk on a temp store) ──────────────────


@pytest.fixture
def todo_app(home_app):
    """home_app whose first workstream has two todos (oldest first)."""
    ws_id = home_app.state.store.active[0].id
    t1 = TodoItem(text="first todo", created_at="2026-07-01T10:00:00")
    t2 = TodoItem(text="second todo", context="ctx notes",
                  created_at="2026-07-02T10:00:00")
    ws = home_app.state.store.active[0]
    ws.todos = [t1, t2]
    home_app.state.store.update(ws)
    home_app._ws_id = ws_id
    return home_app


@pytest.mark.asyncio
class TestTodoView:
    async def test_e_opens_todo_view_with_stats_and_items(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            assert isinstance(h.top, TodoView)
            text = h.screen_text()
            assert "◆ Todos" in text
            assert "2 pending" in text
            assert "first todo" in text and "second todo" in text
            assert "ctx notes" in text  # context preview of the highlighted

    async def test_a_adds_todo_via_quick_note(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e", "a")
            assert isinstance(h.top, QuickNoteView)
            await h.feed_bytes(b"third todo")
            await h.press("ctrl+s")
            assert isinstance(h.top, TodoView)
            ws = h.app.state.get_ws(h.app._ws_id)
            assert [t.text for t in ws.todos][-1] == "third todo"
            assert h.app.toast_text == "Todo added"

    async def test_space_toggles_done(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            view = h.top
            item = view._highlighted_item()
            assert item.done is False
            await h.press("space")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert next(t for t in ws.todos if t.id == item.id).done is True
            assert "1 pending" in h.screen_text()
            assert "1 done" in h.screen_text()

    async def test_e_edits_text(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            item = h.top._highlighted_item()
            orig_text = item.text
            await h.press("e")
            assert isinstance(h.top, TodoEditView)
            assert h.top.input.text == orig_text
            await h.press("end")
            await h.feed_bytes(b" (edited)")
            await h.press("enter")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert next(t for t in ws.todos if t.id == item.id).text \
                == f"{orig_text} (edited)"

    async def test_E_edits_context_save_on_close(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            item = h.top._highlighted_item()
            await h.press("E")
            assert isinstance(h.top, TodoContextView)
            await h.feed_bytes(b"new context")
            await h.press("escape")  # saves, as the original
            assert isinstance(h.top, TodoView)
            ws = h.app.state.get_ws(h.app._ws_id)
            saved = next(t for t in ws.todos if t.id == item.id).context
            assert "new context" in saved

    async def test_d_deletes(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            item = h.top._highlighted_item()
            await h.press("d")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert item.id not in [t.id for t in ws.todos]
            assert h.app.toast_text == "Todo deleted"

    async def test_J_reorders_in_store(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e")
            item = h.top._highlighted_item()
            before = [t.id for t in h.app.state.get_ws(h.app._ws_id).todos]
            assert before.index(item.id) == 1  # newest-first display order
            await h.press("K")  # move up within ws.todos' active list
            after = [t.id for t in h.app.state.get_ws(h.app._ws_id).todos]
            assert after.index(item.id) == 0
            assert after != before

    async def test_enter_spawns_with_context_and_marks_done(self, todo_app):
        launched = []
        async with Headless(todo_app) as h:
            h.app.launch_claude_session = \
                lambda ws, **kw: launched.append((ws, kw))
            await h.press("e")
            item = h.top._highlighted_item()  # "second todo" w/ context
            await h.press("enter")
            assert len(launched) == 1
            ws, kw = launched[0]
            assert kw["prompt"] == "second todo\n\nctx notes"
            assert kw["reuse_pending"] is False
            stored = h.app.state.get_ws(h.app._ws_id).todos
            assert next(t for t in stored if t.id == item.id).done is True

    async def test_l_never_spawns(self, todo_app):
        launched = []
        async with Headless(todo_app) as h:
            h.app.launch_claude_session = \
                lambda ws, **kw: launched.append(ws)
            await h.press("e", "l")
            assert launched == []
            assert isinstance(h.top, TodoView)

    @pytest.mark.parametrize("key", ["escape", "q", "ctrl+h"])
    async def test_back_keys_dismiss(self, todo_app, key):
        async with Headless(todo_app) as h:
            await h.press("e")
            await h.press(key)
            assert h.top is h.app.home

    async def test_question_mark_opens_todo_help(self, todo_app):
        async with Headless(todo_app) as h:
            await h.press("e", "question_mark")
            assert isinstance(h.top, HelpView)
            assert "Todo List — Help" in h.screen_text()

    async def test_empty_state_renders(self, home_app):
        async with Headless(home_app) as h:
            await h.press("e")
            assert "No todos" in h.screen_text()


# ─── LinksView (LinksScreen key walk + home 'o' wiring) ──────────────


@pytest.mark.asyncio
class TestLinksView:
    async def test_o_without_links_toasts(self, home_app):
        async with Headless(home_app) as h:
            await h.press("o")
            assert h.top is h.app.home
            assert h.app.toast_text == "No links"

    async def test_o_single_link_opens_directly(self, home_app, monkeypatch):
        opened = []
        monkeypatch.setattr("tui.views.home.open_link",
                            lambda link, **kw: opened.append(link))
        async with Headless(home_app) as h:
            ws = h.app.state.get_ws(h.app.home.ws_list.highlighted_id)
            ws.add_link("url", "https://one.test", "one")
            await h.press("o")
            assert h.top is h.app.home  # no modal
            assert [l.value for l in opened] == ["https://one.test"]
            assert h.app.toast_text.startswith("Opening")

    async def test_o_multiple_links_lists_and_enter_opens(self, home_app, monkeypatch):
        opened = []
        monkeypatch.setattr("tui.views.links.open_link",
                            lambda link, **kw: opened.append(link))
        async with Headless(home_app) as h:
            ws = h.app.state.get_ws(h.app.home.ws_list.highlighted_id)
            ws.add_link("url", "https://one.test", "one")
            ws.add_link("ticket", "UB-9", "UB-9")
            await h.press("o")
            assert isinstance(h.top, LinksView)
            text = h.screen_text()
            assert "https://one.test" in text and "UB-9" in text
            await h.press("j", "enter")
            assert [l.value for l in opened] == ["UB-9"]
            assert isinstance(h.top, LinksView)  # stays open, as the original
            await h.press("escape")
            assert h.top is h.app.home


# ─── SessionPickerView (SessionPickerScreen + resume wiring) ─────────


@pytest.mark.asyncio
class TestSessionPickerView:
    def two_sessions(self):
        return [make_session(1), make_session(2)]

    async def test_enter_dismisses_highlighted_session(self, app, populated_store):
        ws = populated_store.active[0]
        sessions = self.two_sessions()
        async with Headless(app) as h:
            view = SessionPickerView(ws, sessions)
            results = await push_modal(h, view)
            assert f"Resume: {ws.name}" in h.screen_text()
            await h.press("j", "enter")  # j = next session block
            assert results == [sessions[1]]

    async def test_escape_cancels_none(self, app, populated_store):
        async with Headless(app) as h:
            results = await push_modal(
                h, SessionPickerView(populated_store.active[0],
                                     self.two_sessions()))
            await h.press("escape")
            assert results == [None]

    async def test_l_never_selects(self, app, populated_store):
        async with Headless(app) as h:
            results = await push_modal(
                h, SessionPickerView(populated_store.active[0],
                                     self.two_sessions()))
            await h.press("l")
            assert results == []

    async def test_stale_liveness_result_dropped(self, app, populated_store, monkeypatch):
        import actions
        monkeypatch.setattr(actions, "refresh_liveness", lambda sessions: None)
        # keep the titles worker from also calling the recorded rebuild
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "titled")
        async with Headless(app) as h:
            view = SessionPickerView(populated_store.active[0],
                                     self.two_sessions())
            await push_modal(h, view)
            rebuilds = []
            view._rebuild_rows = lambda: rebuilds.append(1)

            g = h.app.gen(LIVENESS_GROUP)
            await view._liveness_runner(g)  # generation unchanged → applies
            assert rebuilds == [1]

            async def noop():
                pass
            h.app.exclusive(LIVENESS_GROUP, noop())  # a newer run started
            await view._liveness_runner(g)  # now stale → dropped
            assert rebuilds == [1]

    async def test_missing_titles_generated_in_thread(self, app, populated_store, monkeypatch):
        titled = []
        monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "")
        monkeypatch.setattr(thread_namer, "title_sessions",
                            lambda sessions: titled.extend(sessions))
        sessions = self.two_sessions()
        async with Headless(app) as h:
            await push_modal(h, SessionPickerView(populated_store.active[0],
                                                  sessions))
            await h.pause(0.05)  # let the to_thread worker finish
            assert titled == sessions

    async def test_r_multi_match_opens_picker_and_resumes_pick(self, home_app, monkeypatch):
        import actions
        sessions = self.two_sessions()
        resumed = []
        monkeypatch.setattr(actions, "resume_session_now",
                            lambda ws, s, dirs, app: resumed.append(s))
        async with Headless(home_app) as h:
            h.app.state.sessions_for_ws = lambda ws: sessions
            await h.press("r")
            assert isinstance(h.top, SessionPickerView)
            await h.press("j", "enter")
            assert h.top is h.app.home
            assert resumed == [sessions[1]]

    async def test_r_single_match_resumes_directly(self, home_app, monkeypatch):
        import actions
        sessions = [make_session(1)]
        resumed = []
        monkeypatch.setattr(actions, "resume_session_now",
                            lambda ws, s, dirs, app: resumed.append(s))
        async with Headless(home_app) as h:
            h.app.state.sessions_for_ws = lambda ws: sessions
            await h.press("r")
            assert h.top is h.app.home  # no picker
            assert resumed == sessions


# ─── HelpView (HelpScreen: filter + contexts) ────────────────────────


@pytest.mark.asyncio
class TestHelpView:
    async def test_question_mark_opens_home_help(self, home_app):
        async with Headless(home_app) as h:
            await h.press("question_mark")
            assert isinstance(h.top, HelpView)
            text = h.screen_text()
            assert "Workstreams — Help" in text
            assert "Getting around" in text

    async def test_typing_filters(self, home_app):
        async with Headless(home_app) as h:
            await h.press("question_mark")
            view = h.top
            total = len(view.picker.list.rows)
            await h.feed_bytes(b"brain dump")
            assert 0 < len(view.picker.list.rows) < total

    async def test_enter_and_escape_both_close(self, home_app):
        async with Headless(home_app) as h:
            await h.press("question_mark", "enter")
            assert h.top is h.app.home
            await h.press("question_mark", "escape")
            assert h.top is h.app.home


def test_help_contexts_have_distinct_items():
    home = HelpView(context="home")._get_items()
    todo = HelpView(context="todo")._get_items()
    trash = HelpView(context="trash")._get_items()
    unknown = HelpView(context="nope")._get_items()
    assert [i[1] for i in home] != [i[1] for i in todo]
    assert any("Spawn Claude session from this todo" in m for _, m in todo)
    assert any("Restore selected item" in m for _, m in trash)
    assert [i[1] for i in unknown] == [i[1] for i in home]  # fallback


# ─── AutoModeStartView (AutoModeStartScreen key walk) ────────────────


def _backlog():
    return [
        TodoItem(text="todo one", created_at="2026-07-01T10:00:00"),
        TodoItem(text="todo two", created_at="2026-07-02T10:00:00"),
        TodoItem(text="todo three", created_at="2026-07-03T10:00:00",
                 origin="crystallized"),
    ]


@pytest.mark.asyncio
class TestAutoModeStartView:
    async def test_renders_unchecked_rows_and_counts(self, app):
        async with Headless(app) as h:
            await push_modal(h, AutoModeStartView("My ws", _backlog()))
            text = h.screen_text()
            assert "Auto mode: My ws" in text
            assert "0/3 selected" in text
            assert text.count("○") == 3 and "◉" not in text

    async def test_space_toggles_current(self, app):
        async with Headless(app) as h:
            view = AutoModeStartView("ws", _backlog())
            await push_modal(h, view)
            await h.press("space")
            assert "1/3 selected" in h.screen_text()
            assert "◉" in h.screen_text()
            await h.press("space")  # toggle back off
            assert "0/3 selected" in h.screen_text()

    async def test_a_selects_all_n_none(self, app):
        async with Headless(app) as h:
            view = AutoModeStartView("ws", _backlog())
            await push_modal(h, view)
            await h.press("a")
            assert "3/3 selected" in h.screen_text()
            await h.press("n")
            assert "0/3 selected" in h.screen_text()

    async def test_enter_dismisses_selected_id_set(self, app):
        todos = _backlog()
        async with Headless(app) as h:
            view = AutoModeStartView("ws", todos)
            results = await push_modal(h, view)
            await h.press("space", "j", "space", "enter")
            assert results == [{todos[0].id, todos[1].id}]

    async def test_escape_cancels_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, AutoModeStartView("ws", _backlog()))
            await h.press("a", "escape")
            assert results == [None]


# ─── TrashView (TrashScreen + palette wiring) ────────────────────────


@pytest.fixture
def trash_app(home_app):
    """home_app with one soft-deleted session on the first workstream."""
    s = make_session(1)
    home_app.state.sessions = [s]
    ws = home_app.state.store.active[0]
    ws.deleted_sessions[s.session_id] = "2026-07-07T09:00:00+00:00"
    home_app.state.store.update(ws)
    home_app._ws_id = ws.id
    home_app._sid = s.session_id
    return home_app


@pytest.mark.asyncio
class TestTrashView:
    async def test_renders_grouped_entries(self, trash_app):
        async with Headless(trash_app) as h:
            ws_name = h.app.state.get_ws(h.app._ws_id).name
            await push_modal(h, TrashView(h.app.state))
            text = h.screen_text()
            assert "Trash" in text
            assert f"◆ {ws_name}" in text  # group header row
            assert "aaaabbb1" in text  # the session block (short sid)

    async def test_empty_trash_renders_placeholder(self, home_app):
        async with Headless(home_app) as h:
            await push_modal(h, TrashView(h.app.state))
            assert "Trash is empty" in h.screen_text()

    async def test_u_restores(self, trash_app):
        async with Headless(trash_app) as h:
            await push_modal(h, TrashView(h.app.state))
            await h.press("u")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert ws.deleted_sessions == {}
            assert h.app.toast_text == "Restored"
            assert "Trash is empty" in h.screen_text()

    async def test_D_purges_after_confirm(self, trash_app):
        async with Headless(trash_app) as h:
            await push_modal(h, TrashView(h.app.state))
            await h.press("D")
            assert isinstance(h.top, ConfirmView)
            assert "Purge" in h.screen_text()
            await h.press("y")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert ws.deleted_sessions == {}
            assert h.app.toast_text == "Purged"

    async def test_D_confirm_n_keeps(self, trash_app):
        async with Headless(trash_app) as h:
            await push_modal(h, TrashView(h.app.state))
            await h.press("D", "n")
            ws = h.app.state.get_ws(h.app._ws_id)
            assert h.app._sid in ws.deleted_sessions

    async def test_reachable_via_palette_trash_command(self, trash_app):
        async with Headless(trash_app) as h:
            await h.press("colon")
            await h.feed_bytes(b"trash")
            await h.press("enter")
            assert isinstance(h.top, TrashView)
            await h.press("escape")
            assert h.top is h.app.home

    async def test_question_mark_opens_trash_help(self, trash_app):
        async with Headless(trash_app) as h:
            await push_modal(h, TrashView(h.app.state))
            await h.press("question_mark")
            assert isinstance(h.top, HelpView)
            assert "Trash — Help" in h.screen_text()


# ─── Command palette (':' → FuzzyModalView over state commands) ──────


@pytest.mark.asyncio
class TestCommandPalette:
    async def test_colon_opens_palette_with_commands(self, home_app):
        async with Headless(home_app) as h:
            await h.press("colon")
            assert isinstance(h.top, FuzzyModalView)
            text = h.screen_text()
            assert "Command Palette" in text
            assert "braindump" in text  # alias column renders

    async def test_archive_command_executes_state_command(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            name = h.app.state.get_ws(ws_id).name
            await h.press("colon")
            await h.feed_bytes(b"archive")
            assert h.top.picker.highlighted_id == "archive"
            await h.press("enter")
            assert h.top is h.app.home
            assert ws_id in [w.id for w in h.app.state.store.archived]
            assert h.app.toast_text == f"Archived: {name}"

    async def test_help_command_pushes_help_view(self, home_app):
        async with Headless(home_app) as h:
            await h.press("colon")
            await h.feed_bytes(b"help")
            await h.press("enter")
            assert isinstance(h.top, HelpView)

    async def test_escape_closes_without_running(self, home_app):
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("colon", "escape")
            assert h.top is h.app.home
            assert len(h.app.state.store.active) == before
