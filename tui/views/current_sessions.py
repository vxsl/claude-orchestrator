"""CurrentSessionsView — port of screens.CurrentSessionsScreen (P4-C).

Cross-workstream view: all non-shelved, non-archived sessions active
today, grouped under workstream header rows (most-recent group first).
Lives on the permanent "Sessions" tab (index 1) — reached by tab
cycling from home, never pushed by a key of its own, same as app.py.

Reloads every 5s in a thread worker on the "current_sessions" exclusive
group; the generation is captured per run and stale results are dropped
(exclusive() can't cancel a body already inside to_thread). The 0.3s
throbber re-renders only THINKING session blocks in place via
update_row — id-keyed, so a structure change under the tick is detected
(update_row returns False) and falls back to a full rebuild; this is
the engine equivalent of the original's ids-match check before
replace_option_prompt_at_index (screens.py:3922).

enter/l/r resume via app.launch_claude_session (the embedded
ClaudeSessionView since P5); space archives; ctrl+space archives and
goes back; ':' palette; '?' the sessions help; esc/^H back.
"""

from __future__ import annotations

import asyncio

from rendering import (
    C_CYAN, C_DIM, C_YELLOW,
    _is_session_seen, _is_today, _render_session_option, _rich_escape,
)
from threads import ThreadActivity, load_last_seen, session_activity

from ..widgets import BlockList
from .help import HelpView
from .home import render_tab_bar
from .modals import ListModalView

RELOAD_GROUP = "current_sessions"

_TITLE = f"[bold {C_CYAN}]Sessions[/bold {C_CYAN}]  [{C_DIM}]today · active[/{C_DIM}]"
_HELP = "  ".join(
    f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in [
        ("↑↓/jk", "nav"), ("enter/l/r", "open"), ("spc", "archive"),
        ("^H/esc", "back"),
    ]
)


