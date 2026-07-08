"""QuickNoteView — port of screens.QuickNoteScreen (P4-B).

Multiline todo note for a workstream. ctrl+s → dismiss(stripped text,
or None when empty — the caller treats it as cancel, same as the
original); escape / ctrl+h → dismiss(None).
"""

from __future__ import annotations

from rendering import C_DIM, _rich_escape

from ..widgets import TextEdit
from .modals import FormModalView

_HINT = f"[{C_DIM}]^S[/{C_DIM}] save  [{C_DIM}]Esc[/{C_DIM}] cancel"


class QuickNoteView(FormModalView):
    box_size = (72, 16)
    textedit_height = 7

    def __init__(self, ws) -> None:
        super().__init__(title=f"Todo: {_rich_escape(ws.name)}", hint=_HINT)
        self.ws = ws
        self.area = self.add_field("", TextEdit(""))

    def _submit(self) -> None:
        self.dismiss("\n".join(self.area.lines).strip() or None)
