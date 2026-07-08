"""ConfirmView — port of screens.ConfirmScreen (P4-A).

y → dismiss(True); n / escape / ctrl+h / backspace → dismiss(False).
The message is Rich markup and may span multiple lines (the box sizes
itself to fit).
"""

from __future__ import annotations

from rendering import C_DIM, C_RED

from ..widgets import strip_markup
from .modals import ModalView

_HINT = f"[{C_DIM}]y[/{C_DIM}] yes  [{C_DIM}]n[/{C_DIM}] no  [{C_DIM}]^H[/{C_DIM}] back"


class ConfirmView(ModalView):
    cancel_result = False
    border_color = C_RED  # ConfirmScreen's `border: round $error 40%`

    def __init__(self, message: str) -> None:
        super().__init__(hint=_HINT)
        self.message = message
        self._lines = message.split("\n")
        width = max([50] + [len(strip_markup(l)) + 6 for l in self._lines])
        self.box_size = (width, len(self._lines) + 6)

    def _dispatch_key(self, ev) -> bool:
        if ev.key == "y":
            self.dismiss(True)
            return True
        if ev.key == "n":
            self.dismiss(False)
            return True
        return False

    def render_body(self, frame, body) -> None:
        for i, line in enumerate(self._lines[: body.h]):
            self._write_centered(frame, body.x, body.y + i, body.w, line)
