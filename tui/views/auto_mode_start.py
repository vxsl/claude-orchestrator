"""AutoModeStartView — port of screens.AutoModeStartScreen (P4-B).

Multi-select over a workstream's backlog when auto mode starts:
space toggles ◉/○, a selects all, n none; enter dismisses with the
set of todo ids to RUN (the caller computes skip_ids = backlog −
returned set); escape / ctrl+h → None. Its caller — app
toggle_auto_mode, reached via ctrl+y on a Claude session screen —
lands with the ClaudeSessionScreen port in P5.
"""

from __future__ import annotations

from datetime import datetime

from rendering import (
    C_BLUE, C_DIM, C_FAINT, C_GOLD, C_GREEN, C_LIGHT, C_YELLOW, _rich_escape,
)

from .modals import ListModalView

_HELP = "  ".join(
    f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in [
        ("Space", "toggle"), ("a", "all"), ("n", "none"),
        ("Enter", "run"), ("Esc", "cancel"),
    ]
)


def _relative_time_short(iso_str: str) -> str:
    """Compact age string (e.g. '2h', '3d') — ported verbatim."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        delta = datetime.now().astimezone() - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s"
        m = s // 60
        if m < 60:
            return f"{m}m"
        h = m // 60
        if h < 24:
            return f"{h}h"
        d = h // 24
        if d < 30:
            return f"{d}d"
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "?"


class AutoModeStartView(ListModalView):
    box_size = (100, 32)
    border_color = C_BLUE

    def __init__(self, ws_name: str, todos: list) -> None:
        super().__init__(title="", hint=_HELP)
        self.ws_name = ws_name
        self._todos = list(todos)  # snapshot at construction
        self._selected: set[str] = set()  # default: none — opt-in
        self._rebuild()

    def _title_text(self) -> str:
        return (
            f"Auto mode: {_rich_escape(self.ws_name)}    "
            f"[{C_DIM}]{len(self._selected)}/{len(self._todos)} selected — "
            f"Space toggle, a all, n none, Enter run, Esc cancel[/{C_DIM}]"
        )

    def _row(self, t) -> str:
        check = (f"[{C_GREEN}]◉[/{C_GREEN}]" if t.id in self._selected
                 else f"[{C_DIM}]○[/{C_DIM}]")
        age = _relative_time_short(t.created_at)
        text = _rich_escape(t.text[:80])
        text_color = C_LIGHT if t.id in self._selected else C_DIM
        is_crystal = getattr(t, "origin", "manual") == "crystallized"
        tag_color = C_GOLD if is_crystal else C_BLUE
        tag = "c" if is_crystal else "m"
        return (f"{check} [{tag_color}]{tag}[/{tag_color}]  "
                f"[{text_color}]{text}[/{text_color}]    "
                f"[{C_FAINT}]{age}[/{C_FAINT}]")

    def _rebuild(self) -> None:
        self.title = self._title_text()
        self.list.set_rows([(t.id, self._row(t), False) for t in self._todos])
        self.request_paint()

    def _dispatch_key(self, ev) -> bool:
        key = ev.key
        if key == "space":
            tid = self.list.highlighted_id
            if tid is not None:
                self._selected.symmetric_difference_update({tid})
                self._rebuild()
            return True
        if key == "a":
            self._selected = {t.id for t in self._todos}
            self._rebuild()
            return True
        if key == "n":
            self._selected = set()
            self._rebuild()
            return True
        if key == "enter":
            self.dismiss(set(self._selected))
            return True
        if key == "l":  # not bound in the original — never selects
            return True
        return self.list.handle_key(ev)
