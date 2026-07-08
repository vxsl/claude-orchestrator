"""Todo views — ports of screens.TodoScreen / _TodoContextScreen (P4-B).

TodoContextView edits a todo's context notes. Save-on-close like the
original (^H/escape *saves*): every exit path dismisses with the
stripped editor text; the caller persists it via state.edit_todo.
"""

from __future__ import annotations

from rendering import C_DIM, _rich_escape

from ..widgets import TextEdit
from .modals import FormModalView

_CTX_HINT = f"[{C_DIM}]^H[/{C_DIM}] save & back"


class TodoContextView(FormModalView):
    box_size = (80, 24)
    textedit_height = 15

    def __init__(self, item) -> None:
        label = item.text[:40] if item else "?"
        super().__init__(title=f"Context: {_rich_escape(label)}", hint=_CTX_HINT)
        self.item = item
        self.area = self.add_field("", TextEdit(item.context if item else ""))

    def _text(self) -> str:
        return "\n".join(self.area.lines).strip()

    def _submit(self) -> None:
        self.dismiss(self._text())

    def _cancel(self) -> None:  # escape/^H save too — original semantics
        self.dismiss(self._text())
