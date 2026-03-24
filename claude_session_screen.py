"""Claude session screen — replaces the tmux 4-pane orch-claude layout.

Composes: header widget (3 lines, live stats), TerminalWidget (claude CLI),
footer widget (1 line, static), sidebar (two TerminalWidgets running tig).

All rendering happens inside Textual — no tmux pane splitting.
"""

from __future__ import annotations

import logging
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
from textual.screen import Screen
from textual.widgets import Static

from models import Link, Store, Workstream
from rendering import (
    BG_RAISED, BG_BASE, BG_CHROME, BG_SURFACE,
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_MID, C_ORANGE,
    C_PURPLE, C_YELLOW,
    CATEGORY_THEME,
)
from sessions import ClaudeSession, parse_session
from terminal import TerminalWidget
from thread_namer import get_session_title

log = logging.getLogger("orch.claude_session")

# Keys that pass through the TerminalWidget to the screen for panel navigation
_PASSTHROUGH_KEYS = {"ctrl+j", "ctrl+k", "ctrl+e", "ctrl+h", "ctrl+z", "ctrl+backslash", "ctrl+alt+h", "ctrl+alt+l"}

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


# ── Header Widget ────────────────────────────────────────────────────

class SessionHeaderWidget(Static):
    """3-line live status header for a Claude session."""

    DEFAULT_CSS = f"""
    SessionHeaderWidget {{
        height: auto;
        padding: 0 2;
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
        self._refresh_async()  # populate immediately, don't wait for first interval
        self.set_interval(5.0, self._refresh_async)

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
        duration = ""
        age = ""
        files: list[str] = []
        tool_counts: dict[str, int] = {}
        last_msg = ""
        last_role = ""

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
                    duration = s.duration_display
                    age = s.age
                    files = s.files_mutated or []
                    tool_counts = s.tool_counts or {}
                    last_msg = s.last_user_message_text or s.last_message_text or ""
                    last_role = "user" if s.last_user_message_text else (s.last_message_role or "")
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
            if duration:
                r2_parts.append(f"[{C_DIM}]{duration}[/]")
            if age:
                r2_parts.append(f"[{C_MID}]{age}[/]")
            l2 += f"  [{C_DIM}]│[/]  " + "  ".join(r2_parts)
        line2 = l2

        bar = _tool_bar_markup(tool_counts)
        flist = _file_list_markup(files)
        l3 = bar
        if flist:
            l3 += f"  {flist}"
        if last_msg:
            prefix = "you: " if last_role == "user" else ""
            clean = last_msg.replace("\n", " ").strip()
            if len(prefix + clean) > 60:
                clean = clean[:60 - len(prefix) - 1] + "…"
            l3 += f"  [{C_DIM}]│[/]  [{C_FAINT}]{_esc(prefix + clean)}[/]"
        line3 = l3

        self.app.call_from_thread(self.update, f"{line1}\n{line2}\n{line3}")


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

    def __init__(self, session_id: str, cwd: str, ws_name: str, git_branch: str = "") -> None:
        super().__init__()
        self._session_id = session_id
        self._cwd = cwd
        self._ws_name = ws_name
        self._git_branch = git_branch

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
        right = f"[{C_DIM}]claude --resume {self._session_id}[/]"

        # Compute visible widths (strip Rich markup)
        left_w = len(re.sub(r"\[/?[^\]]*\]", "", left))
        right_w = len(re.sub(r"\[/?[^\]]*\]", "", right))
        width = self.size.width - 4  # subtract padding
        gap = max(2, width - left_w - right_w)
        self.update(f"{left}{' ' * gap}{right}")


# ── Claude Session Screen ────────────────────────────────────────────

class ClaudeSessionScreen(Screen):
    """Full-screen Claude session with embedded terminal, header, footer, and tig sidebar."""

    BINDINGS = [
        Binding("ctrl+e", "extract_todo", "Extract todo", priority=True),
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
    .panel-hidden {{
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

        log.debug("ClaudeSessionScreen.__init__: sid=%s cwd=%s new=%s reattach_tmux=%s",
                  self._session_id[:8], self._cwd, self._is_new, reattach_tmux)

    def _resolve_cwd(self) -> str:
        from actions import ws_working_dir
        return ws_working_dir(self._ws)

    # ── Context & command building ────────────────────────────────

    def _build_context(self) -> str:
        ws = self._ws
        parts = [f'You are working on the brain workstream: "{ws.name}"']
        if ws.description:
            parts.append(f"Description: {ws.description}")
        if ws.category:
            parts.append(f"Category: {ws.category.value}")
        if ws.notes:
            parts.append(f"Recent notes: {ws.notes[:500]}")
        if self._prompt:
            parts.append(f"\nInitial task: {self._prompt}")

        # Continuation context
        cont_dir = Path.home() / ".cache" / "claude-orchestrator" / "continuations"
        cont_file = cont_dir / f"{ws.id}.md"
        if ws.id and cont_file.exists():
            try:
                parts.append(f"\nContinuation context from previous session:\n{cont_file.read_text()}")
                cont_file.unlink()
            except Exception:
                pass

        # Distill command hints
        if ws.id:
            parts.append(
                '\nExtract todo: The user can press C-e or type /user:extract-orch-todo '
                'to distill this conversation into a rich todo item on the workstream. '
                'The slash command has full instructions. You can also run '
                '`orch distill crystallize --text "..." --context "..."` directly. '
                '$ORCH_WS_ID is set automatically.'
            )

        # Commit reminder — agents frequently forget to commit, leaving all work unstaged
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

    def _build_claude_command(self) -> str:
        args = ["claude"]
        if self._is_new:
            args += ["--session-id", self._session_id]
        else:
            args += ["--resume", self._session_id]
        args += ["--append-system-prompt", self._sys_prompt]
        args += ["-n", f"orch:{self._ws.name}"]
        if self._prompt:
            args.append(self._prompt)
        return shlex.join(args)

    def _build_env(self) -> dict[str, str]:
        return {
            "ORCH_WS_ID": self._ws.id or "",
            "ORCH_SESSION_ID": self._session_id,
            "CLAUDE_SESSION_ID": self._session_id,
            "ORCH_DIR": ORCH_DIR,
        }

    def _jsonl_path(self) -> str:
        encoded_dir = self._cwd.replace("/", "-")
        return str(Path.home() / ".claude" / "projects" / encoded_dir / f"{self._session_id}.jsonl")

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
        log.debug("ClaudeSessionScreen.compose: starting")
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
        yield SessionFooterWidget(
            session_id=self._session_id,
            cwd=self._cwd,
            ws_name=self._ws.name,
            git_branch=self._git_branch,
        )
        log.debug("ClaudeSessionScreen.compose: done")

    def on_mount(self) -> None:
        log.debug("ClaudeSessionScreen.on_mount: starting terminals")
        claude_tw = self.query_one("#cs-terminal", TerminalWidget)
        if self._reattach_tmux:
            # Reattach to a surviving tmux session
            claude_tw.attach_persistent(self._session_id)
            log.debug("  reattached cs-terminal via tmux")
        else:
            claude_tw.start_persistent(self._session_id)
            log.debug("  started cs-terminal via tmux")
        if self._sidebar_enabled:
            for tw in self.query(TerminalWidget):
                if tw.id != "cs-terminal":
                    try:
                        tw.start()
                        log.debug("  started %s", tw.id)
                    except Exception as e:
                        log.error("  FAILED to start %s: %s", tw.id, e)
        claude_tw.focus()
        self._update_pane_focus()
        log.debug("ClaudeSessionScreen.on_mount: done")

    def on_unmount(self) -> None:
        if self._tigrc_path:
            try:
                os.unlink(self._tigrc_path)
            except OSError:
                pass

    # ── Post-session handling ─────────────────────────────────────

    def on_terminal_widget_finished(self, event: TerminalWidget.Finished) -> None:
        widget = event._sender
        if not isinstance(widget, TerminalWidget) or widget.id != "cs-terminal":
            return  # A sidebar terminal exited, ignore

        log.debug("Claude terminal finished, cleaning up")

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
        # Textual reports ctrl+h as key="backspace" character="\x08";
        # distinguish from physical backspace (character="\x7f").
        if event.key == "backspace" and event.character == "\x08":
            event.stop()
            event.prevent_default()
            self.action_go_back()

    def action_go_back(self) -> None:
        """Detach from the session — process keeps running in tmux."""
        claude_tw = self.query_one("#cs-terminal", TerminalWidget)
        claude_tw.detach_persistent()
        self.dismiss({"detached": True,
                      "session_id": self._session_id,
                      "ws": self._ws, "start_time": self._start_time,
                      "jsonl": self._jsonl})

    # ── Panel navigation ──────────────────────────────────────────

    @property
    def _panel_ids(self) -> list[str]:
        if self._sidebar_enabled:
            return ["cs-terminal", "cs-tig-status", "cs-tig-log"]
        return ["cs-terminal"]

    _TIG_WRAP_IDS = {
        "cs-tig-status": "cs-tig-status-wrap",
        "cs-tig-log": "cs-tig-log-wrap",
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
        """Toggle zoom on the active panel — hide everything else."""
        if self._zoomed_panel:
            # Unzoom: show everything
            self._zoomed_panel = None
            for w in self.query(".panel-hidden"):
                w.remove_class("panel-hidden")
        else:
            # Zoom the active panel
            self._zoomed_panel = self._active_panel
            if self._active_panel == "cs-terminal":
                # Hide sidebar, keep header/footer
                try:
                    self.query_one("#cs-sidebar").add_class("panel-hidden")
                except Exception:
                    pass
            elif self._active_panel in ("cs-tig-status", "cs-tig-log"):
                # Hide main column, hide the other sidebar panel
                self.query_one("#cs-main-col").add_class("panel-hidden")
                other = "cs-tig-log-wrap" if self._active_panel == "cs-tig-status" else "cs-tig-status-wrap"
                self.query_one(f"#{other}").add_class("panel-hidden")

    # ── C-e: extract todo ─────────────────────────────────────────

    def action_extract_todo(self) -> None:
        term = self.query_one("#cs-terminal", TerminalWidget)
        term._write_to_pty("/user:extract-orch-todo\r")


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