class CurrentSessionsView(ListModalView):
    fullscreen = True
    list_cls = BlockList

    def __init__(self, state, tabs) -> None:
        super().__init__(title="", hint=_HELP)  # header drawn in render_body
        self.state = state
        self.tabs = tabs
        self._sessions: list[tuple] = []  # (Workstream, ClaudeSession)
        self._session_ws_map: dict[str, object] = {}
        self._throbber_frame = 0
        self._last_seen_cache: dict[str, str] = {}
        self._started = False
        self._throbber = None
        self._throbber_paused = False

    # ── lifecycle ─────────────────────────────────────────────────

    def on_resize(self, rect) -> None:
        self._box = self._compute_box(rect)  # row width known pre-render

    def on_show(self) -> None:
        super().on_show()
        # Port of on_mount / on_screen_resume: fresh seen-state + sync load
        # so rows are correct the frame the tab appears.
        self._last_seen_cache = load_last_seen()
        self._apply(self._collect())
        if not self._started:
            self._started = True
            self.set_interval(5.0, self._schedule_reload)
            self._throbber = self.set_interval(0.3, self._tick_throbber)

    # ── data (port of _load_sessions) ─────────────────────────────

    def _collect(self) -> list[tuple]:
        results = []
        seen_sids: set[str] = set()
        for ws in self.state.store.active:
            sessions = self.state.sessions_for_ws(ws, include_archived_sessions=False)
            shelved = set(ws.shelved_sessions)
            for s in sessions:
                if s.session_id in shelved:
                    continue
                if not _is_today(s.last_activity or ""):
                    continue
                if s.session_id in seen_sids:
                    continue
                seen_sids.add(s.session_id)
                results.append((ws, s))
        results.sort(key=lambda x: x[1].last_activity or "", reverse=True)
        return results

    def _schedule_reload(self) -> None:
        app = self.app
        # exclusive() bumps the group generation to exactly this value
        # before the runner starts; a later run invalidates this one.
        app.exclusive(RELOAD_GROUP, self._reload_runner(app.gen(RELOAD_GROUP) + 1))

    async def _reload_runner(self, g: int) -> None:
        results = await asyncio.to_thread(self._collect)
        last_seen = await asyncio.to_thread(load_last_seen)
        if self.app.gen(RELOAD_GROUP) != g:
            return  # superseded while off-loop — drop the stale result
        self._last_seen_cache = last_seen
        self._apply(results)

    def _apply(self, results: list[tuple]) -> None:
        self._sessions = results
        self._session_ws_map = {s.session_id: ws for ws, s in results}
        self._build_rows()
        self._resume_throbber()

    # ── rows ──────────────────────────────────────────────────────

    def _line_width(self) -> int:
        w = self.body_rect.w - 2
        return w if w > 20 else 80  # pre-layout fallback, as the original

    def _session_lines(self, ws, s) -> list[str]:
        act = session_activity(s, self._last_seen_cache)
        seen = _is_session_seen(s, self._last_seen_cache)
        prompt = _render_session_option(
            s, act, self._throbber_frame,
            ws_repo_path=ws.repo_path or "", seen=seen,
            line_width=self._line_width(),
        )
        return str(prompt).split("\n")

    def _build_rows(self) -> None:
        lw = self._line_width()
        # Group by workstream, section order = most-recent-session first
        ws_groups: dict[str, list] = {}
        ws_order: list[str] = []
        ws_by_id: dict[str, object] = {}
        for ws, s in self._sessions:
            if ws.id not in ws_groups:
                ws_groups[ws.id] = []
                ws_order.append(ws.id)
                ws_by_id[ws.id] = ws
            ws_groups[ws.id].append(s)

        rows: list[tuple] = []
        for i_ws, ws_id in enumerate(ws_order):
            ws = ws_by_id[ws_id]
            icon = getattr(ws, "icon", "") or "◆"
            fill = max(2, lw - len(icon) - 1 - len(ws.name) - 1)
            if i_ws > 0:
                rows.append((f"__gap__{ws_id}", "", True))
            rows.append((
                f"__ws__{ws_id}",
                f"[{C_CYAN}]{icon} {_rich_escape(ws.name)} {'─' * fill}[/{C_CYAN}]",
                True,
            ))
            for s in ws_groups[ws_id]:
                lines = self._session_lines(ws, s)
                rows.append((s.session_id, lines[0], False))
                rows.extend(((s.session_id, j), line, True)
                            for j, line in enumerate(lines[1:], 1))
        self.list.set_rows(rows)  # keeps the highlight by row id
        self.request_paint()

    # ── throbber (THINKING rows only, in place) ───────────────────

    def _tick_throbber(self) -> None:
        app = self.app
        if app is not None and not app.ui_visible:
            return
        thinking = [
            (ws, s) for ws, s in self._sessions
            if session_activity(s, self._last_seen_cache) == ThreadActivity.THINKING
        ]
        if not thinking:
            if self._throbber is not None and not self._throbber_paused:
                self._throbber.pause()
                self._throbber_paused = True
            return
        self._throbber_frame += 1
        ok = True
        for ws, s in thinking:
            for j, line in enumerate(self._session_lines(ws, s)):
                row_id = s.session_id if j == 0 else (s.session_id, j)
                ok = self.list.update_row(row_id, line) and ok
        if not ok:  # row structure changed under us — full rebuild
            self._build_rows()
        self.request_paint()

    def _resume_throbber(self) -> None:
        if self._throbber is not None and self._throbber_paused:
            self._throbber.resume()
            self._throbber_paused = False

    # ── selection & keys ──────────────────────────────────────────

    def _selected(self):
        """(Workstream, session_id) for the highlighted block, or (None, None)."""
        hid = self.list.highlighted_id
        if hid is None:
            return None, None
        sid = BlockList.block_key(hid)
        if not isinstance(sid, str) or sid.startswith("__"):
            return None, None
        return self._session_ws_map.get(sid), sid

    def _on_selected(self, item_id) -> None:
        ws = self._session_ws_map.get(item_id)
        if ws:
            self.app.launch_claude_session(ws, session_id=item_id)

    def _dispatch_key(self, ev) -> bool:
        key = ev.key
        if key == "r":
            ws, sid = self._selected()
            if ws and sid:
                self.app.launch_claude_session(ws, session_id=sid)
            return True
        if key == "space":
            self._archive_selected()
            return True
        if key in ("ctrl+@", "ctrl+space") or ev.char == "\x00":
            ws, sid = self._selected()
            if ws and sid and sid not in ws.archived_sessions:
                from datetime import datetime, timezone
                ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
                self.state.store.update(ws)
            self.dismiss(None)
            return True
        if key == "colon":
            if hasattr(self.app, "open_command_palette"):
                self.app.open_command_palette()
            return True
        if key == "question_mark":
            self.app.push(HelpView(context="sessions"))
            return True
        return self.list.handle_key(ev)

    def _archive_selected(self) -> None:
        ws, sid = self._selected()
        if not ws or not sid:
            return
        if sid not in ws.archived_sessions:
            from datetime import datetime, timezone
            ws.archived_sessions[sid] = datetime.now(timezone.utc).isoformat()
            self.state.store.update(ws)
            self.state.invalidate_caches()
            self._apply(self._collect())

    # ── rendering ─────────────────────────────────────────────────

    def render_body(self, frame, body) -> None:
        self._write_line(frame, body.x, body.y, body.w,
                         render_tab_bar(self.state, self.tabs))
        self._write_line(frame, body.x, body.y + 1, body.w, _TITLE)
        list_h = max(1, body.h - 3)
        self.list.page_size = list_h
        if not self._sessions:
            self._write_line(frame, body.x, body.y + 3, body.w,
                             f"[{C_DIM}]No sessions active today[/{C_DIM}]")
            return
        for i, line in enumerate(self.list.render(body.w, list_h)):
            self._write_line(frame, body.x, body.y + 3 + i, body.w, line)
