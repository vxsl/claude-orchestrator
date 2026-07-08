"""TodoEditView — port of screens._TodoEditScreen (P4-A).

One LineEdit prefilled with the todo text. enter → dismiss(stripped
text, or None when emptied — same as the original); escape / ctrl+h →
dismiss(None).
"""

from __future__ import annotations

from rendering import C_DIM

from ..widgets import LineEdit
from .modals import FormModalView

_HINT = f"[{C_DIM}]Enter[/{C_DIM}] save  [{C_DIM}]^H[/{C_DIM}] back"


class TodoEditView(FormModalView):
    box_size = (70, 9)

    def __init__(self, initial_text: str) -> None:
        super().__init__(title="Edit Todo", hint=_HINT)
        self.input = self.add_field("", LineEdit(initial_text))

    def _submit(self) -> None:
        # Unlike the base (None = stay open), the original dismisses
        # None when the text was emptied — the caller treats it as cancel.
        self.dismiss(self.input.text.strip() or None)
