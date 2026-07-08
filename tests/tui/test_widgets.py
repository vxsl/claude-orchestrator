"""Widget primitive tests: ListView nav/windowing, LineEdit editing,
TextEdit multiline, FuzzyList filtering, footer_markup. Widgets are plain
classes — mostly direct calls with synthesized KeyEvents; one Headless
integration test proves composition inside a real view."""

import pytest

import config
from tui.frame import Frame
from tui.keys import KeyEvent
from tui.testing import make_key_event
from tui.widgets import (
    HIGHLIGHT_BG, Cycler, FuzzyList, LineEdit, ListView, TextEdit,
    footer_markup, strip_markup,
)


def press(widget, *names):
    for name in names:
        widget.handle_key(make_key_event(name))


def rows_of(*specs):
    """spec: id, or (id, disabled)"""
    out = []
    for spec in specs:
        if isinstance(spec, tuple):
            out.append((spec[0], f"row {spec[0]}", spec[1]))
        else:
            out.append((spec, f"row {spec}", False))
    return out


# ─── ListView: navigation ─────────────────────────────────────────


def make_list(*specs):
    lv = ListView()
    lv.set_rows(rows_of(*specs))
    return lv


def test_empty_list_has_no_highlight():
    lv = ListView()
    assert lv.highlighted == -1 and lv.highlighted_id is None
    assert lv.handle_key(make_key_event("j")) is True  # consumed, no crash


def test_set_rows_highlights_first_enabled():
    lv = make_list(("sep", True), "a", "b")
    assert lv.highlighted == 1 and lv.highlighted_id == "a"


def test_vim_and_arrow_nav():
    lv = make_list("a", "b", "c")
    for down in ("j", "down", "ctrl+n"):
        lv.highlighted = 0
        press(lv, down)
        assert lv.highlighted == 1, down
    for up in ("k", "up", "ctrl+p"):
        lv.highlighted = 1
        press(lv, up)
        assert lv.highlighted == 0, up


def test_no_wrap_at_edges():
    lv = make_list("a", "b")
    press(lv, "k")
    assert lv.highlighted == 0  # top stays top
    press(lv, "j", "j", "j")
    assert lv.highlighted == 1  # bottom stays bottom


def test_home_end():
    lv = make_list("a", "b", "c", "d")
    press(lv, "G")
    assert lv.highlighted_id == "d"
    press(lv, "g")
    assert lv.highlighted_id == "a"


def test_home_end_skip_disabled_edges():
    lv = make_list(("top", True), "a", "b", ("bottom", True))
    press(lv, "G")
    assert lv.highlighted_id == "b"
    press(lv, "g")
    assert lv.highlighted_id == "a"


def test_half_page_uses_page_size():
    lv = make_list(*range(20))
    lv.page_size = 10
    press(lv, "ctrl+d")
    assert lv.highlighted == 5
    press(lv, "ctrl+d")
    assert lv.highlighted == 10
    press(lv, "ctrl+u")
    assert lv.highlighted == 5


def test_half_page_clamps_and_skips_disabled():
    lv = ListView()
    lv.set_rows(rows_of(0, 1, 2, (3, True), 4))
    lv.page_size = 6
    press(lv, "ctrl+d")  # target 3 is disabled: continue downward
    assert lv.highlighted == 4
    press(lv, "ctrl+d")  # already at bottom
    assert lv.highlighted == 4
    press(lv, "ctrl+u")  # target 1
    assert lv.highlighted == 1


def test_nav_skips_disabled_separators():
    lv = make_list("a", ("sep1", True), ("sep2", True), "b")
    assert lv.highlighted_id == "a"
    press(lv, "j")
    assert lv.highlighted_id == "b"
    press(lv, "k")
    assert lv.highlighted_id == "a"


def test_disabled_tail_blocks_downward_move():
    lv = make_list("a", ("sep", True))
    press(lv, "j")
    assert lv.highlighted_id == "a"  # nothing enabled below


def test_all_disabled_rows_never_highlighted():
    lv = make_list(("s1", True), ("s2", True))
    assert lv.highlighted == -1
    press(lv, "j", "k", "g", "G", "ctrl+d")
    assert lv.highlighted == -1


# ─── ListView: set_rows / update_row / callbacks ──────────────────


