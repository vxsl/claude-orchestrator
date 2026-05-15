"""Claude session screen — replaces the tmux 4-pane orch-claude layout.

Composes: header widget (3 lines, live stats), TerminalWidget (claude CLI),
footer widget (1 line, static), sidebar (two TerminalWidgets running tig).

All rendering happens inside Textual — no tmux pane splitting.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from textual import events, work
from textual.binding import Binding
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from models import Link, Store, Workstream
from rendering import (
    BG_RAISED, BG_BASE, BG_CHROME, BG_SURFACE,
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_GREEN, C_MID, C_ORANGE,
    C_PURPLE, C_YELLOW,
    CATEGORY_THEME, THROBBER_FRAMES,
    _activity_icon, _context_bar_compact, _session_title,
)
from sessions import ClaudeSession, parse_session
from terminal import TerminalWidget
from thread_namer import get_session_title
from threads import ThreadActivity, session_activity

# Keys that pass through the TerminalWidget to the screen for panel navigation
_PASSTHROUGH_KEYS = {"ctrl+j", "ctrl+k", "ctrl+e", "ctrl+h", "ctrl+z", "ctrl+backslash", "ctrl+b", "ctrl+x", "ctrl+@", "ctrl+y"}

ORCH_DIR = str(Path(__file__).parent)


# ── Git helpers ───────────────────────────────────────────────────────

def _git_status_snapshot() -> str:
    """Return a compact git status + recent log for the cwd, for system prompt injection."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
        status = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        parts = [f"Current branch: {branch}"]
        if status:
            parts.append(f"Status:\n{status}")
        else:
            parts.append("Status: clean")
        if log:
            parts.append(f"Recent commits:\n{log}")
        return "\n".join(parts)
    except Exception:
        return "(git status unavailable)"


# ── Session helpers ───────────────────────────────────────────────────

def auto_link_session(store: Store, ws_id: str, session_id: str) -> None:
    """Link a claude-session to a workstream if not already linked.

    Skips linking if the ws already has directory links (worktree/file) that would
    auto-discover this session. Session links are only useful as fallback for ws
    that lack directory-based discovery.
    """
    if not ws_id:
        return
    ws = store.get(ws_id)
    if not ws:
        return
    # If this ws has directory links, sessions are discovered automatically —
    # no need to accumulate session links (they grow unboundedly).
    has_dir_links = any(l.kind in ("worktree", "file") for l in ws.links)
    if has_dir_links:
        return
    for link in ws.links:
        if link.kind == "claude-session" and link.value == session_id:
            return
    ws.links.append(Link(kind="claude-session", label="session", value=session_id))
    ws.touch()
    store.update(ws)


def log_session_exit(session_id: str, ws_name: str, start_time: float,
                     exit_type: str = "textual") -> None:
    """Append a line to the session-exits diagnostic log."""
    try:
        diag_dir = Path.home() / ".cache" / "claude-orchestrator" / "diag"
        diag_dir.mkdir(parents=True, exist_ok=True)
        elapsed = int(time.time() - start_time)
        with open(diag_dir / "session-exits.log", "a") as f:
            f.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}  "
                f"exit={exit_type}  session={session_id[:8]}  "
                f"ws={ws_name}  elapsed={elapsed}s\n"
            )
    except Exception:
        pass


# ── Spawn-arg builders ──────────────────────────────────────────────
#
# These shape the system prompt, command line, env, and JSONL path used
# to spawn a claude session. Used by ClaudeSessionScreen for the
# interactive flow AND by `spawn_implementer_session` for the headless
# auto-mode flow — keep them as pure module functions so both paths
# stay byte-identical.

def build_session_context(ws: Workstream) -> str:
    """System-prompt context block for a session bound to `ws`."""
    parts = [f'You are working on the brain workstream: "{ws.name}"']
    if ws.description:
        parts.append(f"Description: {ws.description}")
    if ws.category:
        parts.append(f"Category: {ws.category.value}")
    if ws.notes:
        parts.append(f"Recent notes: {ws.notes[:500]}")

    # Continuation context (one-shot file dropped by a prior session)
    cont_dir = Path.home() / ".cache" / "claude-orchestrator" / "continuations"
    cont_file = cont_dir / f"{ws.id}.md"
    if ws.id and cont_file.exists():
        try:
            parts.append(f"\nContinuation context from previous session:\n{cont_file.read_text()}")
            cont_file.unlink()
        except Exception:
            pass

    if ws.id:
        parts.append(
            '\nExtract todo: The user can press C-e or type /user:extract-orch-todo '
            'to distill this conversation into a rich todo item on the workstream. '
            'The slash command has full instructions. You can also run '
            '`orch distill crystallize --text "..." --context "..."` directly. '
            '$ORCH_WS_ID is set automatically.'
        )
        parts.append(
            '\nNotify: Send a desktop notification to the user with '
            '`~/bin/notification/claude-notify.sh "message"`. '
            'Use this when you hit a blocker, need a decision, or finish a long task '
            'and want the user\'s attention. Keep the message short (one line).'
        )

    parts.append(
        '\ngitStatus: This is the git status at the start of the conversation. '
        'Note that this status is a snapshot in time, and will not update during the conversation.\n'
        + _git_status_snapshot()
    )
    parts.append(
        '\nIMPORTANT — commit your work: Commit early and often. '
        'Make a git commit as soon as you have a coherent working change, even mid-task. '
        'When you finish or pause, always commit before stopping. '
        'Do not leave work uncommitted — other agents share this repo and uncommitted changes are invisible to them.'
    )
    return "\n".join(parts)


