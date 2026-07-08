"""Wave-B form/note screen tests (P4-B): QuickNote, TodoContext, Add,
AddLink, BrainDump — key walks of the Textual originals' BINDINGS, plus
the home wiring flows ('n', 'a', 'W', and the full 'b' chain with the
brain parse mocked) mutating a temp store through OrchApp.
"""

from types import SimpleNamespace

import pytest

import brain
from brain import ParsedTask
from models import Category, TodoItem
from tui.app import App
from tui.orch_app import OrchApp
from tui.testing import Headless
from tui.view import View
from tui.views.add import AddView
from tui.views.add_link import AddLinkView
from tui.views.brain_dump import BrainDumpView
from tui.views.brain_preview import BrainPreviewView
from tui.views.quick_note import QuickNoteView
from tui.views.todo import TodoContextView


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


# ─── QuickNoteView (QuickNoteScreen key walk) ────────────────────────


@pytest.mark.asyncio
class TestQuickNoteView:
    WS = SimpleNamespace(name="My stream")

    async def test_ctrl_s_saves_stripped_text(self, app):
        async with Headless(app) as h:
            view = QuickNoteView(self.WS)
            results = await push_modal(h, view)
            assert "Todo: My stream" in h.screen_text()
            await h.feed_bytes(b"  buy milk ")
            await h.press("ctrl+s")
            assert results == ["buy milk"]

    async def test_multiline_preserved(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, QuickNoteView(self.WS))
            await h.feed_bytes(b"line one")
            await h.press("enter")
            await h.feed_bytes(b"line two")
            await h.press("ctrl+s")
            assert results == ["line one\nline two"]

    async def test_empty_ctrl_s_dismisses_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, QuickNoteView(self.WS))
            await h.press("ctrl+s")
            assert results == [None]

    async def test_escape_cancels_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, QuickNoteView(self.WS))
            await h.feed_bytes(b"kept?")
            await h.press("escape")
            assert results == [None]


# ─── TodoContextView (_TodoContextScreen: every exit saves) ──────────


@pytest.mark.asyncio
class TestTodoContextView:
    def item(self, ctx="old context"):
        return TodoItem(text="fix the bug", context=ctx)

    async def test_prefilled_and_escape_saves(self, app):
        async with Headless(app) as h:
            view = TodoContextView(self.item())
            results = await push_modal(h, view)
            text = h.screen_text()
            assert "Context: fix the bug" in text
            assert "old context" in text
            await h.press("end")
            await h.feed_bytes(b" edited")
            await h.press("escape")
            assert results == ["old context edited"]

    @pytest.mark.parametrize("key", ["ctrl+h", "ctrl+s"])
    async def test_other_exits_save_too(self, app, key):
        async with Headless(app) as h:
            results = await push_modal(h, TodoContextView(self.item("keep")))
            await h.press(key)
            assert results == ["keep"]


# ─── AddView (AddScreen key walk) ────────────────────────────────────


@pytest.mark.asyncio
class TestAddView:
    async def test_fill_fields_and_submit(self, app):
        async with Headless(app) as h:
            view = AddView()
            results = await push_modal(h, view)
            await h.feed_bytes(b"New thing")
            await h.press("tab")
            await h.feed_bytes(b"a desc")
            await h.press("tab", "space")  # cycle PERSONAL → WORK (wraps)
            await h.press("enter")
            assert len(results) == 1
            ws = results[0]
            assert ws.name == "New thing"
            assert ws.description == "a desc"
            assert ws.category == Category.WORK

    async def test_default_category_is_personal(self, app):
        async with Headless(app) as h:
            view = AddView()
            results = await push_modal(h, view)
            await h.feed_bytes(b"n")
            await h.press("enter")
            assert results[0].category == Category.PERSONAL

    async def test_empty_name_stays_open_with_toast(self, app):
        async with Headless(app) as h:
            view = AddView()
            results = await push_modal(h, view)
            await h.press("enter")
            assert results == []
            assert h.top is view
            assert app.toasts == ["Name cannot be empty"]

    async def test_escape_cancels_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, AddView())
            await h.press("escape")
            assert results == [None]


# ─── AddLinkView (AddLinkScreen key walk + live description) ─────────


@pytest.mark.asyncio
class TestAddLinkView:
    async def test_value_focused_and_submit_builds_link(self, app):
        async with Headless(app) as h:
            view = AddLinkView("My ws")
            results = await push_modal(h, view)
            assert "Add Link: My ws" in h.screen_text()
            await h.feed_bytes(b"https://x.test")  # goes to the value field
            await h.press("enter")
            assert len(results) == 1
            link = results[0]
            assert (link.kind, link.label, link.value) == \
                ("url", "url", "https://x.test")

    async def test_cycling_kind_updates_description_line(self, app):
        async with Headless(app) as h:
            view = AddLinkView("ws")
            await push_modal(h, view)
            assert "Web URL" in h.screen_text()  # default kind: url
            await h.press("shift+tab", "space")  # focus kind, cycle → slack
            assert view.kind.value == "slack"
            text = h.screen_text()
            assert "Slack channel or thread URL" in text
            assert "Web URL" not in text

    async def test_kind_cycler_wraps_and_submits(self, app):
        async with Headless(app) as h:
            view = AddLinkView("ws")
            results = await push_modal(h, view)
            await h.feed_bytes(b"UB-1")
            await h.press("shift+tab", "left")  # url → file (cycle back)
            assert view.kind.value == "file"
            await h.press("enter")  # enter on the Cycler submits too
            assert results[0].kind == "file"

    async def test_empty_value_stays_open_with_toast(self, app):
        async with Headless(app) as h:
            view = AddLinkView("ws")
            results = await push_modal(h, view)
            await h.press("enter")
            assert results == []
            assert h.top is view
            assert app.toasts == ["Value cannot be empty"]


