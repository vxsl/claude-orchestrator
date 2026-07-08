"""Modal base + wave-A view tests (P4): dismiss semantics, focus ring
cycling, fuzzy filter/cancel paths, result delivery through
App.push(on_result), and the home-wired flows ('C' repo-spawn, 'd'
delete, 'u' archive, 't' trust) on a Headless OrchApp with a temp store.
"""

from types import SimpleNamespace

import pytest

from brain import ParsedTask
from models import Category
from tui.app import App
from tui.layout import Rect
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.view import View
from tui.views.brain_preview import BrainPreviewView
from tui.views.confirm import ConfirmView
from tui.views.modals import FormModalView, FuzzyModalView, ListModalView, ModalView
from tui.views.pickers import (
    SENTINEL_NEW, LinkSessionView, RepoPickerView, WorkstreamPickerView,
)
from tui.views.todo_edit import TodoEditView
from tui.widgets import LineEdit, TextEdit


class Backdrop(View):
    """Opaque root view so modals never empty the stack (which exits)."""

    def render(self, frame, rect) -> None:
        frame.write_markup(0, 0, rect.w, "BACKDROP")


async def push_modal(h, view):
    """Push `view` with a recording on_result callback; returns the list."""
    results = []
    h.app.push(view, on_result=results.append)
    await h.pause()
    return results


@pytest.fixture
def app():
    a = App()
    a.push(Backdrop())
    return a


# ─── ModalView ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestModalView:
    async def test_escape_dismisses_with_cancel_result(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ModalView(title="T"))
            await h.press("escape")
            assert results == [None]
            assert h.top is h.app._stack[0][0]  # back to the backdrop

    async def test_ctrl_h_dismisses(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ModalView(title="T"))
            await h.press("ctrl+h")
            assert results == [None]

    async def test_backspace_dismisses(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ModalView(title="T"))
            await h.press("backspace")
            assert results == [None]

    async def test_cancel_result_override(self, app):
        class Denies(ModalView):
            cancel_result = "denied"

        async with Headless(app) as h:
            results = await push_modal(h, Denies())
            await h.press("escape")
            assert results == ["denied"]

    async def test_renders_border_title_over_backdrop(self, app):
        async with Headless(app) as h:
            await push_modal(h, ModalView(title="My Modal"))
            text = h.screen_text()
            assert "My Modal" in text
            assert "╭" in text and "╰" in text
            assert "BACKDROP" in text  # opaque=False: view below still paints

    async def test_box_centered_and_clamped(self, app):
        async with Headless(app) as h:
            view = ModalView(title="T")
            view.box_size = (60, 10)
            await push_modal(h, view)
            assert view._box == Rect(30, 15, 60, 10)  # centered on 120x40
            h.io.cols, h.io.rows = 40, 8
            h.app._on_resize()
            assert view._box.w <= 40 and view._box.h <= 8

    async def test_other_keys_bubble_unconsumed(self, app):
        async with Headless(app) as h:
            view = ModalView(title="T")
            results = await push_modal(h, view)
            await h.press("z")
            assert results == []  # no dismissal
            assert h.top is view


# ─── ListModalView ───────────────────────────────────────────────────


class ColorList(ListModalView):
    def __init__(self, **kwargs):
        super().__init__(title="Colors", **kwargs)
        self.list.set_rows(
            [("red", "red row", False), ("green", "green row", False),
             ("blue", "blue row", False)]
        )


