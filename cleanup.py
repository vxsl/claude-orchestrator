"""Auto-cleanup of idle Claude sessions.

The orch-sessions tmux server keeps Claude processes alive across orch
restarts so users can resume.  Without bounded lifetime, every spawn
accumulates ~500 MB of resident Claude until the user manually quits.

This module finds sessions whose JSONL hasn't been touched in N hours
and kills the underlying tmux session (which kills Claude).  The JSONL
file is preserved, so `claude --resume <id>` still works.

Idle threshold is configurable via ORCH_AUTO_CLEANUP_HOURS (default 6,
0 disables).  Currently-attached tmux sessions are always skipped.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from sessions import CLAUDE_PROJECTS_DIR
from terminal import TerminalWidget


DEFAULT_IDLE_HOURS = 6.0


def _idle_hours_from_env() -> float:
    raw = os.environ.get("ORCH_AUTO_CLEANUP_HOURS")
    if raw is None:
        return DEFAULT_IDLE_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_IDLE_HOURS


def attached_orch_sessions() -> set[str]:
    """Return tmux session names on the orch-sessions socket with attached clients."""
    try:
        result = subprocess.run(
            ["tmux", "-L", TerminalWidget.TMUX_SOCKET,
             "list-clients", "-F", "#{client_session}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()


def _build_jsonl_index(projects_dir: Path) -> dict[str, float]:
    """Map session_id -> latest JSONL mtime across all project dirs.

    A session UUID can appear in multiple project dirs (resume from another
    cwd); take the most recent mtime.
    """
    index: dict[str, float] = {}
    if not projects_dir.exists():
        return index
    for proj in projects_dir.iterdir():
        if not proj.is_dir():
            continue
        try:
            entries = list(proj.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.suffix != ".jsonl":
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            sid = f.stem
            if mtime > index.get(sid, 0.0):
                index[sid] = mtime
    return index


def find_idle_orch_sessions(
    idle_hours: float,
    *,
    now: float | None = None,
    projects_dir: Path | None = None,
    list_sessions=TerminalWidget.list_tmux_sessions,
    attached_fn=attached_orch_sessions,
) -> list[tuple[str, float]]:
    """Return [(session_name, age_hours)] for non-attached sessions older than threshold.

    Sessions without a discoverable JSONL are skipped — we have no signal
    of their last activity, so killing them might surprise the user.
    """
    if idle_hours <= 0:
        return []

    now = now if now is not None else time.time()
    cutoff = idle_hours * 3600
    attached = attached_fn()
    sessions = list_sessions()
    index = _build_jsonl_index(projects_dir or CLAUDE_PROJECTS_DIR)

    idle: list[tuple[str, float]] = []
    for name in sessions:
        if name in attached:
            continue
        mtime = index.get(name)
        if mtime is None:
            continue
        age = now - mtime
        if age >= cutoff:
            idle.append((name, age / 3600))
    idle.sort(key=lambda t: t[1], reverse=True)
    return idle


def kill_orch_session(session_name: str) -> bool:
    """Kill a tmux session on the orch-sessions socket.  Returns True on success."""
    try:
        result = subprocess.run(
            ["tmux", "-L", TerminalWidget.TMUX_SOCKET,
             "kill-session", "-t", session_name],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def cleanup_idle_orch_sessions(
    idle_hours: float,
    *,
    dry_run: bool = False,
    now: float | None = None,
    projects_dir: Path | None = None,
    list_sessions=TerminalWidget.list_tmux_sessions,
    attached_fn=attached_orch_sessions,
    kill_fn=kill_orch_session,
) -> list[tuple[str, float]]:
    """Kill idle orch-sessions tmux sessions.  Returns list of (name, age_hours) killed."""
    idle = find_idle_orch_sessions(
        idle_hours,
        now=now,
        projects_dir=projects_dir,
        list_sessions=list_sessions,
        attached_fn=attached_fn,
    )
    if dry_run:
        return idle
    killed: list[tuple[str, float]] = []
    for name, age_h in idle:
        if kill_fn(name):
            killed.append((name, age_h))
    return killed