# ─── BrainDumpView (BrainDumpScreen key walk) ────────────────────────


@pytest.mark.asyncio
class TestBrainDumpView:
    async def test_ctrl_s_submits_text(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, BrainDumpView())
            assert "Brain Dump" in h.screen_text()
            await h.feed_bytes(b"do a thing, also another")
            await h.press("ctrl+s")
            assert results == ["do a thing, also another"]

    async def test_empty_ctrl_s_stays_open_with_toast(self, app):
        async with Headless(app) as h:
            view = BrainDumpView()
            results = await push_modal(h, view)
            await h.press("ctrl+s")
            assert results == []
            assert h.top is view
            assert app.toasts == ["Nothing to parse"]

    async def test_escape_cancels_none(self, app):
        async with Headless(app) as h:
            results = await push_modal(h, BrainDumpView())
            await h.press("escape")
            assert results == [None]


# ─── Home wiring: n / a / W / b ──────────────────────────────────────


@pytest.mark.asyncio
class TestQuickNoteWiring:
    async def test_n_adds_todo_to_selected_ws(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("n")
            assert isinstance(h.top, QuickNoteView)
            await h.feed_bytes(b"remember this")
            await h.press("ctrl+s")
            assert h.top is h.app.home
            todos = h.app.state.get_ws(ws_id).todos
            assert [t.text for t in todos] == ["remember this"]
            assert h.app.toast_text == "Todo added"

    async def test_n_cancel_adds_nothing(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("n", "escape")
            assert h.top is h.app.home
            assert h.app.state.get_ws(ws_id).todos == []


@pytest.mark.asyncio
class TestAddWiring:
    async def test_a_creates_workstream_and_renders(self, home_app):
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("a")
            assert isinstance(h.top, AddView)
            await h.feed_bytes(b"Fresh stream")
            await h.press("enter")
            assert h.top is h.app.home
            assert len(h.app.state.store.active) == before + 1
            assert h.app.toast_text == "Created: Fresh stream"
            assert "Fresh stream" in h.screen_text()

    async def test_a_escape_creates_nothing(self, home_app):
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("a", "escape")
            assert len(h.app.state.store.active) == before


@pytest.mark.asyncio
class TestAddLinkWiring:
    async def test_W_adds_link_to_selected_ws(self, home_app):
        async with Headless(home_app) as h:
            ws_id = h.app.home.ws_list.highlighted_id
            await h.press("W")
            assert isinstance(h.top, AddLinkView)
            await h.feed_bytes(b"https://example.test/pr/1")
            await h.press("enter")
            links = h.app.state.get_ws(ws_id).links
            assert [(l.kind, l.value) for l in links] == \
                [("url", "https://example.test/pr/1")]
            assert h.app.toast_text.startswith("Added url link to")


def _fake_tasks():
    return [
        ParsedTask(raw_text="fix login page today", name="fix login page",
                   category=Category.WORK),
        ParsedTask(raw_text="water plants", name="water plants",
                   category=Category.PERSONAL),
    ]


@pytest.mark.asyncio
class TestBrainDumpWiring:
    async def test_b_chain_add_creates_workstreams(self, home_app, monkeypatch):
        monkeypatch.setattr(brain, "parse_brain_dump", lambda text: _fake_tasks())
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("b")
            assert isinstance(h.top, BrainDumpView)
            await h.feed_bytes(b"whatever")
            await h.press("ctrl+s")
            assert isinstance(h.top, BrainPreviewView)
            assert "Parsed 2 tasks" in h.screen_text()
            await h.press("y")
            assert h.top is h.app.home
            assert len(h.app.state.store.active) == before + 2
            names = [w.name for w in h.app.state.store.active]
            assert "fix login page" in names and "water plants" in names
            assert h.app.toast_text == "Added 2 workstreams"

    async def test_b_chain_launch_uses_launch_claude_session(self, home_app, monkeypatch):
        monkeypatch.setattr(brain, "parse_brain_dump", lambda text: _fake_tasks())
        launched = []
        async with Headless(home_app) as h:
            h.app.launch_claude_session = lambda ws, **kw: launched.append(ws)
            await h.press("b")
            await h.feed_bytes(b"whatever")
            await h.press("ctrl+s", "l")
            assert [w.name for w in launched] == ["fix login page"]
            assert "launching session" in h.app.toast_text

    async def test_b_chain_no_tasks_toasts(self, home_app, monkeypatch):
        monkeypatch.setattr(brain, "parse_brain_dump", lambda text: [])
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("b")
            await h.feed_bytes(b"???")
            await h.press("ctrl+s")
            assert h.top is h.app.home
            assert h.app.toast_text == "No tasks found in input"
            assert len(h.app.state.store.active) == before

    async def test_b_chain_preview_cancel_creates_nothing(self, home_app, monkeypatch):
        monkeypatch.setattr(brain, "parse_brain_dump", lambda text: _fake_tasks())
        async with Headless(home_app) as h:
            before = len(h.app.state.store.active)
            await h.press("b")
            await h.feed_bytes(b"whatever")
            await h.press("ctrl+s", "escape")
            assert h.top is h.app.home
            assert len(h.app.state.store.active) == before
