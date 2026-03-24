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


# ─── Git Remote Utilities ────────────────────────────────────────────

def get_git_remote_host(path: str) -> str | None:
    """Get the hostname of the git remote 'origin' for the repo at path.

    Parses both SSH (git@gitlab.com:org/repo) and HTTPS
    (https://github.com/user/repo) URL formats.
    Returns just the hostname (e.g., "gitlab.com"), or None on failure.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        if not url:
            return None
        # SSH format: git@hostname:org/repo.git
        if url.startswith("git@"):
            # git@gitlab.com:org/repo.git → gitlab.com
            host_part = url.split("@", 1)[1]
            hostname = host_part.split(":", 1)[0]
            return hostname
        # HTTPS format: https://hostname/user/repo
        if "://" in url:
            # https://github.com/user/repo → github.com
            after_scheme = url.split("://", 1)[1]
            hostname = after_scheme.split("/", 1)[0]
            # Strip optional user:pass@ prefix
            if "@" in hostname:
                hostname = hostname.split("@", 1)[1]
            return hostname
        return None
    except Exception:
        return None


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

        # Wait for orch-claude to finish creating all panes before switching
        # focus — avoids the half-width flash the user sees mid-layout.
        subprocess.run(
            ["tmux", "wait-for", f"orch-layout-ready-{window_id}"],
            capture_output=True, timeout=10,
        )
        log.debug("launch_orch_claude: layout-ready signal received for %s", window_id)

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


# ─── Tig Tigrc Generation ───────────────────────────────────────────

import tempfile


def generate_tig_tigrc(subtle: bool = False) -> str:
    """Generate a temp tigrc for an embedded tig widget.

    Reads the user's existing tigrc and appends orch-sidebar overrides.
    When subtle=True, applies a muted color theme matching the orch palette.
    Caller is responsible for deleting the returned path on cleanup.
    """
    user_tigrc = os.environ.get("TIGRC_USER", str(Path.home() / ".tigrc"))
    content = ""
    if os.path.isfile(user_tigrc):
        try:
            content = Path(user_tigrc).read_text()
        except Exception:
            pass

    content += """
# orch-sidebar overrides
set refresh-mode = periodic
set refresh-interval = 3
set main-view = line-number:no id:no date:no author:no commit-title:yes,overflow=no
set line-graphics = ascii
set status-view-show-untracked-dirs = no
set show-changes = no
"""
    if subtle:
        content += """
# Muted color theme — default bg so Textual widget background shows through
color default          color241  color234
color cursor           color245  color236  bold
color title-focus      color241  default   bold
color title-blur       color238  default
color header           color238  default
color stat-head        color234  color234
color section          color241  default
color main-commit      color241  default
color main-head        color183  default   bold
color main-refs        color139  default
color diff-header      color238  default
color diff-index       color236  default
color diff-chunk       color241  default
color diff-add         color71   default
color diff-del         color210  default
color "diff ---"       color236  default
color "diff +++"       color236  default
color "@@"             color241  default
color stat-staged      color71   default
color stat-unstaged    color210  default
color stat-untracked   color241  default
color help-group       color241  default   bold
color help-action      color241  default
"""

    fd, path = tempfile.mkstemp(suffix=".tigrc", prefix="orch-")
    os.write(fd, content.encode())
    os.close(fd)
    return path


# ─── File Picker ─────────────────────────────────────────────────────

def open_file_picker(cwd: str) -> None:
    """Open fzedit file picker in the given directory.

    Suspends the TUI and runs fzedit interactively.  If fzedit is not
    found on PATH the call is silently skipped (caller should notify).
    """
    import shutil

    fzedit = shutil.which("fzedit")
    if not fzedit:
        log.warning("open_file_picker: fzedit not found on PATH")
        return
    try:
        subprocess.run([fzedit], cwd=cwd)
    except Exception as e:
        log.error("open_file_picker: %s", e)


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


# ─── MR Cache ────────────────────────────────────────────────────────

_MR_CACHE_PATH = Path.home() / ".cache" / "jira-fzf" / "mr_cache.json"


def get_mr_cache() -> dict[str, dict]:
    """Read the dev-workflow-tools MR (merge request) cache.

    Returns a dict of branch_name -> MR info dict.
    """
    import json

    if not _MR_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_MR_CACHE_PATH.read_text())
        if isinstance(data, dict):
            return data
        # Handle list format: each entry has a source_branch key
        if isinstance(data, list):
            result: dict[str, dict] = {}
            for mr in data:
                branch = mr.get("source_branch", "")
                if branch:
                    result[branch] = mr
            return result
    except (json.JSONDecodeError, OSError):
        pass
    return {}


# ─── Ticket-Solve Cache ─────────────────────────────────────────────

_TICKET_SOLVE_DIR = Path.home() / ".cache" / "ticket-solve"


def get_ticket_solve_status(ticket_key: str) -> dict | None:
    """Read ticket-solve status for a specific ticket.

    Returns the parsed JSON dict or None if not found.
    """
    import json

    cache_file = _TICKET_SOLVE_DIR / f"{ticket_key}.json"
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ─── Worktree Discovery ─────────────────────────────────────────────

import re

_TICKET_KEY_RE = re.compile(r'^([A-Z]+-\d+)')
_SKIP_BRANCHES = frozenset({"main", "master", "develop", "dev", "staging", "production"})


def extract_ticket_key(branch: str) -> str:
    """Extract Jira ticket key from branch name (e.g. 'UB-1234-fix-thing' -> 'UB-1234')."""
    m = _TICKET_KEY_RE.match(branch)
    return m.group(1) if m else ""


def discover_worktrees(repo_paths: list[str]) -> list[dict]:
    """Discover all git worktrees across repos.

    Returns list of dicts with: path, branch, ticket_key, repo_path.
    Skips bare worktrees and main/master/develop branches.
    """
    results: list[dict] = []
    seen_paths: set[str] = set()

    for repo in repo_paths:
        worktrees = get_worktree_list(repo)
        for wt in worktrees:
            path = wt.get("path", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)

            if wt.get("bare"):
                continue

            branch = wt.get("branch", "")
            is_primary = (path == repo.rstrip("/"))
            if not branch or (branch in _SKIP_BRANCHES and not is_primary):
                continue

            ticket_key = extract_ticket_key(branch)
            results.append({
                "path": path,
                "branch": branch,
                "ticket_key": ticket_key,
                "repo_path": repo,
                "is_primary": is_primary,
            })

    return results


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
