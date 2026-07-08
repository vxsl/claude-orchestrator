"""Claude-session spawn helpers, shared by both UI engines.

These shape the system prompt, command line, env, and JSONL path used to
spawn a claude session, plus the post-session bookkeeping (auto-linking,
exit logging). Used by the Textual ClaudeSessionScreen, the tui engine's
ClaudeSessionView/OrchApp, and the headless auto-mode flow
(`spawn_implementer_session`) — keep them as pure module functions so
every spawn path stays byte-identical.

Moved verbatim out of claude_session_screen.py (which re-imports them
for back-compat) so the tui engine can use them without importing
Textual — tests/test_purity.py enforces that this module stays pure.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from models import Link, Store, Workstream

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
        # Force Claude Code's classic (inline) renderer instead of the
        # fullscreen TUI it defaults to since v2.1.172.  The fullscreen TUI
        # draws on the alternate screen and owns its own scrollback, which
        # leaves tmux's scrollback empty (history_size=0) — so copy-mode
        # scroll/select in the embedded terminal dead-ends at [0/0].  The
        # classic renderer streams the transcript into tmux's main buffer,
        # restoring unified scrollback + copy-mode.  See claude-code#67289.
        "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
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
    """Spawn a claude session in tmux with no UI attached.

    Used by auto-mode to launch implementer sessions without forcing the
    user's screen to switch. The session lives in the orch tmux server
    (TerminalHost.TMUX_SOCKET) and can be attached to from the
    workstream detail view if the user wants to watch it.

    Returns (session_id, jsonl_path). Raises RuntimeError on tmux failure.
    """
    from actions import ws_working_dir
    from term_host import TerminalHost

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

    conf = TerminalHost._tmux_conf_path()
    tmux_cmd = [
        "tmux", "-L", TerminalHost.TMUX_SOCKET, "-f", conf,
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
    TerminalHost._reload_tmux_config(env)
    return session_id, claude_jsonl_path(cwd_resolved, session_id)
