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


def find_tmux_windows_for_ws(ws_name: str) -> list[tuple[str, str]]:
    """Find tmux windows tagged with an orch session ID whose name matches a workstream.

    Returns list of (session_id, window_id) tuples for windows named ``orch:<ws_name>``.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F",
             "#{@orch_session_id}\t#{window_id}\t#{window_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        target_name = f"\U0001f916{ws_name[:18]}"
        matches = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            sid, wid, wname = parts[0], parts[1], parts[2]
            if sid and wname == target_name:
                matches.append((sid, wid))
        return matches
    except Exception:
        return []


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
    """Resume a specific session immediately via ClaudeSessionScreen."""
    log.debug("resume_session_now: sid=%s title=%s", session.session_id, session.display_name)
    mark_thread_seen(session.session_id)

    cwd = session.project_path
    if not os.path.isdir(cwd):
        log.debug("resume_session_now: cwd %s not a dir, falling back", cwd)
        cwd = dirs[0] if dirs else os.getcwd()
    app.launch_claude_session(ws, session_id=session.session_id, cwd=cwd)


def do_resume(ws: Workstream, app, sessions: list[ClaudeSession] | None = None,
              sessions_for_ws_fn=None):
    """Smart resume: auto-discover sessions, fall back to directory.

    With 1 matching session: resumes immediately.
    With 2+: opens a thread picker so the user can choose.
    """
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
        app.launch_claude_session(ws, cwd=dirs[0])
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
            app.launch_claude_session(ws, session_id=link.value)
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


# ─── Git Status ─────────────────────────────────────────────────────

from dataclasses import dataclass, field


@dataclass
class WorktreeStatus:
    """Git status for a worktree directory."""
    path: str = ""
    branch: str = ""
    is_dirty: bool = False
    has_staged: bool = False
    has_unstaged: bool = False
    ahead: int = 0
    behind: int = 0
    error: str = ""


def get_worktree_git_status(path: str) -> WorktreeStatus:
    """Run `git status --porcelain --branch` in a directory and parse the result.

    Returns a WorktreeStatus with branch name, dirty state, and ahead/behind.
    Non-blocking — catches all errors and returns an error status.
    """
    status = WorktreeStatus(path=path)
    if not path or not os.path.isdir(path):
        status.error = "not a directory"
        return status

    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain=v1", "--branch"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            status.error = result.stderr.strip()[:100]
            return status

        for line in result.stdout.split("\n"):
            if line.startswith("## "):
                # Parse branch info: "## branch...origin/branch [ahead 1, behind 2]"
                branch_line = line[3:]
                # Extract branch name (before ...)
                if "..." in branch_line:
                    status.branch = branch_line.split("...")[0]
                else:
                    # No tracking info
                    status.branch = branch_line.split()[0] if branch_line.split() else ""

                # Parse ahead/behind
                if "[" in branch_line:
                    bracket = branch_line[branch_line.index("[") + 1:branch_line.index("]")]
                    for part in bracket.split(","):
                        part = part.strip()
                        if part.startswith("ahead"):
                            try:
                                status.ahead = int(part.split()[-1])
                            except ValueError:
                                pass
                        elif part.startswith("behind"):
                            try:
                                status.behind = int(part.split()[-1])
                            except ValueError:
                                pass
            elif line and not line.startswith("## "):
                # File status line
                index_status = line[0] if len(line) > 0 else " "
                worktree_status = line[1] if len(line) > 1 else " "
                if index_status not in (" ", "?"):
                    status.has_staged = True
                if worktree_status not in (" ", "?"):
                    status.has_unstaged = True
                status.is_dirty = True

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        status.error = str(e)[:100]

    return status


# ─── Jira Cache ──────────────────────────────────────────────────────

@dataclass
class JiraTicketInfo:
    """Cached Jira ticket metadata from dev-workflow-tools."""
    key: str = ""          # e.g. "UB-1234"
    summary: str = ""      # ticket title
    status: str = ""       # e.g. "In Progress", "Done"
    assignee: str = ""     # display name


_JIRA_CACHE_PATH = Path.home() / ".cache" / "jira-fzf" / "tickets.json"


def get_jira_cache() -> dict[str, JiraTicketInfo]:
    """Read the dev-workflow-tools Jira ticket cache.

    Returns a dict of ticket_key -> JiraTicketInfo.
    No API calls — reads only the existing cache file.
    """
    import json

    cache: dict[str, JiraTicketInfo] = {}
    if not _JIRA_CACHE_PATH.exists():
        return cache

    try:
        data = json.loads(_JIRA_CACHE_PATH.read_text())
        # The cache format is a list of ticket objects
        tickets = data if isinstance(data, list) else data.get("issues", [])
        for ticket in tickets:
            key = ticket.get("key", "")
            if not key:
                continue
            fields = ticket.get("fields", {})
            assignee = fields.get("assignee") or {}
            cache[key] = JiraTicketInfo(
                key=key,
                summary=fields.get("summary", ""),
                status=(fields.get("status") or {}).get("name", ""),
                assignee=assignee.get("displayName", "") if isinstance(assignee, dict) else "",
            )
    except (json.JSONDecodeError, OSError, KeyError):
        pass

    return cache


def get_jira_ticket_info(ticket_id: str) -> JiraTicketInfo | None:
    """Look up a single ticket in the Jira cache."""
    cache = get_jira_cache()
    return cache.get(ticket_id)


# ─── Dev-Workflow Tool Integration ───────────────────────────────────

_DEV_TOOLS_DIR = Path.home() / "bin" / "dev-workflow-tools"


def dev_tools_available() -> bool:
    """Check if dev-workflow-tools are installed."""
    return _DEV_TOOLS_DIR.is_dir()


def run_dev_tool(tool_name: str, args: list[str] | None = None,
                 cwd: str | None = None) -> list[str]:
    """Build the command list for a dev-workflow-tool.

    Returns the command to run (caller handles execution in terminal or subprocess).
    """
    tool_path = _DEV_TOOLS_DIR / "bin" / tool_name
    if not tool_path.exists():
        return []
    cmd = [str(tool_path)]
    if args:
        cmd.extend(args)
    return cmd


def get_worktree_list(repo_path: str) -> list[dict]:
    """Get all git worktrees for a repo.

    Returns list of dicts with: path, branch, bare, HEAD.
    """
    worktrees = []
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return worktrees

        current: dict = {}
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:].strip()}
            elif line.startswith("HEAD "):
                current["HEAD"] = line[5:].strip()
            elif line.startswith("branch "):
                # Strip refs/heads/ prefix
                branch = line[7:].strip()
                if branch.startswith("refs/heads/"):
                    branch = branch[11:]
                current["branch"] = branch
            elif line == "bare":
                current["bare"] = True
        if current:
            worktrees.append(current)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return worktrees


def get_recent_branches(repo_path: str, limit: int = 20) -> list[dict]:
    """Get recent branches from git reflog for a repo.

    Returns list of dicts with: branch, checkout_time.
    """
    branches = []
    seen = set()
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "reflog", "--format=%gd %gs",
             "--date=iso"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return branches

        for line in result.stdout.split("\n"):
            if "checkout: moving from" in line:
                # Extract the target branch
                parts = line.split("checkout: moving from ")
                if len(parts) >= 2:
                    rest = parts[1]
                    if " to " in rest:
                        target = rest.split(" to ")[1].strip()
                        if target and target not in seen:
                            seen.add(target)
                            branches.append({"branch": target})
                            if len(branches) >= limit:
                                break
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return branches


def run_git_action(action: str, cwd: str) -> tuple[bool, str]:
    """Run a simple git action in a directory.

    Returns (success, message).
    """
    if action == "wip":
        # Quick WIP commit
        try:
            subprocess.run(["git", "-C", cwd, "add", "-A"],
                          capture_output=True, timeout=10)
            result = subprocess.run(
                ["git", "-C", cwd, "commit", "--no-verify", "-m", "wip"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return True, "WIP commit created"
            return False, result.stderr.strip()[:100]
        except Exception as e:
            return False, str(e)[:100]

    elif action == "restage":
        # Unstage last 2 WIP commits, keep oldest staged
        tool_cmd = run_dev_tool("restage")
        if tool_cmd:
            try:
                result = subprocess.run(
                    tool_cmd, cwd=cwd,
                    capture_output=True, text=True, timeout=30,
                )
                return result.returncode == 0, result.stdout.strip()[:200]
            except Exception as e:
                return False, str(e)[:100]
        return False, "restage tool not found"

    return False, f"Unknown git action: {action}"
