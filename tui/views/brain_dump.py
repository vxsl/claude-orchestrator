"""BrainDumpView — port of screens.BrainDumpScreen (P4-B).

Stream-of-consciousness TextEdit. ctrl+s → dismiss(stripped text);
empty text keeps the modal open with a toast (as the original).
escape / ctrl+h → dismiss(None). The parse → preview → add/launch
chain lives in HomeView._do_brain.
"""

from __future__ import annotations

from rendering import C_DIM

from ..layout import Rect
from ..widgets import TextEdit
from .modals import FormModalView

_HINT = f"[{C_DIM}]Ctrl+S[/{C_DIM}] submit  [{C_DIM}]^H[/{C_DIM}] back"
_DESC = (
    f"[{C_DIM}]Type your stream of consciousness. Commas, newlines, "
    f"'also'/'and then' split into tasks.[/{C_DIM}]"
)


class BrainDumpView(FormModalView):
    box_size = (80, 24)
    textedit_height = 12

    def __init__(self) -> None:
        super().__init__(title="Brain Dump", hint=_HINT)
        self.area = self.add_field("", TextEdit(""))

    def _on_submit(self):
        text = "\n".join(self.area.lines).strip()
        if not text:
            self.app.notify("Nothing to parse", severity="warning", timeout=2)
            return None
        return text

    def render_body(self, frame, body) -> None:
        self._write_line(frame, body.x, body.y, body.w, _DESC)
        super().render_body(
            frame, Rect(body.x, body.y + 2, body.w, max(0, body.h - 2))
        )