@pytest.mark.asyncio
class TestListModalView:
    async def test_enter_dismisses_with_highlighted_id(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ColorList())
            await h.press("j", "enter")
            assert results == ["green"]

    async def test_on_selected_hook_overrides_default(self, app):
        class Custom(ColorList):
            def _on_selected(self, item_id):
                self.dismiss(("picked", item_id))

        async with Headless(app) as h:
            results = await push_modal(h, Custom())
            await h.press("G", "enter")
            assert results == [("picked", "blue")]

    async def test_vim_nav_and_rows_render(self, app):
        async with Headless(app) as h:
            view = ColorList()
            await push_modal(h, view)
            assert "green row" in h.screen_text()
            await h.press("G")
            assert view.list.highlighted_id == "blue"
            await h.press("g")
            assert view.list.highlighted_id == "red"

    async def test_page_size_synced_from_body_rect(self, app):
        async with Headless(app) as h:
            view = ColorList()
            await push_modal(h, view)
            assert view.list.page_size == max(1, view.body_rect.h)

    async def test_escape_cancels_with_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ColorList())
            await h.press("escape")
            assert results == [None]

    async def test_fullscreen_uses_whole_screen(self, app):
        class Full(ColorList):
            fullscreen = True

        async with Headless(app) as h:
            view = Full()
            await push_modal(h, view)
            assert view._box == Rect(0, 0, 120, 40)
            assert h.screen_text().splitlines()[0].startswith("╭")


# ─── FuzzyModalView ──────────────────────────────────────────────────


_FRUIT = [("a", "apple"), ("b", "banana"), ("c", "cherry")]


def fruit_picker():
    view = FuzzyModalView(title="Fruit")
    view._get_items = lambda: list(_FRUIT)  # instance-assignment pattern
    return view


@pytest.mark.asyncio
class TestFuzzyModalView:
    async def test_items_load_on_show_and_render(self, app):
        async with Headless(app) as h:
            view = fruit_picker()
            await push_modal(h, view)
            text = h.screen_text()
            assert "apple" in text and "cherry" in text
            assert "3 of 3" in text

    async def test_typing_filters_and_updates_status(self, app):
        async with Headless(app) as h:
            view = fruit_picker()
            await push_modal(h, view)
            await h.feed_bytes(b"ban")
            assert [r[0] for r in view.picker.list.rows] == ["b"]
            assert "1 of 3" in h.screen_text()

    async def test_enter_dismisses_with_highlighted_id(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, fruit_picker())
            await h.feed_bytes(b"cher")
            await h.press("enter")
            assert results == ["c"]

    async def test_instance_assigned_on_selected(self, app):
        async with Headless(app) as h:
            view = fruit_picker()
            view._on_selected = lambda item_id: view.dismiss(("sel", item_id))
            results = await push_modal(h, view)
            await h.press("enter")
            assert results == [("sel", "a")]

    async def test_escape_cancels(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, fruit_picker())
            await h.press("escape")
            assert results == [None]

    async def test_physical_backspace_on_empty_query_cancels(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, fruit_picker())
            await h.feed_bytes(b"\x7f")
            assert results == [None]

    async def test_backspace_with_text_deletes_not_cancels(self, app):
        async with Headless(app) as h:
            view = fruit_picker()
            results = await push_modal(h, view)
            await h.feed_bytes(b"ap")
            await h.feed_bytes(b"\x7f")
            assert results == []
            assert view.picker.query == "a"

    async def test_ctrl_h_deletes_never_cancels(self, app):
        async with Headless(app) as h:
            view = fruit_picker()
            results = await push_modal(h, view)
            await h.feed_bytes(b"a")
            await h.feed_bytes(b"\x08")  # classic-terminal ctrl+h
            assert view.picker.query == ""
            await h.feed_bytes(b"\x08")  # empty query: still only deletes
            assert results == []
            assert h.top is view


# ─── FormModalView ───────────────────────────────────────────────────


class TwoFieldForm(FormModalView):
    def __init__(self):
        super().__init__(title="Form")
        self.a = self.add_field("A", LineEdit("alpha"))
        self.b = self.add_field("B", LineEdit("beta"))

    def _on_submit(self):
        return (self.a.text, self.b.text)


class NoteForm(FormModalView):
    def __init__(self):
        super().__init__(title="Note")
        self.area = self.add_field("Body", TextEdit("hello"))

    def _on_submit(self):
        return "\n".join(self.area.lines)


