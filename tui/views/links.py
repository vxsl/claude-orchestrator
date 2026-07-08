"""LinksView — port of screens.LinksScreen (P4-B).

List of a workstream's links; enter opens the highlighted one via
actions.open_link and the modal stays open (as the original).
escape / ctrl+h go back with None.
"""

from __future__ import annotations

from actions import open_link
from rendering import C_DIM, _link_icon, _rich_escape

from .modals import ListModalView

_HINT = f"[{C_DIM}]Enter[/{C_DIM}] open  [{C_DIM}]^H[/{C_DIM}] back"


class LinksView(ListModalView):
    def __init__(self, ws, store) -> None:
        super().__init__(title=f"Links: {_rich_escape(ws.name)}", hint=_HINT)
        self.ws = ws
        self.store = store
        rows = [
            (i, f"{_link_icon(lnk.kind)}  {_rich_escape(f'[{lnk.kind}]')} "
                f"{_rich_escape(lnk.value)}", False)
            for i, lnk in enumerate(ws.links)
        ]
        if not rows:
            rows = [("none", "(no links)", True)]
        self.list.set_rows(rows)
        self.box_size = (80, min(20, len(rows)) + 8)

    def _dispatch_key(self, ev) -> bool:
        if ev.key == "l":  # not bound in the original — never opens
            return True
        return self.list.handle_key(ev)

    def _on_selected(self, item_id) -> None:  # enter: open, stay open
        if isinstance(item_id, int) and item_id < len(self.ws.links):
            link = self.ws.links[item_id]
            open_link(link, ws=self.ws, app=self.app)
            self.app.notify(f"Opening {link.label}...", timeout=2)