def test_set_rows_keeps_highlight_by_id():
    lv = make_list("a", "b", "c")
    press(lv, "j")  # highlight b
    lv.set_rows(rows_of("c", "b", "a"))
    assert lv.highlighted_id == "b" and lv.highlighted == 1


def test_set_rows_clamps_when_id_gone():
    lv = make_list("a", "b", "c")
    press(lv, "j")  # b (index 1)
    lv.set_rows(rows_of("a", "c"))
    assert lv.highlighted == 1 and lv.highlighted_id == "c"
    lv.set_rows(rows_of("z"))
    assert lv.highlighted == 0 and lv.highlighted_id == "z"


def test_set_rows_keep_id_false_clamps_by_index():
    lv = make_list("a", "b", "c")
    press(lv, "j")  # index 1
    lv.set_rows(rows_of("a", "b", "c"), keep_id=False)
    assert lv.highlighted == 1


def test_set_rows_empty_resets():
    lv = make_list("a")
    lv.set_rows([])
    assert lv.highlighted == -1 and lv.highlighted_id is None


def test_update_row_swaps_text_in_place():
    lv = make_list("a", "b")
    press(lv, "j")
    highlights = []
    lv.on_highlight = highlights.append
    assert lv.update_row("a", "[bold]spinning[/bold]") is True
    assert lv.rows[0] == ("a", "[bold]spinning[/bold]", False)
    assert lv.highlighted_id == "b"  # untouched
    assert highlights == []  # cheap path: no callbacks
    assert lv.update_row("missing", "x") is False


def test_on_select_fires_for_enter_and_l():
    lv = make_list("a", "b")
    picked = []
    lv.on_select = picked.append
    press(lv, "enter", "j", "l")
    assert picked == ["a", "b"]


def test_select_without_callback_not_consumed():
    lv = make_list("a")
    assert lv.handle_key(make_key_event("enter")) is False


def test_on_highlight_fires_on_movement_and_rebuild():
    lv = ListView()
    seen = []
    lv.on_highlight = seen.append
    lv.set_rows(rows_of("a", "b"))
    press(lv, "j", "j")  # second j hits the edge: no event
    assert seen == ["a", "b"]


# ─── ListView: render ─────────────────────────────────────────────


def test_render_exact_height_and_padding():
    lv = make_list("a", "b")
    lines = lv.render(40, 5)
    assert len(lines) == 5
    assert lines[2:] == ["", "", ""]
    assert lv.render(40, 0) == []


def test_render_highlight_wraps_row():
    lv = make_list("a", "b")
    lines = lv.render(40, 2)
    assert lines[0] == f"[on {HIGHLIGHT_BG}]row a[/]"
    assert lines[1] == "row b"


def test_render_windows_around_highlight():
    lv = make_list(*range(10))
    press(lv, *["j"] * 4)  # highlight index 4
    lines = lv.render(40, 3)
    assert lines == ["row 2", "row 3", f"[on {HIGHLIGHT_BG}]row 4[/]"]
    press(lv, *["k"] * 3)  # back up to index 1: window scrolls up
    lines = lv.render(40, 3)
    assert lines == [f"[on {HIGHLIGHT_BG}]row 1[/]", "row 2", "row 3"]


def test_render_window_stable_when_highlight_visible():
    lv = make_list(*range(10))
    press(lv, *["j"] * 5)
    lv.render(40, 3)  # scroll settles at 3
    press(lv, "k")  # index 4, still visible
    assert lv.render(40, 3)[1] == f"[on {HIGHLIGHT_BG}]row 4[/]"


def test_highlight_with_nested_markup_renders_through_frame():
    lv = ListView()
    lv.set_rows([("a", "[#ff8800]hot[/] rest", False)])
    line = lv.render(20, 1)[0]
    f = Frame(20, 1)
    f.write_markup(0, 0, 20, line)  # must not raise
    assert f.plain_lines()[0] == "hot rest" + " " * 12  # plain text unchanged
    seg = f.rows[0][0]
    assert seg.style.bgcolor.name == HIGHLIGHT_BG  # outer bg applied
    assert seg.style.color.name == "#ff8800"  # inner color wins


