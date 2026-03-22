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

from textual import work
from textual.binding import Binding
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static

from models import Link, Store, Workstream
from rendering import (
    BG_RAISED, BG_BASE, BG_SURFACE,
    C_BLUE, C_CYAN, C_DIM, C_FAINT, C_MID, C_ORANGE,
    C_PURPLE, C_YELLOW,
    STATUS_THEME, CATEGORY_THEME,
)
from sessions import ClaudeSession, parse_session
from terminal import TerminalWidget
from thread_namer import get_session_title

log = logging.getLogger("orch.claude_session")

# Keys that pass through the TerminalWidget to the screen for panel navigation
_PASSTHROUGH_KEYS = {"ctrl+j", "ctrl+k", "ctrl+e", "ctrl+h"}

ORCH_DIR = str(Path(__file__).parent)


# ── Tmux session helpers ──────────────────────────────────────────────

def tmux_session_name(session_id: str) -> str:
    """Deterministic tmux session name for a Claude session."""
    return f"orch-{session_id[:8]}"


def tmux_session_alive(name: str) -> bool:
    """Check if a tmux session exists."""
    return subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    ).returncode == 0


def auto_link_session(store: Store, ws_id: str, session_id: str) -> None:
    """Link a claude-session to a workstream if not already linked."""
    if not ws_id:
        return
    ws = store.get(ws_id)
    if not ws:
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
        padding: 1 3;
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
        # Resolve status/category colors once
        self._sc = C_DIM
        self._cc = C_DIM
        for k, v in STATUS_THEME.items():
            if k and k.value == ws_status:
                self._sc = v
                break
        for k, v in CATEGORY_THEME.items():
            if k and k.value == ws_category:
                self._cc = v
                break

    def on_mount(self) -> None:
        self._render_static()
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
        """Parse JSONL in a thread so we don't block the event loop."""
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
                s = parse_session(jp)
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
        background: {BG_RAISED};
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
        sid_short = self._session_id[:8]
        short_cwd = self._cwd.replace(os.path.expanduser("~"), "~")

        parts = [
            f"[{C_BLUE}]{sid_short}[/]",
            f"[{C_DIM}]{_esc(short_cwd)}[/]",
        ]
        if self._git_branch:
            parts.append(f"[{C_PURPLE}]{_esc(self._git_branch)}[/]")
        parts.append(f"[{C_DIM}]│[/]")
        parts.append(f"[{C_YELLOW}]C-e[/] [{C_DIM}]extract[/]")
        parts.append(f"[{C_YELLOW}]C-j/k[/] [{C_DIM}]panels[/]")

        self.update("  ".join(parts))


# ── Claude Session Screen ────────────────────────────────────────────

