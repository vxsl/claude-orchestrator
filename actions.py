"""External process actions — tmux integration, Claude session launch, link opening.

No Textual dependency. Functions take explicit parameters instead of reaching into app state.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

log = logging.getLogger("orch.actions")
from pathlib import Path
from typing import TYPE_CHECKING

from models import Store, Workstream
from sessions import ClaudeSession, get_live_session_ids
from threads import mark_thread_seen

if TYPE_CHECKING:
    pass


# ─── Tmux Utilities ─────────────────────────────────────────────────

WORKER_SESSION = "orch-workers"


def has_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _ensure_worker_session() -> None:
    """Ensure a persistent tmux session for Claude workers (survives orch restarts)."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", WORKER_SESSION],
        capture_output=True, timeout=5,
    )
    if result.returncode != 0:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", WORKER_SESSION],
            capture_output=True, timeout=5,
        )


def find_tmux_window_for_session(session_id: str) -> str | None:
    """Find a tmux window already running a Claude session (via @orch_session_id tag).

    Searches all tmux sessions so we find windows that survived an orch restart.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F",
             "#{@orch_session_id}\t#{window_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.debug("find_tmux_window: list-windows failed rc=%d", result.returncode)
            return None
        matches = []
        for line in result.stdout.strip().split("\n"):
            if "\t" not in line:
                continue
            tag, wid = line.split("\t", 1)
            if tag:
                matches.append((tag, wid))
            if tag == session_id:
                log.debug("find_tmux_window: found %s -> %s", session_id, wid)
                return wid
        log.debug("find_tmux_window: no match for %s among %d tagged windows", session_id, len(matches))
    except Exception as e:
        log.debug("find_tmux_window: exception %s", e)
    return None


def capture_session_pane(session_id: str) -> str | None:
    """Capture the visible content of a live session's main tmux pane.

    Returns the pane text, or None if the session has no live tmux window.
    """
    window_id = find_tmux_window_for_session(session_id)
    if not window_id:
        return None
    try:
        # Get the @orch_main_pane option from the window
        result = subprocess.run(
            ["tmux", "display-message", "-t", window_id, "-p", "#{@orch_main_pane}"],
            capture_output=True, text=True, timeout=3,
        )
        pane_id = result.stdout.strip()
        if not pane_id or result.returncode != 0:
            return None
        # Capture the pane content
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-e"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception as e:
        log.debug("capture_session_pane: %s", e)
        return None


def switch_to_tmux_window(window_id: str) -> bool:
    """Switch to an existing tmux window by ID.

    Always links the window into the current tmux session first, because
    select-window on a window in a different session (e.g. orch-workers)
    silently switches THAT session's active window without affecting the
    user's view.
    """
    try:
        cur = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not cur:
            log.debug("switch_to_tmux_window: can't determine current session")
            return False

        # Always try to link into the current session (idempotent if already linked)
        link_result = subprocess.run(
            ["tmux", "link-window", "-s", window_id, "-t", f"{cur}:"],
            capture_output=True, text=True, timeout=5,
        )
        log.debug("switch_to_tmux_window: link-window %s -> %s rc=%d stderr=%s",
                  window_id, cur, link_result.returncode, (link_result.stderr or "").strip())

        # Now select it — it's guaranteed to be in our session
        result = subprocess.run(
            ["tmux", "select-window", "-t", window_id],
            capture_output=True, text=True, timeout=5,
        )
        log.debug("switch_to_tmux_window: select-window %s rc=%d", window_id, result.returncode)
        return result.returncode == 0
    except Exception as e:
        log.debug("switch_to_tmux_window: exception %s", e)
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
    if ws.repo_path:
        expanded = os.path.expanduser(ws.repo_path)
        if os.path.isdir(expanded):
            return expanded
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

    orch_session = os.environ.get("TMUX_SESSION", "orch")
    window_name = f"\U0001f916{ws.name[:18]}"

    wrapper_args = [
        wrapper,
        "--ws-id", ws.id,
        "--ws-name", ws.name,
        "--ws-desc", ws.description or "",
        "--ws-status", ws.status.value,
        "--ws-category", ws.category.value,
        "--cwd", cwd,
    ]

    if ws.notes:
        wrapper_args += ["--ws-notes", ws.notes[:500]]

    if session_id:
        wrapper_args += ["--resume", session_id]
    elif prompt:
        wrapper_args += ["--prompt", prompt]

    try:
        # Create window in a persistent worker session so Claude survives
        # if the orch session is destroyed (e.g. destroy-unattached).
        _ensure_worker_session()

        log.debug("launch_orch_claude: new-window in %s, name=%s, cwd=%s, resume=%s",
                  WORKER_SESSION, window_name, cwd, session_id)
        result = subprocess.run(
            ["tmux", "new-window", "-t", WORKER_SESSION,
             "-n", window_name, "-c", cwd,
             "-P", "-F", "#{window_id}"] + wrapper_args,
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.error("launch_orch_claude: new-window FAILED rc=%d stderr=%s",
                      result.returncode, result.stderr.strip())
            return False, result.stderr.strip()

        window_id = result.stdout.strip()
        log.debug("launch_orch_claude: created window %s", window_id)

        # Link the window into the orch session so the user sees it
        link_result = subprocess.run(
            ["tmux", "link-window", "-s", window_id, "-t", f"{orch_session}:"],
            capture_output=True, text=True, timeout=5,
        )
        log.debug("launch_orch_claude: link-window rc=%d stderr=%s",
                  link_result.returncode, (link_result.stderr or "").strip())
        sel_result = subprocess.run(
            ["tmux", "select-window", "-t", window_id],
            capture_output=True, text=True, timeout=5,
        )
        log.debug("launch_orch_claude: select-window rc=%d", sel_result.returncode)

        return True, ""
    except Exception as e:
        log.error("launch_orch_claude: exception %s", e)
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
    log.debug("resume_session_now: sid=%s title=%s", session.session_id, session.display_name)
    mark_thread_seen(session.session_id)

    existing_wid = find_tmux_window_for_session(session.session_id)
    log.debug("resume_session_now: existing_wid=%s", existing_wid)
    if existing_wid and switch_to_tmux_window(existing_wid):
        log.debug("resume_session_now: switched to existing window %s", existing_wid)
        return

    cwd = session.project_path
    if not os.path.isdir(cwd):
        log.debug("resume_session_now: cwd %s not a dir, falling back", cwd)
        cwd = dirs[0] if dirs else os.getcwd()
    ok, err = launch_orch_claude(ws, session_id=session.session_id, cwd=cwd)
    log.debug("resume_session_now: launch_orch_claude ok=%s err=%r", ok, err)
    if ok:
        app.notify(f"Resuming: {session.display_name}", timeout=2)
    else:
        log.error("resume_session_now: FAILED sid=%s err=%s", session.session_id, err)
        app.notify(f"Resume failed: {err}", severity="error", timeout=4)


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
            from screens import SessionPickerScreen

            def on_pick(session: ClaudeSession | None):
                if session:
                    resume_session_now(ws, session, dirs, app)
            app.push_screen(SessionPickerScreen(ws, matching), callback=on_pick)
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