def test_render_passes_wide_rows_through_unmodified():
    wide = "[cyan]" + "x" * 100 + "[/cyan]"
    lv = ListView()
    lv.set_rows([("a", wide, False), ("b", "short", False)])
    lines = lv.render(20, 2)
    assert lines[0] == f"[on {HIGHLIGHT_BG}]{wide}[/]"  # no cropping here
    f = Frame(20, 1)
    f.write_markup(0, 0, 20, lines[0])  # Frame crops without error
    assert f.plain_lines()[0] == "x" * 20


# ─── LineEdit ─────────────────────────────────────────────────────


def type_text(widget, text):
    for ch in text:
        widget.handle_key(make_key_event(ch))


def test_lineedit_typing_and_on_change():
    le = LineEdit()
    changes = []
    le.on_change = changes.append
    type_text(le, "ab")
    press(le, "left")
    type_text(le, "X")
    assert le.text == "aXb" and le.cursor == 2
    assert changes == ["a", "ab", "aXb"]


def test_lineedit_cursor_movement_clamps():
    le = LineEdit("hi")
    press(le, "left", "left", "left")
    assert le.cursor == 0
    press(le, "right", "right", "right")
    assert le.cursor == 2
    press(le, "home")
    assert le.cursor == 0
    press(le, "end")
    assert le.cursor == 2


def test_lineedit_backspace_and_delete():
    le = LineEdit("abc")
    press(le, "backspace")
    assert le.text == "ab"
    press(le, "home", "delete")
    assert le.text == "b"


def test_lineedit_ctrl_h_char_deletes_like_backspace():
    le = LineEdit("ab")
    assert le.handle_key(KeyEvent("backspace", "\x08")) is True
    assert le.text == "a"
    assert le.handle_key(KeyEvent("backspace", "\x7f")) is True
    assert le.text == ""


def test_lineedit_empty_backspace_hook():
    le = LineEdit()
    hits = []
    le.on_empty_backspace = lambda: hits.append(1)
    assert le.handle_key(KeyEvent("backspace", "\x7f")) is True
    assert le.handle_key(KeyEvent("backspace", "\x08")) is True
    assert hits == [1, 1]
    le.text, le.cursor = "x", 1
    press(le, "backspace")
    assert le.text == "" and hits == [1, 1]  # hook only when already empty


def test_lineedit_ctrl_u_clears_to_start():
    le = LineEdit("hello")
    press(le, "left", "left", "ctrl+u")
    assert le.text == "lo" and le.cursor == 0


def test_lineedit_ctrl_w_deletes_word_back():
    le = LineEdit("foo bar  ")
    press(le, "ctrl+w")
    assert le.text == "foo " and le.cursor == 4
    press(le, "ctrl+w")
    assert le.text == ""


def test_lineedit_submit_and_cancel():
    le = LineEdit()
    assert le.handle_key(make_key_event("enter")) is False  # no callback
    got = []
    le.on_submit = got.append
    le.on_cancel = lambda: got.append("cancel")
    type_text(le, "hi")
    press(le, "enter", "escape")
    assert got == ["hi", "cancel"]


def test_lineedit_ignores_non_text_keys():
    le = LineEdit("a")
    assert le.handle_key(make_key_event("f5")) is False
    assert le.handle_key(make_key_event("ctrl+d")) is False
    assert le.handle_key(KeyEvent("alt+x", "x")) is False  # alt+char ≠ text
    assert le.text == "a"


def test_lineedit_render_scrolls_to_keep_cursor_visible():
    le = LineEdit("abcdefghij")  # cursor at end (10)
    assert le.render(5) == "ghij"
    assert le.cursor_col(5) == 4
    press(le, "home")
    assert le.render(5) == "abcde"
    assert le.cursor_col(5) == 0


def test_lineedit_render_escapes_markup():
    le = LineEdit("[red]x")
    f = Frame(20, 1)
    f.write_markup(0, 0, 20, le.render(20))
    assert f.plain_lines()[0].startswith("[red]x")


# ─── TextEdit ─────────────────────────────────────────────────────


def test_textedit_typing_and_newline_split():
    te = TextEdit()
    type_text(te, "abcd")
    press(te, "left", "left", "enter")
    assert te.lines == ["ab", "cd"]
    assert te.cursor == (1, 0)


def test_textedit_backspace_joins_lines():
    te = TextEdit("ab\ncd")
    te.row, te.col = 1, 0
    press(te, "backspace")
    assert te.lines == ["abcd"] and te.cursor == (0, 2)


