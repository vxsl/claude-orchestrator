"""AddLinkView — port of screens.AddLinkScreen (P4-B).

Kind Cycler + value LineEdit with a live kind-description line (updates
as the Cycler cycles, like the original's Select.Changed handler).
enter / ctrl+s → dismiss(Link); an empty value keeps the form open with
a toast. Focus starts on the value field, as in the original.
"""

from __future__ import annotations

from models import Link
from rendering import C_DIM, LINK_KINDS, _rich_escape

from ..widgets import Cycler, LineEdit
from .modals import FormModalView

_HINT = f"[{C_DIM}]Enter[/{C_DIM}] add  [{C_DIM}]^H[/{C_DIM}] back"

LINK_DESCRIPTIONS = {
    "worktree": "Git worktree path (e.g. ~/dev/project)",
    "ticket": "Jira/GitHub ticket ID (e.g. UB-1234)",
    "url": "Web URL",
    "file": "Local directory or file path",
    "claude-session": "Claude session ID",
    "slack": "Slack channel or thread URL",
}


class AddLinkView(FormModalView):
    box_size = (70, 16)

    def __init__(self, ws_name: str) -> None:
        super().__init__(title=f"Add Link: {_rich_escape(ws_name)}", hint=_HINT)
        self.ws_name = ws_name
        self.kind = self.add_field(
            "Kind", Cycler([(k, k) for k in LINK_KINDS], value="url")
        )
        self.value = self.add_field("Value", LineEdit())
        self.ring.focus(self.value)

    def _on_submit(self):
        value = self.value.text.strip()
        if not value:
            self.app.notify("Value cannot be empty", severity="error", timeout=2)
            return None
        kind = self.kind.value
        return Link(kind=kind, label=kind, value=value)

    def render_body(self, frame, body) -> None:
        super().render_body(frame, body)
        desc = LINK_DESCRIPTIONS.get(self.kind.value, "")
        self._write_line(frame, body.x, body.bottom - 1, body.w,
                         f"[{C_DIM}]{desc}[/{C_DIM}]")