@pytest.mark.asyncio
class TestFormModalView:
    async def test_tab_cycles_focus_ring(self, app):
        async with Headless(app) as h:
            view = TwoFieldForm()
            await push_modal(h, view)
            assert view.ring.focused is view.a
            await h.press("tab")
            assert view.ring.focused is view.b
            await h.press("tab")
            assert view.ring.focused is view.a
            await h.press("shift+tab")
            assert view.ring.focused is view.b

    async def test_typing_goes_to_focused_field(self, app):
        async with Headless(app) as h:
            view = TwoFieldForm()
            await push_modal(h, view)
            await h.press("x")
            assert view.a.text == "alphax"
            await h.press("tab", "y")
            assert view.b.text == "betay"

    async def test_enter_submits_from_any_line_edit(self, app):
        async with Headless(app) as h:
            view = TwoFieldForm()
            results = await push_modal(h, view)
            await h.press("tab", "enter")  # submit from the second field
            assert results == [("alpha", "beta")]

    async def test_none_result_keeps_form_open(self, app):
        class Refuses(FormModalView):
            def __init__(self):
                super().__init__(title="R")
                self.add_field("A", LineEdit(""))

        async with Headless(app) as h:
            view = Refuses()
            results = await push_modal(h, view)
            await h.press("enter")
            assert results == []
            assert h.top is view

    async def test_enter_in_textedit_inserts_newline_not_submit(self, app):
        async with Headless(app) as h:
            view = NoteForm()
            results = await push_modal(h, view)
            await h.press("end", "enter")
            assert results == []
            assert view.area.lines == ["hello", ""]

    async def test_ctrl_s_submits_textedit_form(self, app):
        async with Headless(app) as h:
            view = NoteForm()
            results = await push_modal(h, view)
            await h.press("end", "enter")
            await h.feed_bytes(b"world")
            await h.press("ctrl+s")
            assert results == ["hello\nworld"]

    async def test_escape_cancels_with_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, TwoFieldForm())
            await h.press("escape")
            assert results == [None]

    async def test_labels_render(self, app):
        async with Headless(app) as h:
            await push_modal(h, TwoFieldForm())
            text = h.screen_text()
            assert "alpha" in text and "beta" in text


# ─── ConfirmView (ConfirmScreen key walk: y / n / escape / ^H) ───────


@pytest.mark.asyncio
class TestConfirmView:
    async def test_y_confirms_true(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ConfirmView("Delete thing?"))
            assert "Delete thing?" in h.screen_text()
            await h.press("y")
            assert results == [True]

    async def test_n_denies_false(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ConfirmView("Sure?"))
            await h.press("n")
            assert results == [False]

    async def test_escape_denies_false(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ConfirmView("Sure?"))
            await h.press("escape")
            assert results == [False]

    async def test_backspace_goes_back_false(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, ConfirmView("Sure?"))
            await h.press("backspace")
            assert results == [False]

    async def test_multiline_message_renders(self, app):
        async with Headless(app) as h:
            await push_modal(h, ConfirmView("line one\nline two"))
            text = h.screen_text()
            assert "line one" in text and "line two" in text


# ─── TodoEditView (_TodoEditScreen key walk) ─────────────────────────


@pytest.mark.asyncio
class TestTodoEditView:
    async def test_prefilled_and_enter_saves_edited_text(self, app):
        async with Headless(app) as h:
            view = TodoEditView("fix the bug")
            results = await push_modal(h, view)
            assert view.input.text == "fix the bug"
            assert "fix the bug" in h.screen_text()
            await h.feed_bytes(b" now")
            await h.press("enter")
            assert results == ["fix the bug now"]

    async def test_emptied_text_saves_none(self, app):
        async with Headless(app) as h:
            view = TodoEditView("ab")
            results = await push_modal(h, view)
            await h.press("backspace", "backspace", "enter")
            assert results == [None]

    async def test_escape_cancels_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, TodoEditView("keep me"))
            await h.press("escape")
            assert results == [None]

    async def test_ctrl_h_goes_back_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, TodoEditView("keep me"))
            await h.press("ctrl+h")
            assert results == [None]


# ─── BrainPreviewView (BrainPreviewScreen key walk) ──────────────────


def _tasks():
    return [
        ParsedTask(raw_text="fix the login page today", name="fix the login page",
                   category=Category.WORK),
        ParsedTask(raw_text="water plants", name="water plants",
                   category=Category.PERSONAL),
    ]