def test_textedit_vertical_nav_clamps_column():
    te = TextEdit("long line\nx\nlonger line")
    te.row, te.col = 0, 7
    press(te, "down")
    assert te.cursor == (1, 1)
    press(te, "down", "up", "up", "up")
    assert te.cursor == (0, 1)


def test_textedit_home_end():
    te = TextEdit("hello")
    press(te, "home")
    assert te.col == 0
    press(te, "end")
    assert te.col == 5


def test_textedit_submit_and_cancel():
    te = TextEdit("a\nb")
    got = []
    te.on_submit = got.append
    te.on_cancel = lambda: got.append("cancel")
    press(te, "ctrl+s", "escape")
    assert got == ["a\nb", "cancel"]


def test_textedit_render_soft_wraps_display_only():
    te = TextEdit("abcdefgh\nx")
    lines = te.render(5, 4)
    assert lines == ["abcdefgh"[:5], "fgh", "x", ""]
    assert te.lines == ["abcdefgh", "x"]  # model unwrapped


def test_textedit_cursor_pos_maps_through_wrap():
    te = TextEdit("abcdefgh")
    te.col = 7
    te.render(5, 4)
    assert te.cursor_pos(5) == (2, 1)
    te.col = 5  # exactly at a wrap boundary
    te.render(5, 4)
    assert te.cursor_pos(5) == (0, 1)


def test_textedit_render_scrolls_to_cursor():
    te = TextEdit("\n".join("abcdef"))  # 6 one-char lines
    te.row = 5
    lines = te.render(10, 3)
    assert lines == ["d", "e", "f"]
    assert te.cursor_pos(10) == (0, 2)


def test_textedit_render_escapes_markup():
    te = TextEdit("[red]x")
    f = Frame(20, 1)
    f.write_markup(0, 0, 20, te.render(20, 1)[0])
    assert f.plain_lines()[0].startswith("[red]x")


# ─── strip_markup (ported from tests/test_widgets.py) ─────────────


class TestStripMarkup:
    def test_plain_text(self):
        assert strip_markup("hello world") == "hello world"

    def test_bold_tag(self):
        assert strip_markup("[bold]hello[/bold]") == "hello"

    def test_color_tag(self):
        assert strip_markup("[#58a6ff]blue[/#58a6ff]") == "blue"

    def test_named_color(self):
        assert strip_markup("[dim]faded[/dim]") == "faded"

    def test_escaped_bracket(self):
        assert strip_markup(r"array\[0]") == "array[0]"

    def test_nested_tags(self):
        assert strip_markup("[bold #ff0000]red bold[/bold #ff0000] normal") == "red bold normal"

    def test_empty_string(self):
        assert strip_markup("") == ""

    def test_complex_markup(self):
        text = "[bold]name[/bold]  [dim](3 ws)[/dim]  [#6e7681]~/dev/repo[/#6e7681]"
        assert strip_markup(text) == "name  (3 ws)  ~/dev/repo"


# ─── FuzzyList ────────────────────────────────────────────────────


ITEMS = [("a", "[bold]alpha[/bold]"), ("b", "beta"), ("g", "gamma"), ("d", "delta")]


def test_fuzzylist_initial_shows_all():
    fl = FuzzyList(ITEMS)
    assert [r[0] for r in fl.list.rows] == ["a", "b", "g", "d"]
    assert fl.status == "4 of 4"
    assert fl.highlighted_id == "a"


def test_fuzzylist_typing_narrows_and_updates_status():
    fl = FuzzyList(ITEMS)
    type_text(fl, "al")  # matches alpha only (through its markup)
    assert fl.query == "al"
    assert [r[0] for r in fl.list.rows] == ["a"]
    assert fl.status == "1 of 4"
    type_text(fl, "zz")
    assert fl.status == "0 of 4" and fl.highlighted_id is None


def test_fuzzylist_sorts_by_score_desc_and_highlights_best():
    fl = FuzzyList([("x", "xxab"), ("ab", "ab")])
    type_text(fl, "ab")
    assert [r[0] for r in fl.list.rows] == ["ab", "x"]  # start-of-string wins
    assert fl.highlighted_id == "ab"


def test_fuzzylist_deleting_restores():
    fl = FuzzyList(ITEMS)
    type_text(fl, "be")
    assert fl.status == "1 of 4"
    fl.handle_key(KeyEvent("backspace", "\x7f"))
    fl.handle_key(KeyEvent("backspace", "\x7f"))
    assert fl.status == "4 of 4"