def build_claude_command(
    session_id: str,
    cwd: str,
    sys_prompt: str,
    prompt: str | None,
    ws_name: str,
    is_new: bool,
) -> str:
    """Shell command that spawns claude with the given session params.

    The system prompt is always written to a file (claude consumes it
    via --append-system-prompt-file). A long positional prompt is also
    spilled to a file and read via command substitution at exec time,
    because tmux new-session caps the inner command at ~16KB.
    """
    args = ["claude"]
    if is_new:
        args += ["--session-id", session_id]
    else:
        args += ["--resume", session_id]

    spawn_dir = Path.home() / ".cache" / "claude-orchestrator" / "spawn-args"
    spawn_dir.mkdir(parents=True, exist_ok=True)

    sys_path = spawn_dir / f"{session_id}.sys"
    sys_path.write_text(sys_prompt)
    args += ["--append-system-prompt-file", str(sys_path)]

    args += ["-n", f"orch:{ws_name}"]

    try:
        from trust import is_trusted
        if is_trusted(cwd):
            args.append("--dangerously-skip-permissions")
    except Exception:
        pass

    if prompt and len(prompt) > 4000:
        prompt_path = spawn_dir / f"{session_id}.prompt"
        prompt_path.write_text(prompt)
        return shlex.join(args) + f' "$(cat {shlex.quote(str(prompt_path))})"'
    if prompt:
        args.append(prompt)
    return shlex.join(args)


def build_session_env(ws_id: str, session_id: str) -> dict[str, str]:
    return {
        "ORCH_WS_ID": ws_id or "",
        "ORCH_SESSION_ID": session_id,
        "CLAUDE_SESSION_ID": session_id,
        "ORCH_DIR": ORCH_DIR,
    }