@pytest.mark.asyncio
class TestBrainPreviewView:
    async def test_renders_task_names_and_count(self, app):
        async with Headless(app) as h:
            await push_modal(h, BrainPreviewView(_tasks()))
            text = h.screen_text()
            assert "Parsed 2 tasks" in text
            assert "fix the login page" in text
            assert "water plants" in text

    @pytest.mark.parametrize("key,expected", [
        ("enter", "add"), ("y", "add"), ("l", "launch"),
        ("escape", ""), ("n", ""), ("backspace", ""), ("ctrl+h", ""),
    ])
    async def test_key_walk(self, app, key, expected):
        async with Headless(app) as h:
            results = await push_modal(h, BrainPreviewView(_tasks()))
            await h.press(key)
            assert results == [expected]


# ─── Picker views (LinkSession / RepoPicker / WorkstreamPicker) ──────


@pytest.mark.asyncio
class TestLinkSessionView:
    async def test_pick_dismisses_with_workstream(self, populated_store, app):
        session = SimpleNamespace(display_name="my session")
        async with Headless(app) as h:
            view = LinkSessionView(populated_store, session)
            results = await push_modal(h, view)
            assert "Link session: my session" in h.screen_text()
            await h.feed_bytes(b"personal")
            await h.press("enter")
            assert len(results) == 1
            assert results[0].name == "Personal project"

    async def test_escape_cancels_none(self, populated_store, app):
        session = SimpleNamespace(display_name="s")
        async with Headless(app) as h:
            results = await push_modal(h, LinkSessionView(populated_store, session))
            await h.press("escape")
            assert results == [None]


@pytest.mark.asyncio
class TestRepoPickerView:
    REPOS = ["/home/u/dev/zeta", "/home/u/dev/alpha", "/home/u/dev/mid"]

    async def test_ws_repos_sort_first_and_enter_picks_path(self, app):
        async with Headless(app) as h:
            view = RepoPickerView(self.REPOS, {"/home/u/dev/zeta": 2})
            results = await push_modal(h, view)
            ids = [rid for rid, _, _ in view.picker.list.rows]
            assert ids == ["/home/u/dev/zeta", "/home/u/dev/alpha", "/home/u/dev/mid"]
            assert "(2 ws)" in h.screen_text()
            await h.press("j", "enter")
            assert results == ["/home/u/dev/alpha"]

    async def test_filter_then_pick(self, app):
        async with Headless(app) as h:
            view = RepoPickerView(self.REPOS, {})
            results = await push_modal(h, view)
            await h.feed_bytes(b"mid")
            await h.press("enter")
            assert results == ["/home/u/dev/mid"]


@pytest.mark.asyncio
class TestWorkstreamPickerView:
    async def test_pick_existing_dismisses_with_workstream(self, populated_store, app):
        streams = populated_store.active[:2]
        async with Headless(app) as h:
            view = WorkstreamPickerView(streams, "/home/u/dev/repo")
            results = await push_modal(h, view)
            assert "Workstreams in repo" in h.screen_text()
            await h.press("enter")
            assert results == [streams[0]]

    async def test_sentinel_row_dismisses_with_new_marker(self, populated_store, app):
        streams = populated_store.active[:2]
        async with Headless(app) as h:
            view = WorkstreamPickerView(streams, "/home/u/dev/repo")
            results = await push_modal(h, view)
            assert "+ Create new workstream" in h.screen_text()
            # sentinel is the last row ("G" would type into the query)
            await h.press("j", "j", "enter")
            assert results == [SENTINEL_NEW]

    async def test_escape_cancels_none(self, populated_store, app):
        async with Headless(app) as h:
            results = await push_modal(
                h, WorkstreamPickerView(populated_store.active[:2], "/r")
            )
            await h.press("escape")
            assert results == [None]


# ─── Home wiring (OrchApp, temp store, pollers off) ──────────────────


@pytest.fixture
def home_app(populated_store):
    return OrchApp(store_path=populated_store.path, pollers=False)


