"""SessionPickerView — port of screens.SessionPickerScreen (P4-B).

Pick which of a workstream's matching sessions to resume; dismisses
with the ClaudeSession (None on cancel). Liveness refreshes every 10s
in a worker thread — the "picker_liveness" exclusive group's generation
is captured per run and results are dropped when it moved on (stale
worker, the engine's exclusive=True semantics). Missing titles are
generated once in a background thread like the original's worker.
"""

from __future__ import annotations

import asyncio

from rendering import C_DIM, _is_session_seen, _render_session_option, _rich_escape
from threads import load_last_seen, session_activity

from ..widgets import BlockList
from .modals import ListModalView

_HINT = f"[{C_DIM}]Enter[/{C_DIM}] resume  [{C_DIM}]^H[/{C_DIM}] back"

LIVENESS_GROUP = "picker_liveness"
TITLES_GROUP = "picker_titles"


class SessionPickerView(ListModalView):
    box_size = (120, 34)
    list_cls = BlockList

    def __init__(self, ws, sessions: list) -> None:
        super().__init__(title=f"Resume: {_rich_escape(ws.name)}", hint=_HINT)
        self.ws = ws
        self.picker_sessions = list(sessions)
        self._last_seen: dict[str, str] = {}
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────

    def on_resize(self, rect) -> None:
        self._box = self._compute_box(rect)  # row width known pre-render

    def on_show(self) -> None:
        super().on_show()
        if not self._started:
            self._started = True
            self._last_seen = load_last_seen()
            self._rebuild_rows()
            self._generate_titles()
            self.set_interval(10, self._refresh_liveness)

    # ── rows ──────────────────────────────────────────────────────

    def _line_width(self) -> int:
        w = self.body_rect.w
        return w if w > 20 else 0

    def _rebuild_rows(self) -> None:
        lw = self._line_width()
        rows = []
        for s in self.picker_sessions:
            act = session_activity(s, self._last_seen)
            seen = _is_session_seen(s, self._last_seen)
            prompt = _render_session_option(
                s, act, 0, ws_repo_path=self.ws.repo_path,
                seen=seen, line_width=lw,
            )
            lines = str(prompt).split("\n")
            rows.append((s.session_id, lines[0], False))
            rows.extend(((s.session_id, j), line, True)
                        for j, line in enumerate(lines[1:], 1))
        self.list.set_rows(rows)
        self.request_paint()

    # ── selection ─────────────────────────────────────────────────

    def _dispatch_key(self, ev) -> bool:
        if ev.key == "l":  # not bound in the original — never selects
            return True
        return self.list.handle_key(ev)

    def _on_selected(self, item_id) -> None:
        for s in self.picker_sessions:
            if s.session_id == item_id:
                self.dismiss(s)
                return
        self.app.notify("No session selected", severity="error", timeout=2)

    # ── background refresh ────────────────────────────────────────

    def _refresh_liveness(self) -> None:
        app = self.app
        # exclusive() bumps the group generation to exactly this value
        # before the runner starts; a later run invalidates this one.
        app.exclusive(LIVENESS_GROUP,
                      self._liveness_runner(app.gen(LIVENESS_GROUP) + 1))

    async def _liveness_runner(self, g: int) -> None:
        from actions import refresh_liveness

        await asyncio.to_thread(refresh_liveness, self.picker_sessions)
        if self.app.gen(LIVENESS_GROUP) != g:
            return  # superseded while off-loop — drop the stale result
        self._rebuild_rows()

    def _generate_titles(self) -> None:
        async def runner():
            from thread_namer import get_session_title, title_sessions

            untitled = [s for s in self.picker_sessions
                        if not get_session_title(s)]
            if not untitled:
                return
            await asyncio.to_thread(title_sessions, untitled)
            self._rebuild_rows()

        self.app.exclusive(TITLES_GROUP, runner())
