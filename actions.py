"""External process actions — tmux integration, Claude session launch, link opening.

No Textual dependency. Functions take explicit parameters instead of reaching into app state.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from models import Store, Workstream
from sessions import ClaudeSession, get_live_session_ids
from threads import mark_thread_seen

if TYPE_CHECKING:
    pass


# ─── Tmux Utilities ─────────────────────────────────────────────────

def has_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def find_tmux_window_for_session(session_id: str) -> str | None:
    """Find a tmux window already running a Claude session (via @orch_session_id tag)."""
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-F",
             "#{@orch_session_id}\t#{window_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split("\n"):
            if "\t" not in line:
                continue
            tag, wid = line.split("\t", 1)
            if tag == session_id:
                return wid
    except Exception:
        pass
    return None


def switch_to_tmux_window(window_id: str) -> bool:
    """Switch to an existing tmux window by ID."""
    try:
        result = subprocess.run(
            ["tmux", "select-window", "-t", window_id],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ─── Workstream Directory Helpers ────────────────────────────────────

def ws_directories(ws: Workstream) -> list[str]:
    """Get all directory paths linked to a workstream (worktree or file)."""
    dirs = []
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value)
            if os.path.isdir(expanded):
                dirs.append(expanded)
    return dirs


def ws_working_dir(ws: Workstream) -> str:
    dirs = ws_directories(ws)
    return dirs[0] if dirs else os.getcwd()


# ─── Session Launch ──────────────────────────────────────────────────

def launch_orch_claude(
    ws: Workstream,
    store: Store | None = None,
    session_id: str | None = None,
    prompt: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Launch Claude via the orch-claude wrapper in a new tmux window.

    Returns (success, error_message).
    """
    if not os.environ.get("TMUX"):
        return False, "Not running inside tmux"

    wrapper = str(Path(__file__).parent / "orch-claude")

    if cwd is None:
        cwd = ws_working_dir(ws)

    # Save the prompt as a note so it's never lost
    if prompt and prompt.strip() and store:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] spawn: {prompt.strip()}"
        ws.notes = (ws.notes + "\n" + entry) if ws.notes else entry
        store.update(ws)

    tmux_session = os.environ.get("TMUX_SESSION", "orch")

    cmd = [
        "tmux", "new-window", "-t", tmux_session,
        "-n", f"\U0001f916{ws.name[:18]}",
        "-c", cwd,
        wrapper,
        "--ws-id", ws.id,
        "--ws-name", ws.name,
        "--ws-desc", ws.description or "",
        "--ws-status", ws.status.value,
        "--ws-category", ws.category.value,
        "--cwd", cwd,
    ]

    if ws.notes:
        cmd += ["--ws-notes", ws.notes[:500]]

    if session_id:
        cmd += ["--resume", session_id]
    elif prompt:
        cmd += ["--prompt", prompt]

    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        try:
            proc.wait(timeout=2)
            if proc.returncode != 0:
                err = proc.stderr.read().decode().strip() if proc.stderr else "unknown error"
                return False, err
        except subprocess.TimeoutExpired:
            pass  # Still running = success (tmux window is up)
        return True, ""
    except Exception as e:
        return False, str(e)


# ─── Session Discovery for Workstreams ───────────────────────────────

def find_sessions_for_ws(ws: Workstream, all_sessions: list[ClaudeSession]) -> list[ClaudeSession]:
    """Auto-discover Claude sessions matching a workstream's directories."""
    found = []
    seen = set()

    # 1. Explicit claude-session links
    for link in ws.links:
        if link.kind == "claude-session":
            for s in all_sessions:
                if (s.session_id == link.value or s.session_id.startswith(link.value)) \
                        and s.session_id not in seen:
                    found.append(s)
                    seen.add(s.session_id)

    # 2. Auto-match by directory — exact match only
    ws_dirs = set()
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value).rstrip("/")
            if os.path.isdir(expanded):
                ws_dirs.add(expanded)

    if ws_dirs:
        for s in all_sessions:
            if s.session_id in seen:
                continue
            sp = s.project_path.rstrip("/")
            if sp in ws_dirs:
                found.append(s)
                seen.add(s.session_id)

    found.sort(key=lambda s: s.last_activity or "", reverse=True)
    return found


# ─── Resume Logic ────────────────────────────────────────────────────

def resume_session_now(ws: Workstream, session: ClaudeSession, dirs: list[str], app):
    """Resume a specific session immediately.

    If the session is already running in a tmux window (detached), switches
    to that window instead of spawning a duplicate.
    """
    mark_thread_seen(session.session_id)

    existing_wid = find_tmux_window_for_session(session.session_id)
    if existing_wid and switch_to_tmux_window(existing_wid):
        return

    cwd = session.project_path
    if not os.path.isdir(cwd):
        cwd = dirs[0] if dirs else os.getcwd()
    launch_orch_claude(ws, session_id=session.session_id, cwd=cwd)
    app.notify(f"Resuming: {session.display_name}", timeout=2)


def do_resume(ws: Workstream, app, sessions: list[ClaudeSession] | None = None,
              sessions_for_ws_fn=None):
    """Smart resume: auto-discover sessions, fall back to directory.

    With 1 matching session: resumes immediately.
    With 2+: opens a thread picker so the user can choose.
    """
    if not has_tmux():
        app.notify("Not in a tmux session", severity="error", timeout=2)
        return

    if sessions_for_ws_fn:
        matching = sessions_for_ws_fn(ws)
    else:
        matching = find_sessions_for_ws(ws, sessions or [])
    dirs = ws_directories(ws)

    if matching:
        if len(matching) == 1:
            resume_session_now(ws, matching[0], dirs, app)
        else:
            from screens import ThreadPickerScreen

            def on_pick(session: ClaudeSession | None):
                if session:
                    resume_session_now(ws, session, dirs, app)
            app.push_screen(ThreadPickerScreen(ws, matching), callback=on_pick)
        return

    if dirs:
        launch_orch_claude(ws, cwd=dirs[0])
        app.notify(f"New session in {dirs[0]}", timeout=2)
        return

    app.notify("No sessions or directories found", timeout=2)


# ─── Link Opening ────────────────────────────────────────────────────

def open_link(link, ws: Workstream | None = None, app=None):
    value = os.path.expanduser(link.value)
    if link.kind == "url":
        subprocess.Popen(["xdg-open", link.value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "worktree":
        if has_tmux():
            subprocess.Popen(["tmux", "new-window", "-n", link.label, "-c", value],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "file":
        if os.path.isdir(value):
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.path.isfile(value):
            editor = os.environ.get("EDITOR", "nvim")
            if has_tmux():
                subprocess.Popen(["tmux", "new-window", "-n", link.label, editor, value],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif link.kind == "claude-session":
        if ws and app:
            launch_orch_claude(ws, session_id=link.value)
        elif has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"claude:{link.label}",
                 "claude", "--resume", link.value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


# ─── Liveness Refresh ────────────────────────────────────────────────

def refresh_liveness(sessions: list[ClaudeSession]) -> None:
    """Update is_live flags on cached sessions from current process state."""
    live_ids = get_live_session_ids()
    for s in sessions:
        s.is_live = s.session_id in live_ids
