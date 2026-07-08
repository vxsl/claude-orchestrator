"""TrashView — port of screens.TrashScreen (P4-B).

Fullscreen list of soft-deleted sessions grouped under dim workstream
header rows. u restores; D purges after a ConfirmView (deviation: the
Textual original purged without confirming — permanent deletion earns
a confirm here); ':' opens the command palette; '?' the trash help;
escape / ctrl+h go back.
"""

from __future__ import annotations

from rendering import C_DIM, C_RED, C_YELLOW, _render_session_option, _rich_escape
from threads import ThreadActivity

from ..widgets import BlockList
from .confirm import ConfirmView
from .help import HelpView
from .modals import ListModalView

_HELP = "  ".join(
    f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in [
        ("↑↓/jk", "nav"), ("u", "restore"), ("D", "purge forever"),
        ("^H/esc", "back"),
    ]
)


class TrashView(ListModalView):
    fullscreen = True
    list_cls = BlockList

    def __init__(self, state) -> None:
        super().__init__(
            title=f"Trash  [{C_DIM}]soft-deleted sessions[/{C_DIM}]",
            hint=_HELP,
        )
        self.state = state
        self._entries: list[tuple] = []  # (Workstream, ClaudeSession)
        self._ws_map: dict[str, object] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    def on_resize(self, rect) -> None:
        self._box = self._compute_box(rect)  # row width known pre-render

    def on_show(self) -> None:
        super().on_show()
        self._load()

    # ── data (port of TrashScreen._load/_build_list) ──────────────

    def _load(self) -> None:
        all_sessions = {s.session_id: s for s in self.state.sessions}
        entries = []
        seen_sids: set[str] = set()
        for ws in self.state.store.workstreams:
            for sid, deleted_at in ws.deleted_sessions.items():
                if sid in seen_sids:
                    continue
                s = all_sessions.get(sid)
                if s:
                    seen_sids.add(sid)
                    entries.append((ws, s, deleted_at))
        entries.sort(key=lambda x: x[2] or "", reverse=True)
        self._entries = [(ws, s) for ws, s, _ in entries]
        self._ws_map = {s.session_id: ws for ws, s in self._entries}
        self._build_rows()

    def _build_rows(self) -> None:
        lw = self.body_rect.w - 2
        if lw <= 20:
            lw = 80  # pre-layout fallback, as the original
        ws_groups: dict[str, list] = {}
        ws_order: list[str] = []
        ws_by_id: dict[str, object] = {}
        for ws, s in self._entries:
            if ws.id not in ws_groups:
                ws_groups[ws.id] = []
                ws_order.append(ws.id)
                ws_by_id[ws.id] = ws
            ws_groups[ws.id].append(s)
        rows = []
        for ws_id in ws_order:
            ws = ws_by_id[ws_id]
            icon = getattr(ws, "icon", "") or "◆"
            rows.append((f"__ws__{ws_id}",
                         f"[{C_DIM}]{icon} {_rich_escape(ws.name)}[/{C_DIM}]",
                         True))
            for s in ws_groups[ws_id]:
                prompt = _render_session_option(
                    s, ThreadActivity.IDLE, 0,
                    ws_repo_path=ws.repo_path or "", seen=True, line_width=lw,
                )
                lines = str(prompt).split("\n")
                rows.append((s.session_id, lines[0], False))
                rows.extend(((s.session_id, j), line, True)
                            for j, line in enumerate(lines[1:], 1))
        self.list.set_rows(rows)
        self.request_paint()

    def _selected(self):
        hid = self.list.highlighted_id
        if hid is None:
            return None, None
        sid = BlockList.block_key(hid)
        if not isinstance(sid, str) or sid.startswith("__"):
            return None, None
        return self._ws_map.get(sid), sid

    # ── keys ──────────────────────────────────────────────────────

    def _dispatch_key(self, ev) -> bool:
        key = ev.key
        if key == "u":
            self._restore()
            return True
        if key == "D":
            self._purge()
            return True
        if key == "colon":
            if hasattr(self.app, "open_command_palette"):
                self.app.open_command_palette()
            return True
        if key == "question_mark":
            self.app.push(HelpView(context="trash"))
            return True
        if key in ("enter", "l"):  # nothing bound in the original
            return True
        return self.list.handle_key(ev)

    def _restore(self) -> None:
        ws, sid = self._selected()
        if not ws or not sid:
            return
        ws.deleted_sessions.pop(sid, None)
        self.state.store.update(ws)
        self.app.notify("Restored", timeout=1)
        self._load()

    def _purge(self) -> None:
        ws, sid = self._selected()
        if not ws or not sid:
            return

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            ws.deleted_sessions.pop(sid, None)
            self.state.store.update(ws)
            self.app.notify("Purged", timeout=1)
            self._load()

        self.app.push(
            ConfirmView(f"[bold {C_RED}]Purge[/bold {C_RED}] this session "
                        f"forever?\n[{C_DIM}]No recovery after this.[/{C_DIM}]"),
            on_result=on_confirm,
        )

    # ── rendering ─────────────────────────────────────────────────

    def render_body(self, frame, body) -> None:
        if not self._entries:
            self._write_line(frame, body.x, body.y, body.w,
                             f"[{C_DIM}]Trash is empty[/{C_DIM}]")
            return
        super().render_body(frame, body)
