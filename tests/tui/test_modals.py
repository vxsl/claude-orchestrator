"""Modal base + wave-A view tests (P4): dismiss semantics, focus ring
cycling, fuzzy filter/cancel paths, and result delivery through
App.push(on_result), all via the Headless harness.
"""

import pytest

from tui.app import App
from tui.layout import Rect
from tui.testing import Headless
from tui.view import View
from tui.views.modals import FormModalView, FuzzyModalView, ListModalView, ModalView
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