def test_fuzzylist_nav_keys_route_to_list_while_typing():
    fl = FuzzyList(ITEMS)
    press(fl, "j")
    assert fl.highlighted_id == "b" and fl.query == ""  # j navigates, not typed
    press(fl, "ctrl+n")
    assert fl.highlighted_id == "g"
    press(fl, "k", "up")
    assert fl.highlighted_id == "a"


def test_fuzzylist_enter_selects_highlighted():
    fl = FuzzyList(ITEMS)
    picked = []
    fl.on_select = picked.append
    press(fl, "j", "enter")
    assert picked == ["b"]
    type_text(fl, "zzz")  # no matches: enter is consumed but selects nothing
    assert fl.handle_key(make_key_event("enter")) is True
    assert picked == ["b"]


def test_fuzzylist_escape_cancels():
    fl = FuzzyList(ITEMS)
    cancels = []
    fl.on_cancel = lambda: cancels.append(1)
    press(fl, "escape")
    assert cancels == [1]


def test_fuzzylist_physical_backspace_on_empty_query_cancels():
    fl = FuzzyList(ITEMS)
    cancels = []
    fl.on_cancel = lambda: cancels.append(1)
    assert fl.handle_key(KeyEvent("backspace", "\x7f")) is True
    assert cancels == [1]
    type_text(fl, "b")
    fl.handle_key(KeyEvent("backspace", "\x7f"))  # deletes instead
    assert cancels == [1] and fl.query == ""


def test_fuzzylist_ctrl_h_never_cancels_only_deletes():
    fl = FuzzyList(ITEMS)
    cancels = []
    fl.on_cancel = lambda: cancels.append(1)
    assert fl.handle_key(KeyEvent("backspace", "\x08")) is True  # empty: no-op
    assert cancels == []
    type_text(fl, "be")
    fl.handle_key(KeyEvent("backspace", "\x08"))  # classic-terminal ctrl+h
    assert fl.query == "b"
    fl.handle_key(KeyEvent("ctrl+h", None))  # kitty CSI-u form
    assert fl.query == "" and cancels == []


def test_fuzzylist_highlight_callback_tracks_top_match():
    fl = FuzzyList([("ab", "ab"), ("x", "xxy")])
    seen = []
    fl.list.on_highlight = seen.append
    type_text(fl, "x")  # top match changes ab → x
    assert seen == ["x"] and fl.highlighted_id == "x"
    fl.handle_key(KeyEvent("backspace", "\x7f"))  # original order: ab on top
    assert seen == ["x", "ab"]


def test_fuzzylist_set_items_refilters_with_current_query():
    fl = FuzzyList(ITEMS)
    type_text(fl, "be")
    fl.set_items([("b2", "berry"), ("z", "zzz")])
    assert [r[0] for r in fl.list.rows] == ["b2"]
    assert fl.status == "1 of 2"


# ─── Cycler ───────────────────────────────────────────────────────


class TestCycler:
    OPTS = [("w", "work"), ("p", "personal"), ("m", "meta")]

    def test_starts_at_given_value(self):
        assert Cycler(self.OPTS, value="p").value == "p"
        assert Cycler(self.OPTS).value == "w"  # default: first option

    def test_right_and_space_cycle_forward_with_wrap(self):
        c = Cycler(self.OPTS)
        press(c, "right", "space")
        assert c.value == "m"
        press(c, "right")
        assert c.value == "w"  # wrapped

    def test_left_cycles_back_with_wrap(self):
        c = Cycler(self.OPTS)
        press(c, "left")
        assert c.value == "m"

    def test_on_change_fires_with_new_value(self):
        seen = []
        c = Cycler(self.OPTS)
        c.on_change = seen.append
        press(c, "right", "left")
        assert seen == ["p", "w"]

    def test_enter_fires_on_submit_else_unconsumed(self):
        c = Cycler(self.OPTS)
        assert c.handle_key(make_key_event("enter")) is False
        got = []
        c.on_submit = got.append
        assert c.handle_key(make_key_event("enter")) is True
        assert got == ["w"]

    def test_other_keys_unconsumed(self):
        c = Cycler(self.OPTS)
        for name in ("j", "a", "tab", "backspace"):
            assert c.handle_key(make_key_event(name)) is False
        assert c.value == "w"

    def test_render_shows_chevroned_label(self):
        c = Cycler([("x", "the [label]")])
        out = c.render(40)
        assert strip_markup(out) == "‹ the [label] ›"

    def test_empty_options_safe(self):
        c = Cycler([])
        press(c, "right", "left", "space")
        assert c.value is None and c.label == ""


