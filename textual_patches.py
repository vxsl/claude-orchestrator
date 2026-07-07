"""Targeted monkey-patches for Textual performance problems.

Call ``apply()`` once before the App is instantiated (done at app.py import).
Every patch here must fall back to stock Textual behavior on any surprise,
so a future Textual upgrade degrades to "slower but correct".
"""

from textual.geometry import Region
from textual.widgets import OptionList

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    _applied = True
    _patch_option_list_replace_prompt()


def _patch_option_list_replace_prompt() -> None:
    """Stop single-prompt replacement from re-rendering the whole list.

    OptionList._replace_option_prompt (Textual <= 8.2.8) ends with
    _clear_caches(), which drops the rendered strips and line layout of
    EVERY option. Our throbber timers replace one spinner glyph several
    times a second, so with any session THINKING the entire visible list
    was re-visualized, re-styled, and re-rendered per tick — profiled at
    ~15-30% of a core in steady state.

    The override evicts only the replaced option's cached strips when the
    row's height is unchanged (always true for spinner-frame swaps). If the
    height changed, layout isn't built yet, or Textual's private internals
    have drifted, it falls back to the stock full-clear path.
    """
    stock = OptionList._replace_option_prompt

    def _replace_option_prompt(self, index, prompt) -> None:
        try:
            option = self.get_option_at_index(index)
            old_height = self._line_cache.heights.get(index)
            region_width = self.scrollable_content_region.width
            if old_height is None or not region_width:
                stock(self, index, prompt)
                return
            option._set_prompt(prompt)  # also drops option._visual
            padding = self.get_component_styles("option-list--option").padding
            width = region_width - self._get_left_gutter_width()
            new_height = (
                self._get_visual(option).get_height(self.styles, width - padding.width)
                + option._divider
            )
            if new_height != old_height:
                self._clear_caches()
                return
            cache = self._option_render_cache  # keyed by (option, style, padding)
            for key in [k for k in cache.keys() if k[0] is option]:
                cache.discard(key)
            # Repaint only the replaced row (refresh() regions are in content
            # coordinates: gutter is added by _set_dirty, scrolling is applied
            # in render_line). A row scrolled out of view needs no repaint at
            # all — its strips are evicted and rebuild on demand.
            line = self._line_cache.index_to_line.get(index)
            if line is None:
                self.refresh()
                return
            y = line - self.scroll_offset.y
            viewport = self.scrollable_content_region
            if y + old_height <= 0 or y >= viewport.height:
                return
            self.refresh(Region(0, y, viewport.width, old_height))
        except Exception:
            stock(self, index, prompt)

    OptionList._replace_option_prompt = _replace_option_prompt
