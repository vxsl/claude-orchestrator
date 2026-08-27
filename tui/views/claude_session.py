"""ClaudeSessionView — port of claude_session_screen.ClaudeSessionScreen (P5).

Full-screen Claude session: tab bar, live stats header over the embedded
claude TerminalPane (tmux-persistent on the orch-sessions socket), a
36-col sidebar (tig status / tig log / sibling-session list) and a
1-line footer. Everything typed lands on the claude PTY as the exact
bytes the outer terminal sent (`ev.raw` passthrough); only the
_PASSTHROUGH_KEYS action set is intercepted.

Wiring follows the termpane contract (tui/termpane.py docstring):
panes register with the app's 20fps ticker on_show and unregister
on_hide, and — rule that must never break — every PTY child is
stopped/detached while the loop still runs, before the view is torn
down or the app exits (a live child deadlocks asyncio's executor
shutdown). Detach keeps claude alive in tmux; tig panes get stop().

Deviations from the Textual original (see MIGRATION.md):
- Zooming a sidebar panel (ctrl+z) gives it the full screen width; the
  original kept the fixed 36-col sidebar width with the main column
  hidden.
- While the ctrl+r jump overlay is open every key goes to the picker
  (the original let priority bindings like ctrl+e through).
- Keys the focused sidebar panel doesn't handle fall through to the
  app's tab keys only (the original bubbled to all app-level bindings).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from config import get_key, get_session_key, key_label, key_set
from models import _relative_time
from rendering import (
    BG_CHROME, BG_RAISED, BG_SURFACE,
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_GREEN, C_MID, C_ORANGE,
    C_PURPLE, C_YELLOW,
    CATEGORY_THEME, THROBBER_FRAMES,
    _activity_icon, _context_bar_compact, _is_session_seen, _rich_escape,
    _session_title,
)
from session_launch import (
    ORCH_DIR,
    auto_link_session, build_claude_command, build_session_context,
    build_session_env, claude_jsonl_path, log_session_exit,
)
from sessions import extract_user_prompts, parse_session
from state import git_panes_enabled
from threads import ThreadActivity, session_activity

from ..layout import Rect, split_cols, split_rows
from ..termpane import TerminalPane
from ..view import View
from ..widgets import FuzzyList, strip_markup
from .home import render_tab_bar
from .modals import draw_border

# Auto mode's key lives in config.py (SESSION_KEYS) so both engines and a
# user config.toml override agree on it. ctrl+y is no longer bound — it now
# reaches claude as a plain yank.
_AUTO_MODE_KEYS = get_session_key("toggle_auto_mode")
_AUTO_MODE_LABEL = key_label(_AUTO_MODE_KEYS)
# Not a SESSION_KEYS entry: toggle_git_panes is an app-level action (home
# binds it too), it just also has to reach us past the PTY.
_GIT_PANES_KEYS = get_key("toggle_git_panes")
_GIT_PANES_LABEL = key_label(_GIT_PANES_KEYS)

# Keys that pass through the claude/tig panes to the view for panel
# navigation and app-level tab keys (claude_session_screen.py:44).
_PASSTHROUGH_KEYS = {
    "ctrl+j", "ctrl+k", "ctrl+shift+j", "ctrl+shift+k", "ctrl+e", "ctrl+h",
    "ctrl+z", "ctrl+backslash", "ctrl+b", "ctrl+x", "ctrl+@",
    "ctrl+r",
} | key_set(_AUTO_MODE_KEYS) | key_set(_GIT_PANES_KEYS)

_SIDEBAR_W = 36
_SESSIONS_MAX_H = 12  # other-sessions wrap max-height (incl. border)
_SESSIONS_MAX_AGE_H = 1  # quick-switch only lists sessions active this recently


# ── markup helpers (ported from claude_session_screen.py) ─────────────

def _esc(text: str) -> str:
    """Escape Rich markup characters."""
    return text.replace("[", "\\[").replace("]", "\\]")


@lru_cache(maxsize=512)
def _padded_line(markup: str, w: int, bg: str) -> str:
    """Background-padded row markup, cached: header/footer/sessions rows
    repaint at 20fps while a pane streams, and rebuilding the pad (a
    strip_markup regex pass per line) dominated the chrome cost."""
    pad = " " * max(0, w - len(strip_markup(markup)))
    return f"[on {bg}]{markup}{pad}[/on {bg}]"


def _parse_tokens(tokens_str: str) -> float:
    try:
        if tokens_str.endswith("M"):
            return float(tokens_str[:-1]) * 1_000_000
        elif tokens_str.endswith("k"):
            return float(tokens_str[:-1]) * 1_000
        elif tokens_str != "—":
            return float(tokens_str)
    except ValueError:
        pass
    return 0


def _tool_bar_markup(tc: dict[str, int], width: int = 8) -> str:
    """Build a Rich-markup tool usage bar."""
    cats = [("mutate", C_ORANGE), ("bash", C_MID), ("read", C_DIM), ("agent", C_PURPLE)]
    total = sum(tc.values())
    if total == 0:
        return f"[{C_FAINT}]{'─' * width}[/]"
    parts = []
    used = 0
    for cat, color in cats:
        n = tc.get(cat, 0)
        if n == 0:
            continue
        chars = max(1, round(n / total * width))
        chars = min(chars, width - used)
        if chars > 0:
            parts.append(f"[{color}]{'▬' * chars}[/]")
            used += chars
    if used < width:
        parts.append(f"[{C_FAINT}]{'─' * (width - used)}[/]")
    return "".join(parts)


def _file_list_markup(files: list[str]) -> str:
    if not files:
        return ""
    if len(files) <= 4:
        return f"[{C_DIM}]{' '.join(files)}[/]"
    shown = " ".join(files[:3])
    return f"[{C_DIM}]{shown} +{len(files) - 3}[/]"


def _iso_ts(s: str) -> float:
    """Parse ISO timestamp to a unix-ts float, or 0 on failure (sort keys)."""
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _recent_cutoff(hours: float = 2) -> float:
    return (datetime.now().astimezone() - timedelta(hours=hours)).timestamp()


# ── ctrl+r jump-to-message picker items ───────────────────────────────

def _search_snippet(text: str, max_len: int = 30) -> str:
    """Pick a short, distinctive substring from a user message for tmux
    search. First non-empty line trimmed to ``max_len`` chars (long
    snippets may not match: Claude's TUI wraps at the pane width)."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:max_len]
    return text.strip()[:max_len]


def _build_picker_items(
    jsonl_path: str, row_width: int = 72
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return (option_items, id→snippet) for the message picker.

    Items are chronological (oldest first, newest last). Each label is
    laid out as: message snippet ← padded → dim relative time right-edge.
    """
    prompts = extract_user_prompts(jsonl_path)
    items: list[tuple[str, str]] = []
    snippets: dict[str, str] = {}
    for idx, msg in enumerate(prompts):
        item_id = f"msg-{idx}"
        snippets[item_id] = _search_snippet(msg.text)
        plain = msg.text.strip().splitlines()[0] if msg.text.strip() else "(empty)"
        age = _relative_time(msg.timestamp) if msg.timestamp else ""
        if age == "unknown":
            age = ""
        budget = max(8, row_width - len(age) - 2)
        if len(plain) > budget:
            plain = plain[: budget - 1] + "…"
        pad = max(2, row_width - len(plain) - len(age))
        msg_md = _rich_escape(plain)
        if age:
            label = f"{msg_md}{' ' * pad}[{C_DIM}]{age}[/{C_DIM}]"
        else:
            label = msg_md
        items.append((item_id, label))
    return items, snippets


# ── live stats header (port of SessionHeaderWidget) ───────────────────

class SessionHeader:
    """Holds the parsed-JSONL cache and builds the header markup lines.
    `refresh_blocking()` is the 5s worker body — it runs on a thread
    (exclusive group, generation-checked) and returns the new lines."""

    def __init__(self, ws_name: str, ws_status: str, ws_category: str,
                 session_id: str, jsonl_path: str, initial_title: str = "") -> None:
        self._ws_name = ws_name
        self._ws_status = ws_status
        self._ws_category = ws_category
        self._session_id = session_id
        self._jsonl_path = jsonl_path
        self._initial_title = initial_title
        self._start_time = time.time()
        self._cached_session = None
        self._last_jsonl_size = 0
        self.width = 80  # content width, set by the view on render
        self._sc = C_DIM
        self._cc = C_DIM
        for k, v in CATEGORY_THEME.items():
            if k and k.value == ws_category:
                self._cc = v
                break
        self.lines: list[str] = self._static_lines()

    def _format_elapsed(self) -> str:
        secs = int(time.time() - self._start_time)
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m{secs % 60:02d}s"
        else:
            return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"

    def _static_lines(self) -> list[str]:
        """Initial lines before any JSONL data is available."""
        elapsed = self._format_elapsed()
        sid_short = self._session_id[:8]
        title = self._initial_title or self._ws_name
        return [
            f"[bold]{_esc(title)}[/bold]  [{C_DIM}]{elapsed}[/]  [{C_FAINT}]{sid_short}[/]",
            f"[{C_BLUE}]ORCH[/]  [{C_PURPLE}]{_esc(self._ws_name)}[/]  "
            f"[{self._sc}]{self._ws_status}[/]  [{self._cc}]{self._ws_category}[/]",
            f"[{C_FAINT}]{'─' * 8}[/]",
        ]

    def refresh_blocking(self) -> list[str]:
        """Parse the JSONL and rebuild the header lines (thread worker).

        First call does a full parse; subsequent calls only read the tail
        of the file (last 8KB) for incremental updates. Full re-parse is
        triggered every 30s to keep token/message counts accurate.
        """
        import thread_namer

        elapsed = self._format_elapsed()
        sid_short = self._session_id[:8]

        title = ""
        model = ""
        msgs = 0
        asst_msgs = 0
        tokens_str = "—"
        work_time = ""
        age = ""
        files: list[str] = []
        tool_counts: dict[str, int] = {}
        context_tokens = 0
        context_window_size = 200_000
        last_msg = ""
        last_user_messages: list[str] = []

        jp = Path(self._jsonl_path)
        if jp.exists():
            try:
                cur_size = jp.stat().st_size
                need_full = (
                    self._cached_session is None
                    or cur_size < self._last_jsonl_size  # file was truncated
                    or (int(time.time()) % 30 < 5)  # full re-parse every ~30s
                )

                if need_full:
                    s = parse_session(jp)
                    if s:
                        self._cached_session = s
                        self._last_jsonl_size = cur_size
                else:
                    from sessions import refresh_session_tail
                    s = self._cached_session
                    if s:
                        refresh_session_tail(s)
                        self._last_jsonl_size = cur_size

                if s:
                    model = s.model_short
                    msgs = s.message_count
                    asst_msgs = s.assistant_message_count
                    tokens_str = s.tokens_display
                    work_time = s.work_time_display
                    age = s.age
                    files = s.files_mutated or []
                    tool_counts = s.tool_counts or {}
                    context_tokens = s.context_tokens
                    context_window_size = s.context_window_size
                    last_msg = s.last_user_message_text or s.last_message_text or ""
                    last_user_messages = s.last_user_messages or ([] if not last_msg else [last_msg])
                    title = thread_namer.get_session_title(s) or ""
            except Exception:
                pass

        if not title:
            title = self._initial_title or self._ws_name

        r1_parts = []
        if model and model != "—":
            r1_parts.append(f"[{C_CYAN}]{model}[/]")
        r1_parts.append(f"[{C_DIM}]{elapsed}[/]")
        r1_parts.append(f"[{C_FAINT}]{sid_short}[/]")
        line1 = f"[bold]{_esc(title)}[/bold]  {'  '.join(r1_parts)}"

        l2 = (f"[{C_BLUE}]ORCH[/]  [{C_PURPLE}]{_esc(self._ws_name)}[/]  "
              f"[{self._sc}]{self._ws_status}[/]  [{self._cc}]{self._ws_category}[/]")
        if msgs > 0:
            r2_parts = [f"[{C_DIM}]{msgs}↑{asst_msgs}↓[/]"]
            tok_val = _parse_tokens(tokens_str)
            tc = C_ORANGE if tok_val >= 500_000 else C_YELLOW if tok_val >= 100_000 else C_DIM
            r2_parts.append(f"[{tc}]{tokens_str}[/]")
            if work_time:
                r2_parts.append(f"[{C_DIM}][italic]{work_time} think[/italic][/]")
            if age:
                r2_parts.append(f"[{C_MID}]{age}[/]")
            l2 += f"  [{C_DIM}]│[/]  " + "  ".join(r2_parts)

        ctx_bar = _context_bar_compact(context_tokens, context_window_size)
        bar = _tool_bar_markup(tool_counts)
        flist = _file_list_markup(files)
        l3 = "  ".join(p for p in (ctx_bar, bar) if p)
        if flist:
            l3 += f"  {flist}"

        all_lines = [line1, l2, l3]
        if last_user_messages:
            # most-recent-first in the data; display oldest→newest (bottom =
            # most recent), colors dimming from oldest to newest
            msg_colors = ["#7a5218", "#b07a25", C_YELLOW]
            prefix = "you said: "
            w = max(20, self.width)
            shown = list(reversed(last_user_messages[:3]))  # oldest first
            color_offset = 3 - len(shown)
            for i, msg in enumerate(shown):
                color = msg_colors[color_offset + i]
                clean = msg.replace("\n", " ").strip()
                available = max(1, w - len(prefix))
                if len(clean) > available:
                    text = clean[:available - 1] + "…"
                else:
                    text = clean
                is_last = i == len(shown) - 1
                pad = " " * max(0, available - len(text) + (0 if is_last else 1))
                style = "bold italic" if is_last else "italic"
                all_lines.append(
                    f"[{C_DIM} on black]{_esc(prefix)}[/{C_DIM} on black]"
                    f"[{style} {color} on black]{_esc(text)}{pad}[/{style} {color} on black]"
                )
        return all_lines


# ── sibling-sessions sidebar list (port of WsSessionListWidget) ───────

class WsSessionList:
    """Sidebar list of non-idle sessions in the workstream (excluding the
    one being viewed). j/k to move, Enter fires `on_select(sid)`."""

    def __init__(self, ws_id: str, current_session_id: str) -> None:
        self._ws_id = ws_id
        self._current_session_id = current_session_id
        # rows: (sid, title, activity, age, seen, last_asst, is_current)
        self.rows: list[tuple] = []
        self.selected_sid: str | None = None
        self.throbber_frame = 0
        self.on_select = None          # callable(sid)
        self.on_items_changed = None   # callable(has_items)
        self._last_had_items = False
        self._rev = 0                  # bumped on any visible change
        self._lines_cache: tuple | None = None  # (key, lines)

    def refresh(self, state) -> bool:
        """Recompute rows from live session data; returns True on change.
        Runs on the loop every 0.5s — sessions_for_ws is cached, so this
        is ~free unless the daemon just invalidated."""
        try:
            ws = state.store.get(self._ws_id) if self._ws_id else None
            sessions = state.sessions_for_ws(ws) if ws else []
        except Exception:
            sessions = []

        try:
            from threads import load_last_seen
            last_seen = load_last_seen()
        except Exception:
            last_seen = {}

        # Three inclusion paths, in priority order:
        #   1. "Bright icon" rows — THINKING always, plus unseen
        #      AWAITING_INPUT/RESPONSE_READY.
        #   2. The current session (so the user can see where they are).
        #   3. Recently viewed non-archived sessions, even if seen.
        # Paths 1 and 3 are both gated on the session having been active
        # within _SESSIONS_MAX_AGE_H; only the current session is exempt.
        order = {
            ThreadActivity.THINKING: 0,
            ThreadActivity.AWAITING_INPUT: 1,
            ThreadActivity.RESPONSE_READY: 2,
        }
        cutoff = _recent_cutoff(hours=_SESSIONS_MAX_AGE_H)
        candidates = []
        for s in sessions:
            is_current = s.session_id == self._current_session_id
            act = session_activity(s, last_seen)
            seen = _is_session_seen(s, last_seen)
            seen_ts = last_seen.get(s.session_id, "")
            recently_viewed = bool(seen_ts) and _iso_ts(seen_ts) >= cutoff
            gone_cold = _iso_ts(s.last_activity or s.started_at) < cutoff

            if is_current:
                if act not in order:
                    act = ThreadActivity.AWAITING_INPUT  # placeholder
                bucket = order.get(act, 1)
            elif gone_cold:
                # Nothing older than the cutoff is a quick-switch target,
                # however bright its icon would otherwise be.
                continue
            elif act == ThreadActivity.THINKING:
                bucket = 0
            elif act in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY) and not seen:
                bucket = order[act]
            elif recently_viewed and act != ThreadActivity.IDLE:
                bucket = 9  # below all active rows
            else:
                continue

            candidates.append((bucket, -_iso_ts(s.last_activity or s.started_at), s, act, seen))
        candidates.sort(key=lambda x: (x[0], x[1]))
        candidates = candidates[:8]

        new_rows = []
        for _, _, s, act, seen in candidates:
            title = _session_title(s)
            last_asst = s.last_assistant_message_text or ""
            is_current = s.session_id == self._current_session_id
            new_rows.append((s.session_id, title, act, s.age, seen, last_asst, is_current))

        changed = new_rows != self.rows
        self.rows = new_rows
        if changed:
            self._rev += 1

        # Maintain selection across refreshes
        sids = [r[0] for r in self.rows]
        if self.selected_sid not in sids:
            self.selected_sid = sids[0] if sids else None

        has_items = bool(self.rows)
        if has_items != self._last_had_items:
            self._last_had_items = has_items
            if self.on_items_changed is not None:
                self.on_items_changed(has_items)
        return changed

    def tick_throbber(self) -> bool:
        """Advance the throbber when anything is THINKING; True = repaint."""
        if any(r[2] == ThreadActivity.THINKING for r in self.rows):
            self.throbber_frame = (self.throbber_frame + 1) % len(THROBBER_FRAMES)
            return True
        return False

    def render_lines(self, width: int, focused: bool) -> list[str]:
        """Markup lines, two per session (port of _repaint). Cached until
        anything visible changes — this renders at 20fps while streaming."""
        if not self.rows:
            return [f"[{C_FAINT}]no other active sessions[/{C_FAINT}]"]
        key = (width, focused, self._rev, self.throbber_frame, self.selected_sid)
        if self._lines_cache is not None and self._lines_cache[0] == key:
            return self._lines_cache[1]

        WIDTH = max(20, width)
        lines = []
        for sid, title, act, age, seen, last_asst, is_current in self.rows:
            icon = _activity_icon(act, self.throbber_frame, seen=seen)
            age_str = age.replace(" ago", "")

            if act == ThreadActivity.THINKING:
                title_color = C_BLUE
            elif act == ThreadActivity.AWAITING_INPUT and not seen:
                title_color = C_GREEN
            else:
                title_color = ""

            is_sel = sid == self.selected_sid
            sel_bar = "▍" if is_sel else " "

            right_text = "you" if is_current else age_str
            right_color = C_PURPLE if is_current else C_DIM

            avail = max(4, WIDTH - 3 - len(right_text) - 1)  # bar + icon + space + chip
            t = title.replace("\n", " ").strip()
            if len(t) > avail:
                t = t[: max(1, avail - 1)] + "…"
            pad = " " * max(1, avail - len(t) + 1)
            title_esc = _esc(t)
            if title_color:
                inner = f"[bold]{title_esc}[/bold]" if is_sel else title_esc
                title_fmt = f"[{title_color}]{inner}[/{title_color}]"
            else:
                title_fmt = f"[bold]{title_esc}[/bold]" if is_sel else title_esc
            line1 = f"{sel_bar}{icon} {title_fmt}{pad}[{right_color}]{right_text}[/{right_color}]"

            snippet_avail = max(4, WIDTH - 4)
            if last_asst:
                snippet = last_asst.replace("\n", " ").strip()
                if len(snippet) > snippet_avail:
                    snippet = snippet[: max(1, snippet_avail - 1)] + "…"
                line2 = f"{sel_bar}  [{C_FAINT}]{_esc(snippet)}[/{C_FAINT}]"
            else:
                line2 = f"{sel_bar}  [{C_FAINT}]—[/{C_FAINT}]"

            if is_sel and focused:
                line1 = f"[on {BG_SURFACE}]{line1}[/on {BG_SURFACE}]"
                line2 = f"[on {BG_SURFACE}]{line2}[/on {BG_SURFACE}]"

            lines.append(line1)
            lines.append(line2)
        self._lines_cache = (key, lines)
        return lines

    def _move(self, delta: int) -> None:
        if not self.rows:
            return
        sids = [r[0] for r in self.rows]
        try:
            idx = sids.index(self.selected_sid) if self.selected_sid else 0
        except ValueError:
            idx = 0
        idx = max(0, min(len(sids) - 1, idx + delta))
        self.selected_sid = sids[idx]

    def cycle_target(self, anchor_sid: str, delta: int) -> str | None:
        """Session id `delta` steps from `anchor_sid` in displayed order,
        wrapping at the ends; None when there's nothing to switch to."""
        sids = [r[0] for r in self.rows]
        if not sids:
            return None
        try:
            idx = sids.index(anchor_sid)
        except ValueError:
            return sids[0] if delta > 0 else sids[-1]
        if len(sids) < 2:
            return None
        target = sids[(idx + delta) % len(sids)]
        return target if target != anchor_sid else None

    def handle_key(self, ev) -> bool:
        key = ev.key
        if key in ("j", "down"):
            self._move(1)
            return True
        if key in ("k", "up"):
            self._move(-1)
            return True
        if key == "g":
            if self.rows:
                self.selected_sid = self.rows[0][0]
            return True
        if key == "G":
            if self.rows:
                self.selected_sid = self.rows[-1][0]
            return True
        if key in ("enter", "l", "L"):
            # No-op when the selection is the session we're already in.
            if (self.selected_sid and self.selected_sid != self._current_session_id
                    and self.on_select is not None):
                self.on_select(self.selected_sid)
            return True
        return False


# ── the view ──────────────────────────────────────────────────────────

class ClaudeSessionView(View):
    opaque = True

    def __init__(self, state, tabs, ws, session_id: str | None = None,
                 prompt: str | None = None, cwd: str | None = None,
                 reattach_tmux: bool = False) -> None:
        super().__init__()
        self.state = state
        self.tabs = tabs
        self.ws = ws
        self.store = state.store
        self._prompt = prompt
        self._cwd = cwd or self._resolve_cwd()
        self._is_new = session_id is None
        self.session_id = session_id or str(uuid.uuid4())
        self.start_time = time.time()
        self._reattach_tmux = reattach_tmux
        self._active_panel = "claude"
        self._zoomed_panel: str | None = None
        self._has_other_sessions = False
        self._rect: Rect | None = None
        self._started = False
        self._closed = False  # panes already detached/stopped

        # Pre-compute everything rendering needs (as the original __init__)
        self._sync_slash_commands()
        self._tigrc_path = self._generate_tigrc()
        self._initial_title = self._resolve_initial_title()
        self._claude_command = build_claude_command(
            session_id=self.session_id, cwd=self._cwd,
            sys_prompt=build_session_context(ws), prompt=prompt,
            ws_name=ws.name, is_new=self._is_new,
        )
        self._env = build_session_env(ws.id or "", self.session_id)
        self._tig_env = {"TIGRC_USER": self._tigrc_path, "GIT_OPTIONAL_LOCKS": "0"}
        self._git_branch = self._detect_git_branch()
        self._jsonl = str(claude_jsonl_path(self._cwd, self.session_id))
        self._sidebar_enabled = not os.environ.get("ORCH_NO_SIDEBAR")
        # ORCH_NO_SIDEBAR drops the sidebar entirely; _git_panes_on is the
        # runtime toggle within it (the two tig children are the CPU cost).
        self._git_panes_on = self._sidebar_enabled and git_panes_enabled(self)

        self.claude_pane = self._make_pane(self._claude_command, env=self._env,
                                           cwd=self._cwd)
        self.claude_pane.on_finished = self._on_claude_finished
        if self._sidebar_enabled:
            self.tig_status = self._make_pane("tig status", env=self._tig_env,
                                              cwd=self._cwd)
            self.tig_log = self._make_pane("tig", env=self._tig_env, cwd=self._cwd)
        else:
            self.tig_status = None
            self.tig_log = None

        self.header = SessionHeader(
            ws_name=ws.name,
            ws_status="archived" if ws.archived else "active",
            ws_category=ws.category.value if ws.category else "",
            session_id=self.session_id,
            jsonl_path=self._jsonl,
            initial_title=self._initial_title,
        )
        self.sessions_list = WsSessionList(ws.id or "", self.session_id)
        self.sessions_list.on_select = self._switch_to_session
        self.sessions_list.on_items_changed = self._on_items_changed

        # ctrl+r jump-to-message overlay
        self.picker = FuzzyList()
        self.picker.on_select = self._on_picker_selected
        self.picker.on_cancel = self._close_picker
        self.picker.input.on_change = self._on_picker_query_changed
        self._picker_active = False
        self._msg_snippets: dict[str, str] = {}

        self._keymap = self._build_keymap()
        self._footer_cache: dict[int, str] = {}
        self._layout_cache: tuple | None = None  # (key, layout dict)

    @staticmethod
    def _make_pane(command: str, *, env: dict, cwd: str) -> TerminalPane:
        """Pane factory — the test suite's stub point."""
        return TerminalPane(command, env=env, cwd=cwd,
                            passthrough_keys=_PASSTHROUGH_KEYS)

    # per-session exclusive group: cached views must not cancel each other
    @property
    def _header_group(self) -> str:
        return f"cs_header:{self.session_id}"

    # ── pre-compute helpers (ports) ───────────────────────────────

    def _resolve_cwd(self) -> str:
        from actions import ws_working_dir
        return ws_working_dir(self.ws)

    def _sync_slash_commands(self) -> None:
        cmds_src = Path(ORCH_DIR) / "commands"
        cmds_dst = Path.home() / ".claude" / "commands"
        if not cmds_src.is_dir():
            return
        cmds_dst.mkdir(parents=True, exist_ok=True)
        for cmd_file in cmds_src.glob("*.md"):
            dst_file = cmds_dst / cmd_file.name
            if not dst_file.is_symlink() or dst_file.resolve() != cmd_file.resolve():
                dst_file.unlink(missing_ok=True)
                dst_file.symlink_to(cmd_file)

    def _generate_tigrc(self) -> str:
        from actions import generate_tig_tigrc
        return generate_tig_tigrc(subtle=True)

    def _resolve_initial_title(self) -> str:
        if not self._is_new:
            jp = claude_jsonl_path(self._cwd, self.session_id)
            if jp.exists():
                try:
                    s = parse_session(jp)
                    if s:
                        import thread_namer
                        t = thread_namer.get_session_title(s)
                        if t:
                            return t
                except Exception:
                    pass
        if self._prompt:
            return self._prompt[:60]
        return ""

    def _detect_git_branch(self) -> str:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", self._cwd, "branch", "--show-current"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    # ── lifecycle ─────────────────────────────────────────────────

    def _panes(self) -> list[TerminalPane]:
        return [p for p in (self.claude_pane, self.tig_status, self.tig_log)
                if p is not None]

    def on_show(self) -> None:
        super().on_show()
        app = self.app
        for pane in self._panes():
            pane.request_paint = app.request_paint
            pane.copy_to_clipboard = app.copy_to_clipboard
            app.register_pane(pane)
        if not self._timers:  # first show, or timers cancelled by a pop
            self.set_interval(5.0, self._refresh_header)
            if self._sidebar_enabled:
                self.set_interval(0.5, self._refresh_sessions_list)
                self.set_interval(0.3, self._tick_throbber)
        if not self._started:
            self._started = True
            # The git-panes preference lives on the app, and a View only has
            # one once it's pushed — so this is the first point we can read it.
            self._git_panes_on = self._sidebar_enabled and git_panes_enabled(self)
            self._layout_cache = None
            self._size_panes()  # size before spawn: tmux -x/-y from pane dims
            self._start_terminals()
        self._refresh_header()  # populate immediately, don't wait 5s
        if self._sidebar_enabled:
            self._refresh_sessions_list()

    def on_hide(self) -> None:
        super().on_hide()
        if self.app is not None:
            for pane in self._panes():
                self.app.unregister_pane(pane)

    def on_resize(self, rect) -> None:
        self._rect = rect
        if self._started:
            self._size_panes()

    def sync_layout(self) -> None:
        # The claude pane's rect moves with the sidebar and header, neither of
        # which fires on_resize. While hidden nothing renders, so this is the
        # only thing keeping the pane (and tmux behind it) at the real size.
        if self._started:
            self._size_panes()

    def _size_panes(self) -> None:
        if self._rect is None and self.app is not None and self.app._size != (0, 0):
            self._rect = Rect(0, 0, *self.app._size)
        if self._rect is None:
            return
        lay = self._layout(self._rect)
        for pid in ("claude", "tig_status", "tig_log"):
            rect = lay[pid]
            pane = self._panel_pane(pid)
            if pane is not None and rect is not None and rect.w > 2 and rect.h > 2:
                pane.resize(rect.h - 2, rect.w - 2)  # border inset

    def _start_terminals(self) -> None:
        try:
            if self._reattach_tmux:
                self.claude_pane.attach_persistent(self.session_id)
            else:
                self.claude_pane.start_persistent(self.session_id)
        except Exception as e:
            # tmux failure (bad cwd, command over the ~16KB cap, …): back
            # out to the caller instead of crashing the app.
            self.app.notify(f"Session launch failed: {e}", timeout=6)
            self.set_timer(0, lambda: self.dismiss(None))
            return
        if self._git_panes_on:
            self._start_tig_panes()

    def _start_tig_panes(self) -> None:
        for pane in (self.tig_status, self.tig_log):
            if pane is not None:
                try:
                    pane.start()
                except Exception:
                    pass

    def _stop_tig_panes(self) -> None:
        for pane in (self.tig_status, self.tig_log):
            if pane is not None:
                try:
                    pane.stop()
                except Exception:
                    pass

    # ── teardown (rule #2: no PTY child may outlive the loop) ─────

    def _teardown(self) -> None:
        """Unregister panes, stop the tig children, delete temp files.
        Idempotent; the claude pane is detached/stopped by the caller."""
        if self.app is not None:
            for pane in self._panes():
                self.app.unregister_pane(pane)
        for pane in (self.tig_status, self.tig_log):
            if pane is not None:
                pane.stop()
        self._cleanup_files()

    def _cleanup_files(self) -> None:
        """Port of on_unmount: tigrc + consumed spawn-arg files."""
        if self._tigrc_path:
            try:
                os.unlink(self._tigrc_path)
            except OSError:
                pass
            self._tigrc_path = ""
        spawn_dir = Path.home() / ".cache" / "claude-orchestrator" / "spawn-args"
        for suffix in (".sys", ".prompt"):
            try:
                (spawn_dir / f"{self.session_id}{suffix}").unlink()
            except OSError:
                pass

    def emergency_close(self) -> None:
        """App is exiting with this view still on the stack: detach the
        claude attach-client and stop the tig children while the loop
        still runs (live PTY children deadlock executor shutdown)."""
        if self._closed:
            return
        self._closed = True
        try:
            self.claude_pane.detach_persistent()
        except Exception:
            pass
        self._teardown()

    # ── post-session handling ─────────────────────────────────────

    def _on_claude_finished(self) -> None:
        """Claude exited naturally (port of on_terminal_widget_finished)."""
        if self._closed:
            return
        self._closed = True
        auto_link_session(self.store, self.ws.id, self.session_id)
        log_session_exit(self.session_id, self.ws.name, self.start_time,
                         exit_type="tui")
        session = None
        jp = Path(self._jsonl)
        if jp.exists():
            try:
                session = parse_session(jp)
            except Exception:
                pass
        self.claude_pane.stop()  # attach client is gone; close our fds
        self._teardown()
        self.dismiss(session)

    def go_back(self) -> None:
        """Detach from the session — the process keeps running in tmux."""
        if self._closed:
            return
        self._closed = True
        self.claude_pane.detach_persistent()
        self._teardown()
        self.dismiss({"detached": True,
                      "session_id": self.session_id,
                      "ws": self.ws, "start_time": self.start_time,
                      "jsonl": self._jsonl})

    def _archive_and_go_back(self) -> None:
        """Ctrl+Space: archive this session, then detach and go back."""
        from datetime import timezone
        ws = self.store.get(self.ws.id) or self.ws
        if self.session_id not in ws.archived_sessions:
            ws.archived_sessions[self.session_id] = \
                datetime.now(timezone.utc).isoformat()
            self.store.update(ws)
        self.go_back()

    # ── keys ──────────────────────────────────────────────────────

    def _build_keymap(self) -> dict:
        keymap = {
            "ctrl+j": lambda: self._cycle_panel(1),
            "ctrl+k": lambda: self._cycle_panel(-1),
            "ctrl+e": self._action_extract_todo,
            "ctrl+h": self.go_back,
            "ctrl+backslash": self.go_back,
            "ctrl+z": self._action_zoom_panel,
            "ctrl+@": self._archive_and_go_back,
            "ctrl+r": self._action_jump_to_message,
            "ctrl+shift+j": lambda: self._cycle_session(1),
            "ctrl+shift+k": lambda: self._cycle_session(-1),
        }
        for key in key_set(_AUTO_MODE_KEYS):
            keymap[key] = self._action_toggle_auto_mode
        for key in key_set(_GIT_PANES_KEYS):
            keymap[key] = self._action_toggle_git_panes
        return keymap

    def on_key(self, ev) -> bool:
        if self._picker_active:
            if ev.key == "escape":
                self._close_picker()
            else:
                self.picker.handle_key(ev)
            self.request_paint()
            return True  # overlay is modal over the session
        key = ev.key
        # Terminals without the kitty protocol send \x08 for ctrl+h and
        # \x00 for ctrl+space (physical backspace is \x7f and forwards).
        if key == "backspace" and ev.char == "\x08":
            key = "ctrl+h"
        elif ev.char == "\x00":
            key = "ctrl+@"
        fn = self._keymap.get(key)
        if fn is not None:
            fn()
            self.request_paint()
            return True
        if self._active_panel == "sessions":
            if self.sessions_list.handle_key(ev):
                self.request_paint()
                return True
            return False  # ctrl+b/ctrl+x → app tab keys
        pane = self._panel_pane(self._active_panel) or self.claude_pane
        return pane.handle_key(ev)  # False for passthrough → app tab keys

    def on_paste(self, ev) -> bool:
        if self._picker_active or self._active_panel == "sessions":
            return True  # swallow: never leak a paste into a hidden PTY
        pane = self._panel_pane(self._active_panel) or self.claude_pane
        return pane.handle_paste(ev)

    def on_mouse(self, ev) -> bool:
        if self._picker_active or self._rect is None:
            return False
        lay = self._layout(self._rect)
        for pid in ("claude", "tig_status", "tig_log"):
            rect = lay[pid]
            pane = self._panel_pane(pid)
            if pane is None or rect is None:
                continue
            c = Rect(rect.x + 1, rect.y + 1, rect.w - 2, rect.h - 2)
            if c.x <= ev.x < c.right and c.y <= ev.y < c.bottom:
                return pane.handle_mouse(ev, ev.x - c.x, ev.y - c.y)
        return False

    # ── panel navigation ──────────────────────────────────────────

    def _panel_ids(self) -> list[str]:
        if not self._sidebar_enabled:
            return ["claude"]
        ids = ["claude"]
        if self._git_panes_on:
            ids += ["tig_status", "tig_log"]
        if self._has_other_sessions:
            ids.append("sessions")
        return ids

    def _panel_pane(self, pid: str) -> TerminalPane | None:
        return {"claude": self.claude_pane, "tig_status": self.tig_status,
                "tig_log": self.tig_log}.get(pid)

    def _cycle_panel(self, step: int) -> None:
        ids = self._panel_ids()
        try:
            idx = ids.index(self._active_panel)
        except ValueError:
            idx = 0
        self._active_panel = ids[(idx + step) % len(ids)]

    def _on_items_changed(self, has_items: bool) -> None:
        self._has_other_sessions = has_items
        if not has_items and self._active_panel == "sessions":
            self._active_panel = "claude"  # don't strand focus
        if not has_items and self._zoomed_panel == "sessions":
            self._zoomed_panel = None
        self.request_paint()

    def _action_toggle_git_panes(self) -> None:
        """Show/hide the tig panes, killing their processes when hidden.

        Hiding has to kill: `tig status` and `tig` re-run git on a timer, and
        a session view per tab multiplies that. The preference lives on
        AppState so other views pick it up, and ui_state.py persists it.
        """
        if not self._sidebar_enabled:
            return  # ORCH_NO_SIDEBAR — no sidebar to toggle
        state = getattr(self.app, "state", None)
        enabled = state.set_git_panes() if state is not None else not self._git_panes_on
        self.sync_git_panes(enabled)
        if self.app is not None:
            self.app.notify(f"Git panes {'on' if enabled else 'off'}", timeout=2)

    def sync_git_panes(self, enabled: bool | None = None) -> None:
        """Match the panes to the preference (read from AppState when None)."""
        if not self._sidebar_enabled:
            return
        if enabled is None:
            enabled = git_panes_enabled(self)
        if enabled == self._git_panes_on:
            return
        self._git_panes_on = enabled
        self._layout_cache = None
        if enabled:
            self._size_panes()  # size before spawn, as _start_terminals does
            self._start_tig_panes()
        else:
            self._stop_tig_panes()
        if self._active_panel not in self._panel_ids():
            self._active_panel = "claude"
        if self._zoomed_panel in ("tig_status", "tig_log") and not enabled:
            self._zoomed_panel = None
        self._size_panes()
        self.request_paint()

    def _action_zoom_panel(self) -> None:
        """Toggle zoom on the active panel — hide everything else."""
        self._zoomed_panel = None if self._zoomed_panel else self._active_panel
        self._size_panes()

    # ── C-e: extract todo ─────────────────────────────────────────

    def _action_extract_todo(self) -> None:
        self.claude_pane._write_to_pty("/user:extract-orch-todo\r")

    # ── auto mode (SESSION_KEYS toggle_auto_mode) ─────────────────

    def _action_toggle_auto_mode(self) -> None:
        # Delegate to the app so state is per-workstream, not per-view.
        toggle = getattr(self.app, "toggle_auto_mode", None)
        if toggle is not None:
            toggle(self.ws.id, self.session_id)

    # ── C-r: jump to a previous user message ──────────────────────

    def _action_jump_to_message(self) -> None:
        items, snippets = _build_picker_items(self._jsonl)
        self._msg_snippets = snippets
        self.picker.input.text = ""
        self.picker.input.cursor = 0
        self.picker.set_items(items)
        self._highlight_picker_last()
        self._picker_active = True

    def _highlight_picker_last(self) -> None:
        if self.picker.list.rows:
            self.picker.list.highlighted = len(self.picker.list.rows) - 1

    def _on_picker_query_changed(self, text: str) -> None:
        self.picker._refilter()
        if not text:  # filter cleared: re-highlight the newest message
            self._highlight_picker_last()

    def _close_picker(self) -> None:
        self._picker_active = False
        self.request_paint()

    def _on_picker_selected(self, item_id) -> None:
        snippet = self._msg_snippets.get(item_id)
        self._close_picker()
        if snippet:
            self.claude_pane.search_backward(snippet)

    # ── session switching (C-S-j/k + sidebar Enter) ───────────────

    def _switch_to_session(self, target_sid: str) -> None:
        """Detach the current session (keeps running in tmux), open another."""
        if not target_sid or target_sid == self.session_id or self._closed:
            return
        sessions = self.state.sessions_for_ws(self.ws)
        target = next((s for s in sessions if s.session_id == target_sid), None)
        if target is None:
            return
        app = self.app
        ws = self.ws
        self.go_back()  # detach + dismiss with the detached dict
        app.launch_claude_session(ws, session_id=target_sid)

    def _cycle_session(self, delta: int) -> None:
        """Next/prev session in the sidebar list's displayed order."""
        target = self.sessions_list.cycle_target(self.session_id, delta)
        if target:
            self._switch_to_session(target)

    # ── timers ────────────────────────────────────────────────────

    def _refresh_header(self) -> None:
        app = self.app
        app.exclusive(self._header_group,
                      self._header_runner(app.gen(self._header_group) + 1))

    async def _header_runner(self, g: int) -> None:
        lines = await asyncio.to_thread(self.header.refresh_blocking)
        if self.app.gen(self._header_group) != g:
            return  # superseded while off-loop — drop the stale result
        self.header.lines = lines
        self.request_paint()

    def _refresh_sessions_list(self) -> None:
        if self.sessions_list.refresh(self.state):
            self.request_paint()

    def _tick_throbber(self) -> None:
        app = self.app
        if app is not None and not app.ui_visible:
            return
        if self.sessions_list.tick_throbber():
            self.request_paint()

    # ── layout & render ───────────────────────────────────────────

    def _layout(self, rect: Rect) -> dict[str, Rect | None]:
        """Rect for every region; hidden regions are None. Memoized on its
        inputs — recomputed dozens of times a second while streaming."""
        key = (rect, self._zoomed_panel, self._sidebar_enabled,
               self._git_panes_on, len(self.header.lines),
               self._has_other_sessions, len(self.sessions_list.rows))
        if self._layout_cache is not None and self._layout_cache[0] == key:
            return self._layout_cache[1]
        lay = self._compute_layout(rect)
        self._layout_cache = (key, lay)
        return lay

    def _compute_layout(self, rect: Rect) -> dict[str, Rect | None]:
        tab, body, footer = split_rows(rect, 1, 1.0, 1)
        lay: dict[str, Rect | None] = {
            "tab": tab, "footer": footer, "header": None, "claude": None,
            "tig_status": None, "tig_log": None, "sessions": None,
        }
        zoom = self._zoomed_panel
        if zoom in ("tig_status", "tig_log", "sessions"):
            lay[zoom] = body  # zoomed sidebar panel takes the whole body
            return lay
        # A sidebar with tig off and no sibling sessions has nothing left to
        # show, so it collapses and claude takes the full width.
        sidebar_useful = self._git_panes_on or self._has_other_sessions
        if self._sidebar_enabled and sidebar_useful and zoom is None:
            main, sidebar = split_cols(body, 1.0, _SIDEBAR_W)
        else:
            main, sidebar = body, None  # claude zoom hides the sidebar
        header_h = max(0, min(len(self.header.lines), main.h - 4))
        header, claude = split_rows(main, header_h, 1.0)
        lay["header"] = header
        lay["claude"] = claude
        if sidebar is not None and self._git_panes_on:
            sess_h = 0
            if self._has_other_sessions:
                sess_h = min(_SESSIONS_MAX_H, 2 * len(self.sessions_list.rows) + 2,
                             sidebar.h // 2)
            tig_s, tig_l, sess = split_rows(sidebar, 1.0, 1.0, sess_h)
            lay["tig_status"] = tig_s
            lay["tig_log"] = tig_l
            lay["sessions"] = sess if sess_h > 0 else None
        elif sidebar is not None:
            # tig off: the sibling list is the only thing left in the column,
            # so it takes all of it (sidebar_useful kept us out of here if the
            # list is empty too).
            lay["sessions"] = sidebar
        return lay

    def _pane_content(self, frame, rect: Rect, focused: bool) -> Rect:
        """Panes always reserve a 1-cell border ring (the original's
        `border: blank`) so focus changes never resize the PTY."""
        if focused:
            draw_border(frame, rect, C_BLUE)
        return Rect(rect.x + 1, rect.y + 1, max(0, rect.w - 2), max(0, rect.h - 2))

    def render(self, frame, rect) -> None:
        self._rect = rect
        lay = self._layout(rect)
        frame.write_markup(lay["tab"].x + 1, lay["tab"].y, lay["tab"].w - 1,
                           render_tab_bar(self.state, self.tabs))
        if lay["header"] is not None:
            self._render_header(frame, lay["header"])
        for pid in ("claude", "tig_status", "tig_log"):
            r = lay[pid]
            pane = self._panel_pane(pid)
            if r is None or pane is None or r.w <= 2 or r.h <= 2:
                continue
            focused = self._active_panel == pid and not self._picker_active
            content = self._pane_content(frame, r, focused)
            pane.render(frame, content, focused=focused)
        if lay["sessions"] is not None:
            self._render_sessions(frame, lay["sessions"])
        self._render_footer(frame, lay["footer"])
        if self._picker_active:
            self._render_picker(frame, rect)

    def _raised_line(self, frame, x: int, y: int, w: int, markup: str,
                     bg: str = BG_RAISED) -> None:
        frame.write_markup(x, y, w, _padded_line(markup, w, bg))

    def _render_header(self, frame, rect: Rect) -> None:
        self.header.width = max(20, rect.w - 3)  # padding 0 1 0 2
        for i, line in enumerate(self.header.lines[:rect.h]):
            self._raised_line(frame, rect.x, rect.y + i, rect.w, f"  {line}")

    def _render_sessions(self, frame, rect: Rect) -> None:
        focused = self._active_panel == "sessions" and not self._picker_active
        frame.fill(rect, f"on {BG_RAISED}")  # rows may not reach the bottom
        c = self._pane_content(frame, rect, focused)
        if c.w <= 2 or c.h <= 0:
            return
        lines = self.sessions_list.render_lines(c.w - 2, focused)
        for i, line in enumerate(lines[:c.h]):
            self._raised_line(frame, c.x + 1, c.y + i, c.w - 2, line)

    def _render_footer(self, frame, rect: Rect) -> None:
        """1-line static footer (port of SessionFooterWidget). Composed
        once per width — its inputs never change for a given view."""
        cached = self._footer_cache.get(rect.w)
        if cached is None:
            sid_short = self.session_id[:8]
            short_cwd = self._cwd.replace(os.path.expanduser("~"), "~")
            left_parts = [
                f"[{C_BLUE}]{sid_short}[/]",
                f"[{C_DIM}]{_esc(short_cwd)}[/]",
            ]
            if self._git_branch:
                left_parts.append(f"[{C_PURPLE}]{_esc(self._git_branch)}[/]")
            left_parts.append(f"[{C_DIM}]│[/]")
            left_parts.append(f"[{C_YELLOW}]C-e[/] [{C_DIM}]extract[/]")
            left_parts.append(f"[{C_YELLOW}]C-j/k[/] [{C_DIM}]panels[/]")
            left_parts.append(f"[{C_YELLOW}]C-z[/] [{C_DIM}]zoom[/]")
            left_parts.append(f"[{C_YELLOW}]{_AUTO_MODE_LABEL}[/] [{C_DIM}]auto[/]")
            left_parts.append(f"[{C_YELLOW}]{_GIT_PANES_LABEL}[/] [{C_DIM}]git[/]")
            left = "  ".join(left_parts)
            flag = "--session-id" if self._is_new else "--resume"
            right = f"[{C_DIM}]claude {flag} {self.session_id}[/]"
            width = rect.w - 4  # padding 0 2
            gap = max(2, width - len(strip_markup(left)) - len(strip_markup(right)))
            cached = f"  {left}{' ' * gap}{right}"
            self._footer_cache = {rect.w: cached}
        self._raised_line(frame, rect.x, rect.y, rect.w, cached, bg=BG_CHROME)

    def _render_picker(self, frame, rect: Rect) -> None:
        """Bottom-docked jump-to-message overlay (port of #cs-picker-overlay)."""
        w = min(80, rect.w - 2)
        if w < 12 or rect.h < 6:
            return
        list_h = min(12, max(1, len(self.picker.list.rows)), rect.h - 5)
        h = list_h + 3  # border(2) + input line
        x = rect.x + (rect.w - w) // 2
        y = rect.bottom - 1 - h  # 1-row margin above the bottom edge
        box = Rect(x, y, w, h)
        frame.fill(box, f"on {BG_RAISED}")
        draw_border(frame, box, f"{C_FAINT} on {BG_RAISED}")
        ix, iw = box.x + 2, box.w - 4
        self.picker.list.page_size = list_h
        for i, line in enumerate(self.picker.list.render(iw, list_h)):
            self._raised_line(frame, ix, box.y + 1 + i, iw, line)
        if self.picker.query:
            q = self.picker.input.render(iw - 2)
        else:
            q = f"[{C_DIM}]Jump to user message…[/{C_DIM}]"
        self._raised_line(frame, ix, box.bottom - 2, iw,
                          f"[{C_YELLOW}]›[/{C_YELLOW}] {q}")
        frame.cursor = (ix + 2 + self.picker.input.cursor_col(iw - 2),
                        box.bottom - 2)
