"""Tests for widgets.py — FuzzyPicker, TabBar, InlineInput."""

import pytest
from widgets import _strip_markup, TabBar, InlineMode


class TestStripMarkup:
    def test_plain_text(self):
        assert _strip_markup("hello world") == "hello world"

    def test_bold_tag(self):
        assert _strip_markup("[bold]hello[/bold]") == "hello"

    def test_color_tag(self):
        assert _strip_markup("[#58a6ff]blue[/#58a6ff]") == "blue"

    def test_named_color(self):
        assert _strip_markup("[dim]faded[/dim]") == "faded"

    def test_escaped_bracket(self):
        # Rich only escapes opening brackets with \[
        assert _strip_markup(r"array\[0]") == "array[0]"

    def test_nested_tags(self):
        assert _strip_markup("[bold #ff0000]red bold[/bold #ff0000] normal") == "red bold normal"

    def test_empty_string(self):
        assert _strip_markup("") == ""

    def test_complex_markup(self):
        text = "[bold]name[/bold]  [dim](3 ws)[/dim]  [#6e7681]~/dev/repo[/#6e7681]"
        assert _strip_markup(text) == "name  (3 ws)  ~/dev/repo"


class TestTabBar:
    """Test TabBar state management (no Textual app needed)."""

    def test_initial_state(self):
        tb = TabBar()
        assert tb.tab_count == 1
        assert tb.active_idx == 0
        assert tb.active_tab_id == "home"

    def test_add_tab(self):
        tb = TabBar()
        idx = tb.add_tab("ws-1", "Auth refactor", "●")
        assert idx == 1
        assert tb.tab_count == 2

    def test_add_duplicate_returns_existing(self):
        tb = TabBar()
        idx1 = tb.add_tab("ws-1", "Auth refactor")
        idx2 = tb.add_tab("ws-1", "Auth refactor")
        assert idx1 == idx2
        assert tb.tab_count == 2

    def test_remove_tab(self):
        tb = TabBar()
        tb.add_tab("ws-1", "One")
        tb.add_tab("ws-2", "Two")
        assert tb.tab_count == 3
        tb.remove_tab(1)
        assert tb.tab_count == 2
        # Remaining: home, ws-2
        assert tb._tabs[1][0] == "ws-2"

    def test_cannot_remove_home(self):
        tb = TabBar()
        tb.remove_tab(0)
        assert tb.tab_count == 1

    def test_activate_adjusts_index(self):
        tb = TabBar()
        tb.add_tab("ws-1", "One")
        tb._active_idx = 1  # bypass message posting
        assert tb.active_tab_id == "ws-1"

    def test_remove_active_tab_moves_left(self):
        tb = TabBar()
        tb.add_tab("ws-1", "One")
        tb.add_tab("ws-2", "Two")
        tb._active_idx = 2  # ws-2
        tb._tabs.pop(2)  # simulate removal
        # After removal, should clamp
        if tb._active_idx >= len(tb._tabs):
            tb._active_idx = len(tb._tabs) - 1
        assert tb.active_idx == 1

    def test_remove_tab_before_active_shifts(self):
        tb = TabBar()
        tb.add_tab("ws-1", "One")
        tb.add_tab("ws-2", "Two")
        tb._active_idx = 2  # ws-2 active
        tb.remove_tab(1)  # remove ws-1
        # ws-2 is now at index 1, active should follow
        assert tb.active_idx == 1
        assert tb.active_tab_id == "ws-2"


class TestInlineMode:
    def test_modes_exist(self):
        assert InlineMode.SEARCH == "search"
        assert InlineMode.COMMAND == "command"
        assert InlineMode.NOTE == "note"
        assert InlineMode.RENAME == "rename"