# ─── footer_markup ────────────────────────────────────────────────


SMALL_KEYS = {
    "spawn": ("c", "Spawn", True, False),
    "quit": ("q", "Quit", True, False),
    "quick_note": ("n", "", False, False),
    "help": ("question_mark", "?", True, False),
}


def test_footer_from_default_keys():
    out = footer_markup([], 500)
    assert "c [dim]spawn[/dim]" in out
    assert "r [dim]resume[/dim]" in out
    assert "? [dim]help[/dim]" in out  # symbol desc falls back to action name
    assert "/ [dim]search[/dim]" in out
    assert "^b [dim]›tab[/dim]" in out
    assert "enter [dim]open[/dim]" in out
    assert " [dim]·[/dim] " in out
    assert "quick_note" not in out  # show=False stays hidden by default


def test_footer_exact_output_with_patched_config(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_KEYS", SMALL_KEYS)
    monkeypatch.setattr(config, "load_config", dict)
    out = footer_markup([], 80)
    assert out == " c [dim]spawn[/dim] [dim]·[/dim] q [dim]quit[/dim] [dim]·[/dim] ? [dim]help[/dim]"


def test_footer_includes_explicit_extras(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_KEYS", SMALL_KEYS)
    monkeypatch.setattr(config, "load_config", dict)
    out = footer_markup(["quick_note"], 80)
    assert out.endswith("n [dim]quick_note[/dim]")  # no desc: action name


def test_footer_respects_user_key_overrides(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_KEYS", SMALL_KEYS)
    monkeypatch.setattr(config, "load_config", lambda: {"keybindings": {"spawn": "s,f9"}})
    out = footer_markup([], 80)
    assert "s [dim]spawn[/dim]" in out
    assert "c [dim]spawn[/dim]" not in out


def test_footer_drops_whole_hints_at_width(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_KEYS", SMALL_KEYS)
    monkeypatch.setattr(config, "load_config", dict)
    assert footer_markup([], 10) == " c [dim]spawn[/dim]"
    assert strip_markup(footer_markup([], 18)) == " c spawn · q quit"
    assert footer_markup([], 2) == ""


def test_footer_plain_length_never_exceeds_width():
    for width in (5, 20, 40, 80, 200):
        assert len(strip_markup(footer_markup([], width))) <= width


# ─── integration: widgets composed in a real view ─────────────────


@pytest.mark.asyncio
async def test_fuzzylist_inside_headless_view():
    from tui.app import App
    from tui.testing import Headless
    from tui.view import View

    class PickerView(View):
        def __init__(self):
            super().__init__()
            self.fuzzy = FuzzyList(ITEMS)
            self.fuzzy.on_select = self.dismiss
            self.fuzzy.on_cancel = lambda: self.dismiss(None)

        def on_key(self, ev):
            if self.fuzzy.handle_key(ev):
                self.request_paint()
                return True
            return False

        def render(self, frame, rect):
            frame.write_markup(0, 0, rect.w, self.fuzzy.input.render(rect.w))
            for i, line in enumerate(self.fuzzy.list.render(rect.w, rect.h - 2)):
                frame.write_markup(0, 1 + i, rect.w, line)
            frame.write_markup(0, rect.h - 1, rect.w, f"[dim]{self.fuzzy.status}[/dim]")
            frame.cursor = (self.fuzzy.input.cursor_col(rect.w), 0)

    app = App()
    async with Headless(app, size=(40, 8)) as h:
        h.app.push(PickerView())
        await h.pause()
        text = h.screen_text()
        assert "alpha" in text and "4 of 4" in text
        await h.feed_bytes(b"ga")  # real decoder path
        assert "1 of 4" in h.screen_text()
        assert "gamma" in h.screen_text() and "beta" not in h.screen_text()
        await h.feed_bytes(b"\r")  # enter selects → dismisses the only view
        assert await h._task == "g"