@pytest.mark.asyncio
class TestRepoSpawnWiring:
    async def test_C_opens_repo_picker_and_escape_backs_out(self, home_app):
        async with Headless(home_app) as h:
            h.app.state._all_repos = ["/home/u/dev/repoA"]  # skip the ~ scan
            await h.press("C")
            assert isinstance(h.top, RepoPickerView)
            text = h.screen_text()
            assert "Select Repository" in text
            assert "repoA" in text
            await h.press("escape")
            assert h.top is h.app.home  # clean back-out, nothing launched

    async def test_full_flow_multi_match_create_new(self, home_app):
        async with Headless(home_app) as h:
            repo = "/home/u/dev/shared"
            ws1, ws2 = h.app.state.store.active[:2]
            ws1.repo_path = repo
            ws2.repo_path = repo
            h.app.state._all_repos = [repo]
            launched = []
            h.app.launch_claude_session = lambda ws, **kw: launched.append(ws)
            before = len(h.app.state.store.active)

            await h.press("C", "enter")  # pick the repo → 2 matches
            assert isinstance(h.top, WorkstreamPickerView)
            await h.press("j", "j", "enter")  # "+ Create new workstream"
            assert h.top is h.app.home
            assert len(launched) == 1
            assert launched[0].repo_path == repo
            assert len(h.app.state.store.active) == before + 1

    async def test_full_flow_single_match_launches_directly(self, home_app):
        async with Headless(home_app) as h:
            repo = "/home/u/dev/solo"
            ws1 = h.app.state.store.active[0]
            ws1.repo_path = repo
            h.app.state._all_repos = [repo]
            launched = []
            h.app.launch_claude_session = lambda ws, **kw: launched.append(ws)

            await h.press("C", "enter")
            assert h.top is h.app.home
            assert launched == [ws1]


@pytest.mark.asyncio
class TestDeleteWiring:
    async def test_d_confirm_y_deletes(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            name = h.app.state.get_ws(ws_id).name
            await h.press("d")
            assert isinstance(h.top, ConfirmView)
            assert name in h.screen_text()
            await h.press("y")
            assert h.top is h.app.home
            assert h.app.state.get_ws(ws_id) is None
            assert f"Deleted: {name}" == h.app.toast_text

    async def test_d_confirm_n_keeps(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("d", "n")
            assert h.top is h.app.home
            assert h.app.state.get_ws(ws_id) is not None


@pytest.mark.asyncio
class TestArchiveWiring:
    async def test_u_archives_without_confirm(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            name = h.app.state.get_ws(ws_id).name
            await h.press("u")
            assert h.top is h.app.home  # no confirm modal, as in app.py
            assert ws_id not in [w.id for w in h.app.state.store.active]
            assert ws_id in [w.id for w in h.app.state.store.archived]
            assert f"Archived: {name}" == h.app.toast_text

    async def test_u_in_archived_filter_restores(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("u", "3")  # archive it, switch to archived filter
            assert h.app.home.ws_list.highlighted_id == ws_id
            await h.press("u")
            assert ws_id in [w.id for w in h.app.state.store.active]
            assert h.app.toast_text.startswith("Restored:")


@pytest.mark.asyncio
class TestTrustWiring:
    async def test_t_confirms_then_toggles(self, home_app, monkeypatch, tmp_path):
        import trust

        calls = []
        monkeypatch.setattr(trust, "is_trusted", lambda p: False)
        monkeypatch.setattr(trust, "toggle", lambda p: calls.append(p) or True)
        async with Headless(home_app) as h:
            ws = h.app.state.get_ws(h.app.home.ws_list.highlighted_id)
            ws.repo_path = str(tmp_path)
            await h.press("t")
            assert isinstance(h.top, ConfirmView)
            assert "Trust" in h.screen_text()
            await h.press("y")
            assert calls == [str(tmp_path)]
            assert h.app.toast_text.startswith("Trusted:")

    async def test_t_without_cwd_toasts(self, home_app):
        async with Headless(home_app) as h:
            ws = h.app.state.get_ws(h.app.home.ws_list.highlighted_id)
            ws.repo_path = ""
            await h.press("t")
            assert h.top is h.app.home
            assert "No cwd set" in h.app.toast_text
