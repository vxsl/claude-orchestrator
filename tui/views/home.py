"""HomeView — the orchestrator home screen on the tui engine (P3 port).

Ports app.py's home surface: tab/status/filter/summary bars as markup
lines, the workstream list + preview-sessions list (rows built with the
same rendering.py builders), '/' search, 1/2/3 filters, f1–f5 sorts,
'p' preview toggle, the self-pausing 0.3s throbber, and the working
'c' spawn / 'r' resume actions. P4-A wired the first modals: 'C'
repo-spawn (RepoPickerView → WorkstreamPickerView → launch), 'd'
delete + 't' trust-toggle via ConfirmView, and 'u' archive (no
confirm, as in the Textual app). The remaining modals (detail, add,
notes, palette, help, tabs) are stub toasts until P4-B/C.

Not in this phase (see MIGRATION.md): the two embedded tig panes that
filled the lower half of the Textual home (P5/P6) — the lists own the
whole body for now — and cross-workstream session *content* search
(the home '/' filters workstream names/descriptions only).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

import config
from actions import launch_orch_claude, do_resume
from rendering import (
    C_BLUE, C_DIM, C_FAINT, C_GREEN, C_MID, C_RED, C_YELLOW,
    BG_BASE, BG_RAISED,
    _activity_icon, _any_session_today, _best_activity, _category_markup,
    _is_session_seen, _is_today, _render_session_option, _render_ws_option,
    _rich_escape, _token_color_markup,
)
from threads import ThreadActivity, session_activity

from ..keys import KeyEvent
from ..layout import Rect, split_cols, split_rows
from ..view import View
from ..widgets import SEP_ID as _SEP_ID
from ..widgets import BlockList, LineEdit, ListView, footer_markup
from .add import AddView
from .add_link import AddLinkView
from .brain_dump import BrainDumpView
from .brain_preview import BrainPreviewView
from .confirm import ConfirmView
from .pickers import SENTINEL_NEW, RepoPickerView, WorkstreamPickerView
from .quick_note import QuickNoteView

STUB_DETAIL = "Detail view lands in P4 — ORCH_ENGINE=textual for full UI"
STUB_TABS = "Tabs land in P4 — ORCH_ENGINE=textual for full UI"


def _markup_lines(content: Any) -> list[str]:
    """Split a rendering.py builder result (markup str or rich Text) into
    per-line markup strings (list rows are single lines in this engine)."""
    if isinstance(content, Text):
        return [line.markup for line in content.split("\n", allow_blank=True)]
    return str(content).split("\n")


def _divider_line(width: int = 40) -> str:
    """Port of app.py's _divider_option markup."""
    pad = max(1, (width - 10) // 2)
    return f"[{C_FAINT}]{'─' * pad} earlier {'─' * pad}[/{C_FAINT}]"


class HomeView(View):
    def __init__(self, state, tabs) -> None:
        super().__init__()
        self.state = state
        self.tabs = tabs
        self.ws_list = BlockList()
        self.ws_list.on_highlight = lambda _id: self._on_ws_highlight()
        self.preview_list = BlockList()
        self.search = LineEdit()
        self.search.on_change = self._on_search_changed
        self.search.on_cancel = self._close_search
        self.search.on_submit = lambda _text: self._close_search(keep=True)
        self.search_active = False
        self._preview_ws_id: str | None = None
        self._preview_label = f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]"
        self._ws_count = 0  # blocks (workstreams) currently listed
        self._throbber = None
        self._throbber_paused = False
        self._loaded = False
        self._rect: Rect | None = None
        self._keymap = self._build_keymap()
        self._handlers = self._build_handlers()

    # ── lifecycle ─────────────────────────────────────────────────

    def on_show(self) -> None:
        super().on_show()
        if self._throbber is None:
            self._throbber = self.set_interval(0.3, self._tick_throbber)
        if not self._loaded:
            self._loaded = True
            self.refresh_rows()
        app = self.app
        if app is not None and hasattr(app, "ensure_background_started"):
            app.ensure_background_started()

    def on_resize(self, rect) -> None:
        old_width = self._rect.w if self._rect else -1
        self._rect = rect
        if self._loaded and rect.w != old_width:
            self.refresh_rows()  # ws row layout depends on line width

    def on_data_changed(self) -> None:
        """Sessions/liveness/git/tmux data changed (called by OrchApp)."""
        self._preview_ws_id = None  # force preview rebuild
        self.refresh_rows()
        self._resume_throbber()
        self.request_paint()

    # ── keys ──────────────────────────────────────────────────────

    def _build_keymap(self) -> dict[str, str]:
        keymap: dict[str, str] = {}
        for action in config.DEFAULT_KEYS:
            for key in config.get_key(action).split(","):
                key = key.strip()
                if key:
                    keymap.setdefault(key, action)
        return keymap

    def _build_handlers(self) -> dict[str, Any]:
        def stub(msg):
            return lambda: self._toast(msg)

        return {
            "cursor_down": lambda: self._nav("j"),
            "cursor_up": lambda: self._nav("k"),
            "cursor_top": lambda: self._nav("g"),
            "cursor_bottom": lambda: self._nav("G"),
            "half_page_down": lambda: self._nav("ctrl+d"),
            "half_page_up": lambda: self._nav("ctrl+u"),
            "select_item": stub(STUB_DETAIL),
            "next_tab": stub(STUB_TABS),
            "prev_tab": stub(STUB_TABS),
            "close_tab": stub(STUB_TABS),
            "add": self._action_add,
            "brain_dump": self._action_brain_dump,
            "spawn": self._action_spawn,
            "repo_spawn": self._action_repo_spawn,
            "resume": self._action_resume,
            "link_action": self._action_add_link,
            "quick_note": self._action_quick_note,
            "edit_notes": stub("Todo editor lands in P4"),
            "rename": stub("Rename lands in P4"),
            "open_links": stub("Links land in P4"),
            "toggle_archive": self._action_toggle_archive,
            "delete_item": self._action_delete,
            "toggle_trust": self._action_toggle_trust,
            "filter('all')": lambda: self._set_filter("all"),
            "filter('stale')": lambda: self._set_filter("stale"),
            "filter('archived')": lambda: self._set_filter("archived"),
            "search": self._open_search,
            "sort('activity')": lambda: self._set_sort("activity"),
            "sort('updated')": lambda: self._set_sort("updated"),
            "sort('created')": lambda: self._set_sort("created"),
            "sort('category')": lambda: self._set_sort("category"),
            "sort('name')": lambda: self._set_sort("name"),
            "ship": stub("Dev-workflow actions land in P4"),
            "ticket": stub("Dev-workflow actions land in P4"),
            "branches": stub("Dev-workflow actions land in P4"),
            "rr": stub("Dev-workflow actions land in P4"),
            "command_palette": stub("Command palette lands in P4"),
            "toggle_preview": self._toggle_preview,
            "refresh": self._action_refresh,
            "help": stub("Help screen lands in P4 — keys match the Textual engine"),
            "quit": self._action_quit,
        }

    def on_key(self, ev) -> bool:
        if self.search_active:
            if ev.key in ("tab", "shift+tab"):
                self._toast("Session content search lands in P4")
            else:
                self.search.handle_key(ev)
            self.request_paint()
            return True  # search input swallows everything while open
        action = self._keymap.get(ev.key)
        if action is None:
            return False
        handler = self._handlers.get(action)
        if handler is None:
            return False
        handler()
        self.request_paint()
        return True

    def _nav(self, canonical: str) -> None:
        self.ws_list.handle_key(KeyEvent(canonical, None))

    # ── actions ───────────────────────────────────────────────────

    def _toast(self, msg: str) -> None:
        if self.app is not None:
            self.app.notify(msg)

    def _selected_ws(self):
        key = self.ws_list.highlighted_id
        if not isinstance(key, str) or key == _SEP_ID:
            return None
        if self.state.filter_mode == "archived":
            return self.state.get_archived(key) or self.state.get_ws(key)
        return self.state.get_ws(key)

    def _set_filter(self, mode: str) -> None:
        self.state.set_filter(mode)
        self.refresh_rows()

    def _set_sort(self, mode: str) -> None:
        self.state.set_sort(mode)
        self.refresh_rows()

    def _toggle_preview(self) -> None:
        self.state.preview_visible = not self.state.preview_visible

    def _action_quit(self) -> None:
        if self.app is not None:
            self.app.exit()

    def _action_refresh(self) -> None:
        self.state.store.load()
        self.refresh_rows()
        if self.app is not None and hasattr(self.app, "kick_pollers"):
            self.app.kick_pollers()
        self._toast("Refreshed")

    def _action_spawn(self) -> None:
        ws = self._selected_ws()
        if not ws:
            self._toast("No workstream selected")
            return
        ok, err = launch_orch_claude(ws, self.state.store)
        if ok:
            self._toast(f"Spawned Claude for {ws.name} (new tmux window)")
        else:
            self._toast(f"Spawn failed: {err}")

    def _action_resume(self) -> None:
        ws = self._selected_ws()
        if not ws:
            self._toast("No workstream selected")
            return
        # pick_session=None → multi-match resumes the most recent session
        # (SessionPickerScreen port lands in P4).
        do_resume(
            ws, self.app, self.state.sessions,
            sessions_for_ws_fn=self.state.sessions_for_ws,
            pick_session=None,
        )

    def _action_repo_spawn(self) -> None:
        """Port of app.action_repo_spawn: repo picker → (workstream
        picker) → launch, auto-creating a workstream when none exists."""
        repos = self.state.discover_all_repos()
        ws_counts: dict[str, int] = {}
        for repo in repos:
            n = len(self.state.workstreams_for_repo(repo))
            if n > 0:
                ws_counts[repo] = n

        def on_repo_picked(repo_path: str | None) -> None:
            if not repo_path:
                return
            matches = self.state.workstreams_for_repo(repo_path)
            if len(matches) == 0:
                self._spawn_in_ws(self.state.create_ws_for_repo(repo_path))
            elif len(matches) == 1:
                self._spawn_in_ws(matches[0])
            else:
                def on_ws_picked(result) -> None:
                    if result is None:
                        return
                    if result == SENTINEL_NEW:
                        self._spawn_in_ws(self.state.create_ws_for_repo(repo_path))
                    else:
                        self._spawn_in_ws(result)

                self.app.push(WorkstreamPickerView(matches, repo_path),
                              on_result=on_ws_picked)

        self.app.push(RepoPickerView(repos, ws_counts), on_result=on_repo_picked)

    def _spawn_in_ws(self, ws) -> None:
        self.refresh_rows()  # a just-created workstream should be listed
        self.app.launch_claude_session(ws)

    def _action_add(self) -> None:
        """Port of app.action_add: AddView → store.add."""
        def on_result(ws) -> None:
            if ws:
                self.state.store.add(ws)
                self._toast(f"Created: {ws.name}")
            self.refresh_rows()

        self.app.push(AddView(), on_result=on_result)

    def _action_quick_note(self) -> None:
        """Port of app.action_quick_note: QuickNoteView → add_todo."""
        ws = self._selected_ws()
        if not ws:
            return

        def on_note(text) -> None:
            if not text or not text.strip():
                return
            self.state.add_todo(ws.id, text)
            self.refresh_rows()
            self._toast("Todo added")

        self.app.push(QuickNoteView(ws), on_result=on_note)

    def _action_add_link(self) -> None:
        """Port of app._add_link_to_ws: AddLinkView → state.add_link."""
        ws = self._selected_ws()
        if not ws:
            self._toast("No workstream selected")
            return

        def on_link(link) -> None:
            if link:
                self.state.add_link(ws.id, link)
                self.refresh_rows()
                self._toast(f"Added {link.kind} link to {ws.name}")

        self.app.push(AddLinkView(ws.name), on_result=on_link)

    def _action_brain_dump(self) -> None:
        """Port of app.action_brain_dump → _do_brain chain."""
        def on_text(text) -> None:
            if text is None:
                return
            self._do_brain(text)

        self.app.push(BrainDumpView(), on_result=on_text)

    def _do_brain(self, text: str) -> None:
        from brain import parse_brain_dump
        from models import Workstream

        tasks = parse_brain_dump(text)
        if not tasks:
            self._toast("No tasks found in input")
            return

        def on_result(mode: str) -> None:
            if not mode:
                return
            created = []
            for task in tasks:
                ws = Workstream(name=task.name, description=task.raw_text,
                                category=task.category)
                self.state.store.add(ws)
                created.append(ws)
            self.refresh_rows()
            if mode == "launch" and created:
                # app.py opens the Detail view here; Detail is P4-C, so
                # launch directly (suspend-attach fallback).
                self._toast(f"Added {len(created)} workstreams — launching session...")
                self.app.launch_claude_session(created[0])
            else:
                self._toast(f"Added {len(created)} workstreams")

        self.app.push(BrainPreviewView(tasks), on_result=on_result)

    def _action_toggle_archive(self) -> None:
        """Port of app.action_toggle_archive (no confirm, same as Textual)."""
        ws = self._selected_ws()
        if not ws:
            return
        if self.state.filter_mode == "archived":
            name = self.state.unarchive(ws.id)
            if name:
                self._toast(f"Restored: {name}")
                self.refresh_rows()
        else:
            name = self.state.archive(ws.id)
            if name:
                self._toast(f"Archived: {name}")
                self.refresh_rows()

    def _action_delete(self) -> None:
        ws = self._selected_ws()
        if not ws:
            return

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.state.delete(ws.id)
                self._toast(f"Deleted: {ws.name}")
                self.refresh_rows()

        self.app.push(
            ConfirmView(
                f"[bold {C_RED}]Delete[/bold {C_RED}] "
                f"[bold]{_rich_escape(ws.name)}[/bold]?"
            ),
            on_result=on_confirm,
        )

    def _action_toggle_trust(self) -> None:
        """Port of app.action_toggle_trust: confirm, then toggle the
        trusted-projects entry for the workstream's cwd."""
        import trust
        from pathlib import Path

        ws = self._selected_ws()
        if not ws:
            return
        cwd = ws.repo_path
        if not cwd:
            self._toast(f"No cwd set for {ws.name} — link a worktree or repo first")
            return
        try:
            norm = str(Path(cwd).expanduser().resolve())
        except Exception:
            self._toast(f"Invalid cwd: {cwd}")
            return

        if trust.is_trusted(norm):
            msg = (
                f"[bold]Untrust[/bold] [bold {C_YELLOW}]{_rich_escape(norm)}[/bold {C_YELLOW}]?\n"
                f"[{C_DIM}]New sessions will require permission prompts.[/{C_DIM}]"
            )
        else:
            msg = (
                f"[bold {C_YELLOW}]⚠ Trust[/bold {C_YELLOW}] [bold]{_rich_escape(norm)}[/bold]?\n"
                f"[{C_DIM}]All sessions launched in this tree will skip[/{C_DIM}]\n"
                f"[{C_DIM}]all permission prompts (--dangerously-skip-permissions).[/{C_DIM}]"
            )

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            if trust.toggle(norm):
                self._toast(f"Trusted: {norm}")
            else:
                self._toast(f"Untrusted: {norm}")
            self.refresh_rows()

        self.app.push(ConfirmView(msg), on_result=on_confirm)

    # ── search ────────────────────────────────────────────────────

    def _open_search(self) -> None:
        self.search_active = True
        self.search.text = ""
        self.search.cursor = 0
        self.state.search_mode = "ws"  # content search is a P4 gap
        self.state.search_text = ""
        self.refresh_rows()

    def _on_search_changed(self, text: str) -> None:
        self.state.set_search(text.strip())
        self.refresh_rows()

    def _close_search(self, keep: bool = False) -> None:
        self.search_active = False
        if not keep:
            self.state.search_text = ""
        self.refresh_rows()

    # ── workstream rows ───────────────────────────────────────────

    def _ws_line_width(self) -> int:
        """Usable row width inside the workstream pane (port of
        _olist_line_width: 0 when too narrow → builders use fallback)."""
        if self._rect is None:
            return 0
        body = self._layout(self._rect)["ws"]
        w = body.w - 2
        return w if w > 20 else 0

    def refresh_rows(self) -> None:
        items = self.state.get_unified_items()
        last_seen = self.state.get_last_seen()
        lw = self._ws_line_width()
        import trust as _trust

        rows: list[tuple[Any, str, bool]] = []
        sep_inserted = False
        for ws in items:
            ws_sessions = self.state.sessions_for_ws(ws)
            git_st = self.state.git_status_cache.get(ws.repo_path) if ws.repo_path else None
            trusted = _trust.is_trusted(ws.repo_path) if ws.repo_path else False
            is_today = (
                _any_session_today(ws_sessions) if ws_sessions else _is_today(ws.updated_at)
            )
            if not sep_inserted and rows and not is_today:
                rows.append((_SEP_ID, _divider_line(lw or 40), True))
                sep_inserted = True
            prompt = _render_ws_option(
                ws, ws_sessions, last_seen,
                tmux_check=self.state.ws_has_tmux,
                line_width=lw,
                git_status=git_st,
                trusted=trusted,
            )
            for i, line in enumerate(_markup_lines(prompt)):
                if i == 0:
                    rows.append((ws.id, line, False))
                else:
                    rows.append(((ws.id, i), line, True))
        self._ws_count = len(items)
        self.ws_list.set_rows(rows)
        self._refresh_preview(force=True)
        self.request_paint()

    def _on_ws_highlight(self) -> None:
        self._refresh_preview()
        self.request_paint()

    # ── preview sessions ──────────────────────────────────────────

    def _refresh_preview(self, force: bool = False) -> None:
        ws = self._selected_ws()
        ws_id = ws.id if ws else None
        if not force and ws_id == self._preview_ws_id:
            return
        self._preview_ws_id = ws_id
        if not ws:
            self._preview_label = f"[bold {C_BLUE}]Sessions[/bold {C_BLUE}]"
            self.state.preview_sessions = []
            self.preview_list.set_rows([])
            return
        ws_sessions = self.state.sessions_for_ws(ws)
        archived_count = len(ws.archived_sessions)
        archived_suffix = (
            f"  [{C_DIM}]({archived_count} archived)[/{C_DIM}]" if archived_count else ""
        )
        self._preview_label = (
            f"[bold {C_BLUE}]{_rich_escape(ws.name)}[/bold {C_BLUE}]"
            f"  {_category_markup(ws.category)}"
            f"{archived_suffix}"
        )
        self.state.preview_sessions = ws_sessions
        self.state.last_seen_cache = self.state.get_last_seen()
        if ws_sessions:
            self._resume_throbber()
        self._rebuild_preview_rows()

    def _session_lines(self, s) -> list[str]:
        last_seen = self.state.last_seen_cache
        act = session_activity(s, last_seen)
        seen = _is_session_seen(s, last_seen)
        prompt = _render_session_option(
            s, act, self.state.throbber_frame, title_width=35, seen=seen
        )
        return _markup_lines(prompt)

    def _rebuild_preview_rows(self) -> None:
        rows: list[tuple[Any, str, bool]] = []
        sep_inserted = False
        for i, s in enumerate(self.state.preview_sessions):
            if not sep_inserted and i > 0 and not _is_today(s.last_activity or s.started_at or ""):
                rows.append((_SEP_ID, _divider_line(38), True))
                sep_inserted = True
            for j, line in enumerate(self._session_lines(s)):
                if j == 0:
                    rows.append((s.session_id, line, False))
                else:
                    rows.append(((s.session_id, j), line, True))
            rows.append(((s.session_id, "gap"), "", True))
        self.preview_list.set_rows(rows)

    # ── throbber ──────────────────────────────────────────────────

    def _tick_throbber(self) -> None:
        app = self.app
        if app is not None and not app.ui_visible:
            return
        last_seen = self.state.last_seen_cache
        thinking = [
            s for s in self.state.preview_sessions
            if session_activity(s, last_seen) == ThreadActivity.THINKING
        ]
        if not thinking:
            # Nothing to animate — pause until a THINKING session appears.
            if self._throbber is not None and not self._throbber_paused:
                self._throbber.pause()
                self._throbber_paused = True
            return
        self.state.throbber_frame += 1
        ok = True
        for s in thinking:
            ok = self._update_session_rows(s) and ok
        if not ok:  # row structure changed under us — full rebuild
            self._rebuild_preview_rows()
        self.request_paint()

    def _update_session_rows(self, s) -> bool:
        """Per-row in-place update for one session block (the 1-row throbber
        path). Returns False if the block shape no longer matches."""
        for j, line in enumerate(self._session_lines(s)):
            row_id = s.session_id if j == 0 else (s.session_id, j)
            if not self.preview_list.update_row(row_id, line):
                return False
        return True

    def _resume_throbber(self) -> None:
        if self._throbber is not None and self._throbber_paused:
            self._throbber.resume()
            self._throbber_paused = False

    # ── bars (ports of app.py's renderers) ────────────────────────

    def _tab_activity(self, ws_id: str | None):
        """(activity, icon_markup) for a workstream tab; '' markup when idle."""
        if not ws_id:
            return ThreadActivity.IDLE, ""
        ws = self.state.get_ws(ws_id)
        if not ws:
            return ThreadActivity.IDLE, ""
        sessions = self.state.sessions_for_ws(ws)
        if not sessions:
            return ThreadActivity.IDLE, ""
        last_seen = self.state.last_seen_cache
        act = _best_activity(sessions, last_seen)
        if act == ThreadActivity.IDLE:
            return act, ""
        unseen = any(
            not _is_session_seen(s, last_seen)
            for s in sessions
            if session_activity(s, last_seen) == act
        )
        return act, _activity_icon(act, self.state.throbber_frame, seen=not unseen)

    def _render_tab_bar(self) -> str:
        parts = []
        for i, tab in enumerate(self.tabs.tabs):
            prefix = f"{tab.icon} " if tab.icon else ""
            is_permanent = tab.ws_id is None
            is_active = i == self.tabs.active_idx
            ws_label = tab.label
            if not is_permanent and len(ws_label) > 20:
                ws_label = ws_label[:20] + "…"
            act_prefix = ""
            if not is_permanent:
                _, act_icon = self._tab_activity(tab.ws_id)
                if act_icon:
                    act_prefix = f"{act_icon} "
            if is_active and is_permanent:
                content = f"[bold italic {C_MID} on {BG_BASE}] {prefix}{_rich_escape(ws_label)} [/]"
            elif is_active:
                content = (
                    f"[on {BG_BASE}] {act_prefix}[/on {BG_BASE}]"
                    f"[bold {C_BLUE} on {BG_BASE}]{prefix}{_rich_escape(ws_label)} [/]"
                )
            elif is_permanent:
                content = f"[italic {C_FAINT} on {BG_RAISED}] {prefix}{_rich_escape(ws_label)} [/]"
            else:
                content = (
                    f"[on {BG_RAISED}] {act_prefix}[/on {BG_RAISED}]"
                    f"[{C_DIM} on {BG_RAISED}]{prefix}{_rich_escape(ws_label)} [/{C_DIM} on {BG_RAISED}]"
                )
            parts.append(content)
            if i < len(self.tabs.tabs) - 1:
                parts.append(f"[{C_FAINT}]│[/{C_FAINT}]")
        return "".join(parts)

    def _render_status_bar(self) -> tuple[str, str]:
        SEP = f"  [{C_FAINT}]·[/{C_FAINT}]  "
        ws = self.state.store.active
        stale = len(self.state.store.stale())
        line1_parts = [
            f"[bold {C_BLUE}] ORCH [/bold {C_BLUE}]",
            f"[bold]{len(ws)}[/bold] streams",
        ]
        if stale:
            line1_parts.append(f"[{C_DIM}]{stale} stale[/{C_DIM}]")
        line1 = SEP.join(line1_parts)

        sessions = self.state.sessions
        archived_sids = {
            sid for w in self.state.store.active for sid in w.archived_sessions
        }
        last_seen = self.state.last_seen_cache
        thinking = 0
        your_turn = 0
        total_tokens = 0
        _your_turn_acts = (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY)
        for s in sessions:
            act = session_activity(s, last_seen)
            if act == ThreadActivity.THINKING:
                thinking += 1
            elif act in _your_turn_acts and s.session_id not in archived_sids:
                your_turn += 1
            total_tokens += s.total_input_tokens + s.total_output_tokens
        line2_parts = [f"[{C_DIM}] [/{C_DIM}][bold]{len(sessions)}[/bold] sessions"]
        if thinking:
            line2_parts.append(f"[{C_BLUE}]{thinking} thinking[/{C_BLUE}]")
        if your_turn:
            line2_parts.append(f"[{C_GREEN}]{your_turn} your turn[/{C_GREEN}]")
        if sessions and total_tokens > 0:
            _tk = (
                f"{total_tokens / 1_000_000:.1f}M" if total_tokens > 1_000_000
                else f"{total_tokens / 1_000:.0f}k" if total_tokens > 1_000
                else str(total_tokens)
            )
            line2_parts.append(_token_color_markup(_tk, total_tokens))
        return line1, SEP.join(line2_parts)

    def _render_filter_bar(self) -> str:
        filters = [("all", "All"), ("stale", "Stale"), ("archived", "Archived")]
        SEP = f" [{C_FAINT}]·[/{C_FAINT}] "
        preset_parts = []
        for i, (key, label) in enumerate(filters):
            n = i + 1
            if self.state.filter_mode == key:
                preset_parts.append(f"[bold {C_MID} on #0d1f35]{n}:{label}[/bold {C_MID} on #0d1f35]")
            else:
                preset_parts.append(f"[{C_FAINT}]{n}:{label}[/{C_FAINT}]")
        line = f" {SEP.join(preset_parts)}"
        if self.state.search_text:
            line += (
                f"  [{C_DIM}]·[/{C_DIM}]  [{C_DIM}]search:[/{C_DIM}] "
                f"[{C_YELLOW}]{_rich_escape(self.state.search_text)}[/{C_YELLOW}]"
            )
        return line

    def _render_summary_bar(self) -> str:
        count = self._ws_count
        if self.state.filter_mode == "archived":
            return (
                f"  {count} archived  "
                f"[{C_DIM}]│[/{C_DIM}]  "
                f"[{C_YELLOW}]1[/{C_YELLOW}] back to all"
            )
        solving = sum(
            1 for ws in self.state.store.active
            if getattr(ws, "ticket_solve_status", "").lower() in ("running", "active")
        )
        solve_part = f"  [{C_YELLOW}]{solving} solving[/{C_YELLOW}]" if solving else ""
        return (
            f"  {count} workstreams{solve_part}  "
            f"[{C_DIM}]│[/{C_DIM}]  "
            f"[{C_YELLOW}]r[/{C_YELLOW}] resume  "
            f"[{C_YELLOW}]c[/{C_YELLOW}] new session  "
            f"[{C_YELLOW}]/[/{C_YELLOW}] search  "
            f"[{C_YELLOW}]q[/{C_YELLOW}] quit"
        )

    # ── render ────────────────────────────────────────────────────

    def _layout(self, rect: Rect) -> dict[str, Rect]:
        top, filt, body, summary, footer = split_rows(rect, 3, 1, 1.0, 1, 1)
        if self.state.preview_visible:
            ws_rect, pv_rect = split_cols(body, 2.0, 1.0)
        else:
            ws_rect, pv_rect = body, None
        return {"top": top, "filter": filt, "ws": ws_rect, "preview": pv_rect,
                "summary": summary, "footer": footer}

    def render(self, frame, rect) -> None:
        self._rect = rect
        lay = self._layout(rect)
        top = lay["top"]
        frame.write_markup(top.x, top.y, top.w, self._render_tab_bar())
        line1, line2 = self._render_status_bar()
        if top.h > 1:
            frame.write_markup(top.x, top.y + 1, top.w, line1)
        if top.h > 2:
            frame.write_markup(top.x, top.y + 2, top.w, line2)

        filt = lay["filter"]
        if filt.h > 0:
            frame.write_markup(filt.x, filt.y, filt.w, self._render_filter_bar())

        self._render_list_pane(
            frame, lay["ws"], self.ws_list,
            f"[bold {C_BLUE}]Workstreams[/bold {C_BLUE}]",
        )
        pv = lay["preview"]
        if pv is not None and pv.w > 0:
            if self.state.preview_sessions:
                self._render_list_pane(frame, pv, self.preview_list, self._preview_label)
            else:
                frame.write_markup(pv.x + 1, pv.y, pv.w - 1, self._preview_label)
                if self._selected_ws() is not None and pv.h > 1:
                    frame.write_markup(
                        pv.x + 1, pv.y + 2, pv.w - 1, f"[{C_DIM}]No sessions[/{C_DIM}]"
                    )

        summary = lay["summary"]
        if summary.h > 0:
            frame.write_markup(summary.x, summary.y, summary.w, self._render_summary_bar())

        footer = lay["footer"]
        if footer.h > 0:
            self._render_footer(frame, footer)

    def _render_list_pane(self, frame, rect: Rect, lst: ListView, label: str) -> None:
        frame.write_markup(rect.x + 1, rect.y, rect.w - 1, label)
        list_h = rect.h - 1
        lst.page_size = max(1, list_h)
        for i, line in enumerate(lst.render(rect.w - 2, list_h)):
            frame.write_markup(rect.x + 1, rect.y + 1 + i, rect.w - 2, line)

    def _render_footer(self, frame, rect: Rect) -> None:
        toast = getattr(self.app, "toast_text", "") if self.app is not None else ""
        if self.search_active:
            input_w = max(1, rect.w - 3)
            frame.write_markup(
                rect.x, rect.y, rect.w,
                f" [bold {C_YELLOW}]/[/bold {C_YELLOW}]{self.search.render(input_w)}",
            )
            frame.cursor = (rect.x + 2 + self.search.cursor_col(input_w), rect.y)
        elif toast:
            frame.write_markup(
                rect.x, rect.y, rect.w,
                f" [{C_YELLOW}]{_rich_escape(toast)}[/{C_YELLOW}]",
            )
        else:
            frame.write_markup(rect.x, rect.y, rect.w, footer_markup(width=rect.w))