class ClaudeSessionScreen(Screen):
    """Full-screen Claude session with embedded terminal, header, footer, and tig sidebar."""

    BINDINGS = [
        Binding("ctrl+e", "extract_todo", "Extract todo", priority=True),
        Binding("ctrl+h", "go_back", "^H back", priority=True),
    ]

    DEFAULT_CSS = f"""
    ClaudeSessionScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #cs-outer {{
        width: 100%;
        height: 100%;
        padding: 0;
        background: {BG_BASE};
    }}
    #cs-main-col {{
        width: 1fr;
    }}
    #cs-sidebar {{
        width: 36;
    }}
    #cs-terminal, #cs-tig-status, #cs-tig-log {{
        height: 1fr;
        border: blank;
    }}
    #cs-terminal.pane-focused, #cs-tig-status.pane-focused, #cs-tig-log.pane-focused {{
        border: round {C_BLUE};
        background: {BG_SURFACE};
    }}
    """

    def __init__(
        self,
        ws: Workstream,
        store: Store,
        session_id: str | None = None,
        prompt: str | None = None,
        cwd: str | None = None,
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
        self._start_time = time.time()

        # Pre-compute everything compose() needs (no I/O in compose)
        self._sync_slash_commands()
        self._tigrc_path = self._generate_tigrc()
        self._initial_title = self._resolve_initial_title()
        self._claude_command = self._build_claude_command()
        self._env = self._build_env()
        self._tig_env = {"TIGRC_USER": self._tigrc_path}
        self._git_branch = self._detect_git_branch()
        self._jsonl = self._jsonl_path()
        self.tmux_name = tmux_session_name(self._session_id)
        self._create_tmux_session()

        log.debug("ClaudeSessionScreen.__init__: sid=%s cwd=%s tmux=%s new=%s",
                  self._session_id[:8], self._cwd, self.tmux_name, self._is_new)

    def _resolve_cwd(self) -> str:
        from actions import ws_working_dir
        return ws_working_dir(self._ws)

    # ── Context & command building ────────────────────────────────

    def _build_context(self) -> str:
        ws = self._ws
        parts = [f'You are working on the brain workstream: "{ws.name}"']
        if ws.description:
            parts.append(f"Description: {ws.description}")
        if ws.status:
            parts.append(f"Status: {ws.status.value}")
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

    # ── Tmux session management ──────────────────────────────────

    def _create_tmux_session(self) -> None:
        """Create a detached tmux session running the claude command, if not already running."""
        if tmux_session_alive(self.tmux_name):
            log.debug("tmux session %s already exists, will attach", self.tmux_name)
            return

        # Build shell command with env exports to avoid quoting issues
        env_exports = "; ".join(
            f"export {k}={shlex.quote(v)}" for k, v in self._env.items()
        )
        shell_cmd = f"{env_exports}; exec {self._claude_command}"

        result = subprocess.run(
            ["tmux", "new-session", "-d",
             "-s", self.tmux_name,
             "-c", self._cwd,
             shell_cmd],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error("tmux new-session failed: %s", result.stderr)
            raise RuntimeError(f"tmux new-session failed: {result.stderr}")

        # Configure the session: no status bar, no prefix interference
        for opt, val in [("status", "off"), ("remain-on-exit", "off"),
                         ("prefix", "F12"), ("prefix2", "F12")]:
            subprocess.run(
                ["tmux", "set-option", "-t", self.tmux_name, opt, val],
                capture_output=True,
            )
        log.debug("Created tmux session %s", self.tmux_name)

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
        user_tigrc = os.environ.get("TIGRC_USER", str(Path.home() / ".tigrc"))
        content = ""
        if os.path.isfile(user_tigrc):
            try:
                content = Path(user_tigrc).read_text()
            except Exception:
                pass

        content += """
# orch-sidebar overrides — compact for 36-column pane
set refresh-mode = periodic
set refresh-interval = 3
set main-view-date = custom
set main-view-date-format = "%m/%d"
set main-view-author = no
set main-view-id = yes
set main-view-id-width = 7
set main-view-line-number = no
set line-graphics = utf-8
set status-view-show-untracked-dirs = no
"""
        fd, path = tempfile.mkstemp(suffix=".tigrc", prefix="orch-")
        os.write(fd, content.encode())
        os.close(fd)
        return path

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
        with Horizontal(id="cs-outer"):
            with Vertical(id="cs-main-col"):
                yield SessionHeaderWidget(
                    ws_name=self._ws.name,
                    ws_status=self._ws.status.value if self._ws.status else "",
                    ws_category=self._ws.category.value if self._ws.category else "",
                    session_id=self._session_id,
                    jsonl_path=self._jsonl,
                    initial_title=self._initial_title,
                )
                yield TerminalWidget(
                    command=f"tmux attach-session -t {shlex.quote(self.tmux_name)}",
                    cwd=self._cwd,
                    passthrough_keys=_PASSTHROUGH_KEYS,
                    id="cs-terminal",
                )
                yield SessionFooterWidget(
                    session_id=self._session_id,
                    cwd=self._cwd,
                    ws_name=self._ws.name,
                    git_branch=self._git_branch,
                )
            with Vertical(id="cs-sidebar"):
                yield TerminalWidget(
                    command="tig status",
                    env=self._tig_env,
                    cwd=self._cwd,
                    passthrough_keys=_PASSTHROUGH_KEYS,
                    id="cs-tig-status",
                )
                yield TerminalWidget(
                    command="tig",
                    env=self._tig_env,
                    cwd=self._cwd,
                    passthrough_keys=_PASSTHROUGH_KEYS,
                    id="cs-tig-log",
                )
        log.debug("ClaudeSessionScreen.compose: done")

    def on_mount(self) -> None:
        log.debug("ClaudeSessionScreen.on_mount: starting terminals")
        for tw in self.query(TerminalWidget):
            try:
                tw.start()
                log.debug("  started %s", tw.id)
            except Exception as e:
                log.error("  FAILED to start %s: %s", tw.id, e)
        self.query_one("#cs-terminal", TerminalWidget).focus()
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

        log.debug("Claude terminal finished (tmux session ended), cleaning up")

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

    def action_go_back(self) -> None:
        """Detach from the session (ctrl+h) — tmux session keeps running."""
        self.dismiss("detached")

    # ── Panel navigation ──────────────────────────────────────────

    _PANEL_IDS = ["cs-terminal", "cs-tig-status", "cs-tig-log"]

    def _update_pane_focus(self) -> None:
        """Toggle pane-focused class to match _active_panel."""
        for pid in self._PANEL_IDS:
            try:
                w = self.query_one(f"#{pid}")
                if pid == self._active_panel:
                    w.add_class("pane-focused")
                else:
                    w.remove_class("pane-focused")
            except Exception:
                pass

    def action_next_panel(self) -> None:
        try:
            idx = self._PANEL_IDS.index(self._active_panel)
        except ValueError:
            idx = 0
        next_id = self._PANEL_IDS[(idx + 1) % len(self._PANEL_IDS)]
        self._active_panel = next_id
        self._update_pane_focus()
        try:
            self.query_one(f"#{next_id}").focus()
        except Exception:
            pass

    def action_prev_panel(self) -> None:
        try:
            idx = self._PANEL_IDS.index(self._active_panel)
        except ValueError:
            idx = 0
        prev_id = self._PANEL_IDS[(idx - 1) % len(self._PANEL_IDS)]
        self._active_panel = prev_id
        self._update_pane_focus()
        try:
            self.query_one(f"#{prev_id}").focus()
        except Exception:
            pass

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