def claude_jsonl_path(cwd: str, session_id: str) -> Path:
    # Claude encodes cwd as a project-dir name by replacing both "/" and "."
    # with "-" (dots in dir names like "ul.UB-6732-foo" become dashes too).
    encoded = cwd.replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def spawn_implementer_session(
    ws: Workstream,
    store: Store,
    prompt: str,
    cwd: str | None = None,
) -> tuple[str, Path]:
    """Spawn a claude session in tmux with no Textual UI attached.

    Used by auto-mode to launch implementer sessions without forcing the
    user's screen to switch. The session lives in the orch tmux server
    (TerminalWidget.TMUX_SOCKET) and can be attached to from the
    workstream detail view if the user wants to watch it.

    Returns (session_id, jsonl_path). Raises RuntimeError on tmux failure.
    """
    from actions import ws_working_dir
    from terminal import TerminalWidget

    session_id = str(uuid.uuid4())
    cwd_resolved = cwd or ws_working_dir(ws)
    sys_prompt = build_session_context(ws)
    cmd = build_claude_command(
        session_id=session_id,
        cwd=cwd_resolved,
        sys_prompt=sys_prompt,
        prompt=prompt,
        ws_name=ws.name,
        is_new=True,
    )
    env_vars = build_session_env(ws.id or "", session_id)

    env_prefix = " ".join(
        f"{k}={shlex.quote(v)}" for k, v in env_vars.items()
    )
    inner_cmd = f"env TERM=xterm-256color COLORTERM=truecolor {env_prefix} {cmd}"

    conf = TerminalWidget._tmux_conf_path()
    tmux_cmd = [
        "tmux", "-L", TerminalWidget.TMUX_SOCKET, "-f", conf,
        "new-session", "-d",
        "-s", session_id,
        "-x", "200", "-y", "50",
        "-c", cwd_resolved,
        inner_cmd,
    ]
    env = os.environ.copy()
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    env.pop("TMUX", None)
    result = subprocess.run(
        tmux_cmd, env=env, timeout=10, capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip() or "(no stderr)"
        raise RuntimeError(
            f"tmux new-session failed (rc={result.returncode}): {err} "
            f"[inner_cmd was {len(inner_cmd)} bytes]"
        )
    TerminalWidget._reload_tmux_config(env)
    return session_id, claude_jsonl_path(cwd_resolved, session_id)


# ── Header Widget ────────────────────────────────────────────────────

class SessionHeaderWidget(Static):
    """3-line live status header for a Claude session."""

    DEFAULT_CSS = f"""
    SessionHeaderWidget {{
        height: auto;
        padding: 0 1 0 2;
        background: {BG_RAISED};
    }}
    """

    def __init__(
        self,
        ws_name: str,
        ws_status: str,
        ws_category: str,
        session_id: str,
        jsonl_path: str,
        initial_title: str = "",
    ) -> None:
        super().__init__()
        self._ws_name = ws_name
        self._ws_status = ws_status
        self._ws_category = ws_category
        self._session_id = session_id
        self._jsonl_path = jsonl_path
        self._initial_title = initial_title
        self._start_time = time.time()
        # Resolve category color once (status removed)
        self._sc = C_DIM
        self._cc = C_DIM
        for k, v in CATEGORY_THEME.items():
            if k and k.value == ws_category:
                self._cc = v
                break

    def on_mount(self) -> None:
        self._render_static()
        self._cached_session: ClaudeSession | None = None
        self._last_jsonl_size: int = 0
        self._width: int = 80
        self._refresh_async()  # populate immediately, don't wait for first interval
        self.set_interval(5.0, self._refresh_async)

    def on_resize(self, event) -> None:
        self._width = self.content_size.width
        self._refresh_async()

    def _format_elapsed(self) -> str:
        secs = int(time.time() - self._start_time)
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m{secs % 60:02d}s"
        else:
            return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"

    def _render_static(self) -> None:
        """Initial render before any JSONL data is available."""
        elapsed = self._format_elapsed()
        sid_short = self._session_id[:8]
        title = self._initial_title or self._ws_name
        line1 = f"[bold]{_esc(title)}[/bold]  [{C_DIM}]{elapsed}[/]  [{C_FAINT}]{sid_short}[/]"
        line2 = f"[{C_BLUE}]ORCH[/]  [{C_PURPLE}]{_esc(self._ws_name)}[/]  [{self._sc}]{self._ws_status}[/]  [{self._cc}]{self._ws_category}[/]"
        line3 = f"[{C_FAINT}]{'─' * 8}[/]"
        self.update(f"{line1}\n{line2}\n{line3}")

    @work(thread=True)
    def _refresh_async(self) -> None:
        """Parse JSONL in a thread so we don't block the event loop.

        First call does a full parse; subsequent calls only read the tail
        of the file (last 8KB) for incremental updates. Full re-parse is
        triggered every 30s to keep token/message counts accurate.
        """
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
        context_tokens: int = 0
        context_window_size: int = 200_000
        last_msg = ""
        last_role = ""
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
                    # Incremental: only read tail for last_message updates
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
                    last_role = "user" if s.last_user_message_text else (s.last_message_role or "")
                    last_user_messages = s.last_user_messages or ([] if not last_msg else [last_msg])
                    title = get_session_title(s) or ""
            except Exception:
                pass

        if not title:
            title = self._initial_title or self._ws_name

        # Build markup
        r1_parts = []
        if model and model != "—":
            r1_parts.append(f"[{C_CYAN}]{model}[/]")
        r1_parts.append(f"[{C_DIM}]{elapsed}[/]")
        r1_parts.append(f"[{C_FAINT}]{sid_short}[/]")
        line1 = f"[bold]{_esc(title)}[/bold]  {'  '.join(r1_parts)}"

        l2 = f"[{C_BLUE}]ORCH[/]  [{C_PURPLE}]{_esc(self._ws_name)}[/]  [{self._sc}]{self._ws_status}[/]  [{self._cc}]{self._ws_category}[/]"
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
        line2 = l2

        ctx_bar = _context_bar_compact(context_tokens, context_window_size)
        bar = _tool_bar_markup(tool_counts)
        flist = _file_list_markup(files)
        l3_parts = [p for p in (ctx_bar, bar) if p]
        l3 = "  ".join(l3_parts)
        if flist:
            l3 += f"  {flist}"
        line3 = l3

        all_lines = [line1, line2, line3]
        if last_user_messages:
            # last_user_messages is most-recent-first; display oldest→newest (bottom = most recent)
            # Colors dim from oldest (darkest) to newest (brightest)
            msg_colors = ["#7a5218", "#b07a25", C_YELLOW]  # dim amber → mid gold → bright yellow
            prefix = "you said: "
            w = max(20, self._width)  # content width (already stored as content_size.width)
            msgs = list(reversed(last_user_messages[:3]))  # oldest first
            color_offset = 3 - len(msgs)
            for i, msg in enumerate(msgs):
                color = msg_colors[color_offset + i]
                clean = msg.replace("\n", " ").strip()
                available = max(1, w - len(prefix))
                if len(clean) > available:
                    text = clean[:available - 1] + "…"
                else:
                    text = clean
                is_last = i == len(msgs) - 1
                pad = " " * max(0, available - len(text) + (0 if is_last else 1))
                style = "bold italic" if is_last else "italic"
                all_lines.append(
                    f"[{C_DIM} on black]{_esc(prefix)}[/{C_DIM} on black]"
                    f"[{style} {color} on black]{_esc(text)}{pad}[/{style} {color} on black]"
                )

        self.app.call_from_thread(self.update, "\n".join(all_lines))


# ── Footer Widget ────────────────────────────────────────────────────

class SessionFooterWidget(Static):
    """1-line static footer bar for a Claude session."""

    DEFAULT_CSS = f"""
    SessionFooterWidget {{
        height: 1;
        padding: 0 2;
        background: {BG_CHROME};
        color: {C_DIM};
        dock: bottom;
    }}
    """

    def __init__(self, session_id: str, cwd: str, ws_name: str, git_branch: str = "", is_new: bool = False) -> None:
        super().__init__()
        self._session_id = session_id
        self._cwd = cwd
        self._ws_name = ws_name
        self._git_branch = git_branch
        self._is_new = is_new

    def on_mount(self) -> None:
        self._update_footer()

    def on_resize(self) -> None:
        self._update_footer()

    def _update_footer(self) -> None:
        import re
        sid_short = self._session_id[:8]
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

        left = "  ".join(left_parts)
        flag = "--session-id" if self._is_new else "--resume"
        right = f"[{C_DIM}]claude {flag} {self._session_id}[/]"

        # Compute visible widths (strip Rich markup)
        left_w = len(re.sub(r"\[/?[^\]]*\]", "", left))
        right_w = len(re.sub(r"\[/?[^\]]*\]", "", right))
        width = self.size.width - 4  # subtract padding
        gap = max(2, width - left_w - right_w)
        self.update(f"{left}{' ' * gap}{right}")


# ── Workstream Sessions Sidebar Widget ───────────────────────────────

class WsSessionListWidget(Static):
    """Sidebar list of non-idle sessions in the workstream (excluding the
    one currently being viewed). Focusable; j/k to move, Enter to switch."""

    class SessionSelected(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class ItemsChanged(Message):
        def __init__(self, has_items: bool) -> None:
            super().__init__()
            self.has_items = has_items

    DEFAULT_CSS = f"""
    WsSessionListWidget {{
        height: 1fr;
        padding: 0 1;
        background: {BG_RAISED};
    }}
    """

    can_focus = True

    def __init__(self, ws_id: str, current_session_id: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._ws_id = ws_id
        self._current_session_id = current_session_id
        # rows: (session_id, title, activity, age, seen, last_assistant_text)
        self._rows: list[tuple[str, str, ThreadActivity, str, bool, str]] = []
        self._selected_sid: str | None = None
        self._throbber_frame = 0
        self._last_had_items = False

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)
        self.set_interval(0.1, self._tick_throbber)

    def on_focus(self) -> None:
        self._repaint()

    def on_blur(self) -> None:
        self._repaint()

    def on_resize(self, event) -> None:
        self._repaint()

    def _tick_throbber(self) -> None:
        if any(r[2] == ThreadActivity.THINKING for r in self._rows):
            self._throbber_frame = (self._throbber_frame + 1) % len(THROBBER_FRAMES)
            self._repaint()

    def _refresh(self) -> None:
        try:
            state = self.app.state  # type: ignore[attr-defined]
            ws = state.store.get(self._ws_id) if self._ws_id else None
            sessions = state.sessions_for_ws(ws) if ws else []
        except Exception:
            sessions = []

        try:
            from threads import load_last_seen
            last_seen = load_last_seen()
        except Exception:
            last_seen = {}

        # "Blue or green": THINKING is always interesting (blue throbber);
        # AWAITING_INPUT only counts if unseen (bright green dot, not the
        # dim-green already-acknowledged variant).
        order = {
            ThreadActivity.THINKING: 0,
            ThreadActivity.AWAITING_INPUT: 1,
        }
        candidates = []
        for s in sessions:
            if s.session_id == self._current_session_id:
                continue
            if not s.is_live:
                continue
            act = session_activity(s, last_seen)
            if act not in order:
                continue
            seen_ts = last_seen.get(s.session_id, "")
            anchor = s.last_activity or s.started_at or ""
            seen = bool(seen_ts and anchor and seen_ts >= anchor)
            if act == ThreadActivity.AWAITING_INPUT and seen:
                continue
            candidates.append((order[act], -_iso_ts(s.last_activity or s.started_at), s, act, seen))
        candidates.sort(key=lambda x: (x[0], x[1]))

        new_rows: list[tuple[str, str, ThreadActivity, str, bool, str]] = []
        for _, _, s, act, seen in candidates:
            title = _session_title(s)
            last_asst = s.last_assistant_message_text or ""
            new_rows.append((s.session_id, title, act, s.age, seen, last_asst))

        self._rows = new_rows

        # Maintain selection across refreshes
        sids = [r[0] for r in self._rows]
        if self._selected_sid not in sids:
            self._selected_sid = sids[0] if sids else None

        has_items = bool(self._rows)
        if has_items != self._last_had_items:
            self._last_had_items = has_items
            self.post_message(self.ItemsChanged(has_items))

        self._repaint()

    def _repaint(self) -> None:
        if not self._rows:
            self.update(f"[{C_FAINT}]no other active sessions[/{C_FAINT}]")
            return

        WIDTH = max(20, self.content_size.width or 32)
        focused = self.has_focus
        lines = []
        for sid, title, act, age, seen, last_asst in self._rows:
            icon = _activity_icon(act, self._throbber_frame, seen=seen)
            age_str = age.replace(" ago", "")

            # Title color mirrors the icon: blue for THINKING, green for
            # unseen AWAITING_INPUT, default otherwise.
            if act == ThreadActivity.THINKING:
                title_color = C_BLUE
            elif act == ThreadActivity.AWAITING_INPUT and not seen:
                title_color = C_GREEN
            else:
                title_color = ""

            is_sel = sid == self._selected_sid
            sel_bar = "▍" if is_sel else " "

            # Line 1: ▍|space + icon + title + right-aligned age
            avail = max(4, WIDTH - 3 - len(age_str) - 1)  # bar + icon + space + age
            t = title.replace("\n", " ").strip()
            if len(t) > avail:
                t = t[: max(1, avail - 1)] + "…"
            pad = " " * max(1, avail - len(t) + 1)
            title_esc = _esc(t)
            bold = is_sel
            if title_color:
                inner = f"[bold]{title_esc}[/bold]" if bold else title_esc
                title_fmt = f"[{title_color}]{inner}[/{title_color}]"
            else:
                title_fmt = f"[bold]{title_esc}[/bold]" if bold else title_esc
            line1 = f"{sel_bar}{icon} {title_fmt}{pad}[{C_DIM}]{age_str}[/{C_DIM}]"

            # Line 2: indented snippet of last assistant message
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
        self.update("\n".join(lines))

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        sids = [r[0] for r in self._rows]
        try:
            idx = sids.index(self._selected_sid) if self._selected_sid else 0
        except ValueError:
            idx = 0
        idx = max(0, min(len(sids) - 1, idx + delta))
        self._selected_sid = sids[idx]
        self._repaint()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("j", "down"):
            event.stop()
            event.prevent_default()
            self._move(1)
        elif event.key in ("k", "up"):
            event.stop()
            event.prevent_default()
            self._move(-1)
        elif event.key == "g":
            event.stop()
            event.prevent_default()
            if self._rows:
                self._selected_sid = self._rows[0][0]
                self._repaint()
        elif event.key == "G":
            event.stop()
            event.prevent_default()
            if self._rows:
                self._selected_sid = self._rows[-1][0]
                self._repaint()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            if self._selected_sid:
                self.post_message(self.SessionSelected(self._selected_sid))


def _iso_ts(s: str) -> float:
    """Parse ISO timestamp to a unix-ts float, or 0 on failure (for sort keys)."""
    if not s:
        return 0.0
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Claude Session Screen ────────────────────────────────────────────

class ClaudeSessionScreen(Screen):
    """Full-screen Claude session with embedded terminal, header, footer, and tig sidebar."""

    BINDINGS = [
        Binding("ctrl+e", "extract_todo", "Extract todo", priority=True),
        Binding("ctrl+y", "toggle_auto_mode", "Auto mode", priority=True),
        Binding("ctrl+backslash", "go_back", "C-\\ back", priority=True),
    ]

    DEFAULT_CSS = f"""
    ClaudeSessionScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #detail-tab-bar {{
        height: 1;
        padding: 0 1;
        background: {BG_CHROME};
    }}
    #cs-outer {{
        width: 100%;
        height: 1fr;
        padding: 0;
        background: {BG_BASE};
    }}
    #cs-main-col {{
        width: 1fr;
    }}
    #cs-sidebar {{
        width: 36;
        background: {BG_RAISED};
    }}
    #cs-terminal {{
        height: 1fr;
        border: blank;
    }}
    #cs-terminal.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
    }}
    .cs-tig-wrap {{
        height: 1fr;
        border: blank;
        background: {BG_RAISED};
    }}
    .cs-tig-wrap.pane-focused {{
        border: round {C_BLUE};
        background: {BG_RAISED};
    }}
    #cs-tig-status, #cs-tig-log {{
        height: 1fr;
    }}
    #cs-other-sessions-wrap {{
        height: auto;
        max-height: 12;
    }}
    #cs-other-sessions {{
        height: auto;
    }}
    .panel-hidden {{
        display: none;
    }}
    .panel-zoom-hidden {{
        display: none;
    }}
    """

    def __init__(
        self,
        ws: Workstream,
        store: Store,
        session_id: str | None = None,
        prompt: str | None = None,
        cwd: str | None = None,
        reattach_tmux: bool = False,
    ) -> None:
        super().__init__()
        self._ws = ws
        self._store = store
        self._prompt = prompt
        self._cwd = cwd or self._resolve_cwd()
        self._is_new = session_id is None
        self._session_id = session_id or str(uuid.uuid4())
        self._sys_prompt = self._build_context()
        self._active_panel = "cs-terminal"
        self._zoomed_panel: str | None = None
        self._start_time = time.time()
        self._reattach_tmux = reattach_tmux  # reattach to surviving tmux session

        # Pre-compute everything compose() needs (no I/O in compose)
        self._sync_slash_commands()
        self._tigrc_path = self._generate_tigrc()
        self._initial_title = self._resolve_initial_title()
        self._claude_command = self._build_claude_command()
        self._env = self._build_env()
        self._tig_env = {"TIGRC_USER": self._tigrc_path, "GIT_OPTIONAL_LOCKS": "0"}
        self._git_branch = self._detect_git_branch()
        self._jsonl = self._jsonl_path()
        self._sidebar_enabled = not os.environ.get("ORCH_NO_SIDEBAR")
        self._has_other_sessions = False


    def _resolve_cwd(self) -> str:
        from actions import ws_working_dir
        return ws_working_dir(self._ws)

    # ── Context & command building ────────────────────────────────
    # Delegated to module-level helpers so the headless auto-mode spawn
    # path (`spawn_implementer_session`) produces byte-identical configs.

    def _build_context(self) -> str:
        return build_session_context(self._ws)

    def _build_claude_command(self) -> str:
        return build_claude_command(
            session_id=self._session_id,
            cwd=self._cwd,
            sys_prompt=self._sys_prompt,
            prompt=self._prompt,
            ws_name=self._ws.name,
            is_new=self._is_new,
        )

    def _build_env(self) -> dict[str, str]:
        return build_session_env(self._ws.id or "", self._session_id)

    def _jsonl_path(self) -> str:
        return str(claude_jsonl_path(self._cwd, self._session_id))

    def _detect_git_branch(self) -> str:
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

    # ── Slash command syncing ─────────────────────────────────────

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

    # ── Tigrc generation ──────────────────────────────────────────

    def _generate_tigrc(self) -> str:
        from actions import generate_tig_tigrc
        return generate_tig_tigrc(subtle=True)

    # ── Initial title resolution ──────────────────────────────────

    def _resolve_initial_title(self) -> str:
        if not self._is_new:
            jp = Path(self._jsonl_path())
            if jp.exists():
                try:
                    s = parse_session(jp)
                    if s:
                        t = get_session_title(s)
                        if t:
                            return t
                except Exception:
                    pass
        if self._prompt:
            return self._prompt[:60]
        return ""

    # ── Compose (pure — no I/O) ───────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-tab-bar")
        with Horizontal(id="cs-outer"):
            with Vertical(id="cs-main-col"):
                yield SessionHeaderWidget(
                    ws_name=self._ws.name,
                    ws_status="archived" if self._ws.archived else "active",
                    ws_category=self._ws.category.value if self._ws.category else "",
                    session_id=self._session_id,
                    jsonl_path=self._jsonl,
                    initial_title=self._initial_title,
                )
                yield TerminalWidget(
                    command=self._claude_command,
                    env=self._env,
                    cwd=self._cwd,
                    passthrough_keys=_PASSTHROUGH_KEYS,
                    id="cs-terminal",
                )
            if self._sidebar_enabled:
                with Vertical(id="cs-sidebar"):
                    with Vertical(id="cs-tig-status-wrap", classes="cs-tig-wrap"):
                        yield TerminalWidget(
                            command="tig status",
                            env=self._tig_env,
                            cwd=self._cwd,
                            passthrough_keys=_PASSTHROUGH_KEYS,
                            id="cs-tig-status",
                        )
                    with Vertical(id="cs-tig-log-wrap", classes="cs-tig-wrap"):
                        yield TerminalWidget(
                            command="tig",
                            env=self._tig_env,
                            cwd=self._cwd,
                            passthrough_keys=_PASSTHROUGH_KEYS,
                            id="cs-tig-log",
                        )
                    with Vertical(
                        id="cs-other-sessions-wrap",
                        classes="cs-tig-wrap panel-hidden",
                    ):
                        yield WsSessionListWidget(
                            ws_id=self._ws.id or "",
                            current_session_id=self._session_id,
                            id="cs-other-sessions",
                        )
        yield SessionFooterWidget(
            session_id=self._session_id,
            cwd=self._cwd,
            ws_name=self._ws.name,
            git_branch=self._git_branch,
            is_new=self._is_new,
        )

    def on_mount(self) -> None:
        # Defer until after the first layout pass so TerminalWidget.on_resize has fired
        # and _ncol/_nrow are set to the real widget dimensions. Starting before layout
        # would create the tmux session at the hardcoded 80x24 default, causing Claude
        # to render at 80 cols until the next SIGWINCH.
        self.call_after_refresh(self._start_terminals)

    def _start_terminals(self) -> None:
        claude_tw = self.query_one("#cs-terminal", TerminalWidget)
        if self._reattach_tmux:
            # Reattach to a surviving tmux session
            claude_tw.attach_persistent(self._session_id)
        else:
            claude_tw.start_persistent(self._session_id)
        if self._sidebar_enabled:
            for tw in self.query(TerminalWidget):
                if tw.id != "cs-terminal":
                    try:
                        tw.start()
                    except Exception:
                        pass
        claude_tw.focus()
        self._update_pane_focus()

    def on_unmount(self) -> None:
        if self._tigrc_path:
            try:
                os.unlink(self._tigrc_path)
            except OSError:
                pass
        # Claude has already consumed these at startup; safe to delete.
        spawn_dir = Path.home() / ".cache" / "claude-orchestrator" / "spawn-args"
        for suffix in (".sys", ".prompt"):
            p = spawn_dir / f"{self._session_id}{suffix}"
            try:
                p.unlink()
            except OSError:
                pass

    # ── Post-session handling ─────────────────────────────────────

    def on_terminal_widget_finished(self, event: TerminalWidget.Finished) -> None:
        widget = event._sender
        if not isinstance(widget, TerminalWidget) or widget.id != "cs-terminal":
            return  # A sidebar terminal exited, ignore


        auto_link_session(self._store, self._ws.id, self._session_id)
        log_session_exit(self._session_id, self._ws.name, self._start_time)

        # Parse final session data for summary
        session = None
        jp = Path(self._jsonl)
        if jp.exists():
            try:
                session = parse_session(jp)
            except Exception:
                pass

        # Stop sidebar terminals
        for tw in self.query(TerminalWidget):
            if tw.id != "cs-terminal":
                tw.stop()

        self.dismiss(session)

    # ── Navigation ─────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+z":
            event.stop()
            event.prevent_default()
            self.action_zoom_panel()
            return
        # ctrl+h → go back.
        # Two representations depending on terminal:
        #   key="ctrl+h"  — alacritty/kitty extended keyboard protocol
        #   key="backspace", character="\x08" — classic terminals (\x08 = ctrl+h)
        # Physical backspace sends \x7f (character="\x7f") and is NOT caught here;
        # TerminalWidget forwards it to the PTY so it works correctly in the session.
        if event.key == "ctrl+h" or (event.key == "backspace" and event.character == "\x08"):
            event.stop()
            event.prevent_default()
            self.action_go_back()
            return
        # ctrl+space (terminal sends \x00 → Textual: ctrl+@): archive + go back
        if event.key == "ctrl+@" or event.character == "\x00":
            event.stop()
            event.prevent_default()
            self._archive_and_go_back()
            return

    def _archive_and_go_back(self) -> None:
        """Ctrl+Space: archive this session, then detach and go back."""
        from datetime import datetime, timezone
        ws = self._store.get(self._ws.id) or self._ws
        if self._session_id not in ws.archived_sessions:
            ws.archived_sessions[self._session_id] = datetime.now(timezone.utc).isoformat()
            self._store.update(ws)
        self.action_go_back()

    def action_go_back(self) -> None:
        """Detach from the session — process keeps running in tmux."""
        claude_tw = self.query_one("#cs-terminal", TerminalWidget)
        claude_tw.detach_persistent()
        self.dismiss({"detached": True,
                      "session_id": self._session_id,
                      "ws": self._ws, "start_time": self._start_time,
                      "jsonl": self._jsonl})

    def action_help(self) -> None:
        from screens import HelpScreen
        self.app.push_screen(HelpScreen(context="session"))

    # ── Panel navigation ──────────────────────────────────────────

    @property
    def _panel_ids(self) -> list[str]:
        if not self._sidebar_enabled:
            return ["cs-terminal"]
        ids = ["cs-terminal", "cs-tig-status", "cs-tig-log"]
        if self._has_other_sessions:
            ids.append("cs-other-sessions")
        return ids

    _TIG_WRAP_IDS = {
        "cs-tig-status": "cs-tig-status-wrap",
        "cs-tig-log": "cs-tig-log-wrap",
        "cs-other-sessions": "cs-other-sessions-wrap",
    }

    def _update_pane_focus(self) -> None:
        """Toggle pane-focused class to match _active_panel."""
        for pid in self._panel_ids:
            border_id = self._TIG_WRAP_IDS.get(pid, pid)
            try:
                w = self.query_one(f"#{border_id}")
                if pid == self._active_panel:
                    w.add_class("pane-focused")
                else:
                    w.remove_class("pane-focused")
            except Exception:
                pass

    def action_next_panel(self) -> None:
        try:
            idx = self._panel_ids.index(self._active_panel)
        except ValueError:
            idx = 0
        next_id = self._panel_ids[(idx + 1) % len(self._panel_ids)]
        self._active_panel = next_id
        self._update_pane_focus()
        try:
            self.query_one(f"#{next_id}").focus()
        except Exception:
            pass

    def action_prev_panel(self) -> None:
        try:
            idx = self._panel_ids.index(self._active_panel)
        except ValueError:
            idx = 0
        prev_id = self._panel_ids[(idx - 1) % len(self._panel_ids)]
        self._active_panel = prev_id
        self._update_pane_focus()
        try:
            self.query_one(f"#{prev_id}").focus()
        except Exception:
            pass

    # ── C-z: zoom panel ──────────────────────────────────────────

    def action_zoom_panel(self) -> None:
        """Toggle zoom on the active panel — hide everything else.

        Uses `panel-zoom-hidden` so empty-state hides (panel-hidden) are
        preserved when we unzoom.
        """
        sidebar_panels = {
            "cs-tig-status": "cs-tig-status-wrap",
            "cs-tig-log": "cs-tig-log-wrap",
            "cs-other-sessions": "cs-other-sessions-wrap",
        }
        if self._zoomed_panel:
            # Unzoom: clear zoom-hides, leaving any empty-state hides in place
            self._zoomed_panel = None
            for w in self.query(".panel-zoom-hidden"):
                w.remove_class("panel-zoom-hidden")
        else:
            self._zoomed_panel = self._active_panel
            if self._active_panel == "cs-terminal":
                try:
                    self.query_one("#cs-sidebar").add_class("panel-zoom-hidden")
                except Exception:
                    pass
            elif self._active_panel in sidebar_panels:
                self.query_one("#cs-main-col").add_class("panel-zoom-hidden")
                this_wrap = sidebar_panels[self._active_panel]
                for pid, wrap in sidebar_panels.items():
                    if wrap != this_wrap:
                        try:
                            self.query_one(f"#{wrap}").add_class("panel-zoom-hidden")
                        except Exception:
                            pass

    # ── C-e: extract todo ─────────────────────────────────────────

    def action_extract_todo(self) -> None:
        term = self.query_one("#cs-terminal", TerminalWidget)
        term._write_to_pty("/user:extract-orch-todo\r")

    # ── C-y: auto mode (coordinator/implementer loop) ─────────────

    def action_toggle_auto_mode(self) -> None:
        # Delegate to the App so state is per-workstream, not per-screen.
        # Pressing ctrl+y on an implementer screen cancels the running loop
        # rather than starting a parallel one (which would spawn duplicates).
        self.app.toggle_auto_mode(self._ws.id, self._session_id)

    # ── Other-sessions sidebar widget integration ─────────────────

    def on_ws_session_list_widget_items_changed(
        self, event: "WsSessionListWidget.ItemsChanged"
    ) -> None:
        """Show/hide the other-sessions wrap based on whether the widget has rows."""
        self._has_other_sessions = event.has_items
        try:
            wrap = self.query_one("#cs-other-sessions-wrap")
        except Exception:
            return
        if event.has_items:
            wrap.remove_class("panel-hidden")
        else:
            wrap.add_class("panel-hidden")
            # Don't leave focus stranded on the now-hidden panel
            if self._active_panel == "cs-other-sessions":
                self._active_panel = "cs-terminal"
                self._update_pane_focus()
                try:
                    self.query_one("#cs-terminal").focus()
                except Exception:
                    pass

    def on_ws_session_list_widget_session_selected(
        self, event: "WsSessionListWidget.SessionSelected"
    ) -> None:
        """Switch the screen to the selected session — detach current, push new."""
        target_sid = event.session_id
        sessions = self.app.state.sessions_for_ws(self._ws)
        target = next((s for s in sessions if s.session_id == target_sid), None)
        if not target:
            return
        claude_tw = self.query_one("#cs-terminal", TerminalWidget)
        claude_tw.detach_persistent()
        self.dismiss({
            "detached": True,
            "session_id": self._session_id,
            "ws": self._ws,
            "start_time": self._start_time,
            "jsonl": self._jsonl,
        })
        self.app.launch_claude_session(self._ws, session_id=target_sid)


# ── Helpers ───────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape Rich markup characters."""
    return text.replace("[", "\\[").replace("]", "\\]")


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
