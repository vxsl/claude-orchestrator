"""Claude session discovery — scan ~/.claude/projects/ for sessions and parse JSONL data."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


# ─── Tool category mapping ────────────────────────────────────────
_TOOL_CATEGORIES: dict[str, str] = {
    "Edit": "mutate",
    "Write": "mutate",
    "MultiEdit": "mutate",
    "NotebookEdit": "mutate",
    "Read": "read",
    "Grep": "search",
    "Glob": "search",
    "ToolSearch": "search",
    "Bash": "bash",
    "Agent": "agent",
    "TaskCreate": "agent",
    "TaskUpdate": "agent",
}

_CLAUDE_PLANS_DIR = str(Path.home() / ".claude" / "plans") + os.sep


def _is_plan_file(filepath: str) -> bool:
    """True if filepath is inside ~/.claude/plans/."""
    return filepath.startswith(_CLAUDE_PLANS_DIR)


@dataclass
class SessionMessage:
    """A single message extracted from a session JSONL for content search."""
    role: str       # "user" or "assistant"
    text: str       # full message text (all text + thinking blocks concatenated)
    timestamp: str  # ISO timestamp


def _extract_full_text(data: dict) -> str:
    """Extract full (untruncated) text from a user or assistant JSONL entry.

    Unlike ``_extract_message_text`` this concatenates ALL text and thinking
    blocks rather than taking just the first one, and never truncates.
    """
    msg = data.get("message", {})
    if not msg:
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                t = block.get("text", "")
                if t and "[Request interrupted" not in t:
                    parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking", "")
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def extract_session_content(jsonl_path: str) -> list[SessionMessage]:
    """Extract all user/assistant messages with full text from a session JSONL.

    Returns messages in chronological order.  Includes text blocks and
    thinking blocks from assistant messages.  Skips tool_use, tool_result,
    and system messages.
    """
    messages: list[SessionMessage] = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type", "")
                if msg_type not in ("user", "assistant"):
                    continue
                text = _extract_full_text(data)
                if not text:
                    continue
                messages.append(SessionMessage(
                    role=msg_type,
                    text=text,
                    timestamp=data.get("timestamp", ""),
                ))
    except OSError:
        pass
    return messages


def _last_tool_name(msg: dict) -> str:
    """Return the name of the last tool_use block in an assistant message, or ''."""
    name = ""
    for block in msg.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
    return name


def _last_bash_has_commit(msg: dict) -> bool:
    """True if the last Bash tool_use in this assistant message contains 'git commit'."""
    has = False
    for block in msg.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
            cmd = block.get("input", {}).get("command", "")
            has = "git commit" in cmd
    return has


# Pattern: [branch sha] commit message
_COMMIT_RE = re.compile(r"\[(\S+)\s+([0-9a-f]{7,})\]\s+(.+)")


def _extract_commit_from_result(data: dict) -> tuple[str, str]:
    """Extract (sha, summary) from a tool_result following a git commit.

    Returns ('', '') if no commit pattern found.
    """
    msg = data.get("message", {})
    for block in msg.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content", "")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
        for text in texts:
            m = _COMMIT_RE.search(text)
            if m:
                return m.group(2), m.group(3).strip()
    return "", ""


def _is_human_turn(data: dict) -> bool:
    """True if this user message contains a real human prompt (text block), not just tool_results."""
    msg = data.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text and "[Request interrupted" not in text:
                    return True
    return False


def _is_interrupt_marker(data: dict) -> bool:
    """Return True if this user message is a '[Request interrupted…]' marker."""
    msg = data.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return "[Request interrupted" in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if "[Request interrupted" in (block.get("text") or ""):
                    return True
    return False


def _extract_message_text(data: dict) -> str:
    """Extract a short text snippet from a user or assistant JSONL entry."""
    msg = data.get("message", {})
    if not msg:
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Grab first text block
        text = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t and "[Request interrupted" not in t:
                    text = t
                    break
    else:
        return ""
    # Collapse to single line, truncate
    return " ".join(text.split())[:200]


@dataclass
class ClaudeSession:
    """A discovered Claude Code session."""
    session_id: str
    project_dir: str  # The project directory name (e.g., "-home-kyle-dev-claude-orchestrator")
    project_path: str  # Decoded real path (e.g., "/home/kyle/dev/claude-orchestrator")
    title: str = ""
    started_at: str = ""
    last_activity: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    message_count: int = 0
    assistant_message_count: int = 0
    model: str = ""
    jsonl_path: str = ""
    is_live: bool = False
    last_message_role: str = ""  # "user" or "assistant" — last message type in JSONL
    last_user_message_at: str = ""  # timestamp of last user message
    last_stop_reason: str = ""   # "end_turn", "tool_use", etc. — from last assistant message
    turn_complete: bool = False  # True when system:turn_duration logged after last user/assistant
    all_session_ids: list[str] = field(default_factory=list)  # All sessionIds found in JSONL (for resume matching)
    last_message_text: str = ""  # Snippet of last user or assistant message
    last_tool_name: str = ""     # Name of last tool_use in assistant message
    last_commit_sha: str = ""    # SHA from last git commit (if session ended with one)
    last_commit_summary: str = ""  # Commit message from last git commit
    tool_counts: dict[str, int] = field(default_factory=dict)  # category -> count
    files_mutated: list[str] = field(default_factory=list)      # basenames, ordered by first touch

    @property
    def duration_display(self) -> str:
        """Human-readable session duration from started_at to last_activity."""
        if not self.started_at or not self.last_activity:
            return ""
        try:
            start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.last_activity.replace("Z", "+00:00"))
            seconds = int((end - start).total_seconds())
            if seconds < 60:
                return ""
            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m"
            hours = minutes // 60
            mins = minutes % 60
            if mins:
                return f"{hours}h{mins:02d}m"
            return f"{hours}h"
        except (ValueError, TypeError):
            return ""

    @property
    def model_short(self) -> str:
        for k in ("opus", "sonnet", "haiku"):
            if k in self.model.lower():
                return k
        return self.model[:12] if self.model else "—"

    @property
    def tokens_display(self) -> str:
        total = self.total_input_tokens + self.total_output_tokens
        if total > 1_000_000:
            return f"{total / 1_000_000:.1f}M"
        if total > 1_000:
            return f"{total / 1_000:.1f}k"
        return str(total)

    @property
    def display_name(self) -> str:
        if self.title:
            return self.title
        # Extract something useful from the project path
        return self.project_path.replace(str(Path.home()), "~")

    @property
    def age(self) -> str:
        """Human-readable age."""
        ts = self.last_activity or self.started_at
        if not ts:
            return "unknown"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            delta = datetime.now().astimezone() - dt
            seconds = int(delta.total_seconds())
            if seconds < 0:
                return "just now"
            if seconds < 60:
                return f"{seconds}s ago"
            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours}h ago"
            days = hours // 24
            return f"{days}d ago"
        except (ValueError, TypeError):
            return "unknown"


def _decode_project_dir(dirname: str) -> str:
    """Convert Claude's project dir name back to a real path.
    e.g., '-home-kyle-dev-claude-orchestrator' -> '/home/kyle/dev/claude-orchestrator'
    """
    # Replace leading dash with /
    path = dirname.replace("-", "/", 1) if dirname.startswith("-") else dirname
    # Replace remaining dashes that are path separators
    # Claude uses dashes for path separators, but also for filenames with dashes
    # We need to be smart about this — check which interpretation yields a real path
    parts = dirname.lstrip("-").split("-")
    # Claude encodes dotfiles as double-dash: e.g. "--xmonad" for ".xmonad".
    # After splitting on "-", this produces empty strings before the dotfile segment.
    # Merge each empty part with the following segment, prefixed with ".".
    merged: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "" and i + 1 < len(parts):
            merged.append("." + parts[i + 1])
            i += 2
        else:
            merged.append(parts[i])
            i += 1
    parts = merged
    # Try to reconstruct the path
    best_path = "/" + "/".join(parts)
    # Try progressively joining segments to find real directories
    reconstructed = "/"
    remaining = parts[:]
    while remaining:
        # Try joining next segment
        candidate = os.path.join(reconstructed, remaining[0])
        if os.path.exists(candidate):
            reconstructed = candidate
            remaining.pop(0)
        elif len(remaining) > 1:
            # Maybe this segment has a dash in it — try joining with next
            candidate = os.path.join(reconstructed, remaining[0] + "-" + remaining[1])
            if os.path.exists(candidate):
                reconstructed = candidate
                remaining.pop(0)
                remaining.pop(0)
            else:
                # Try accumulating more
                found = False
                for n in range(2, min(len(remaining) + 1, 8)):
                    candidate = os.path.join(reconstructed, "-".join(remaining[:n]))
                    if os.path.exists(candidate):
                        reconstructed = candidate
                        remaining = remaining[n:]
                        found = True
                        break
                if not found:
                    # Give up on smart reconstruction, just join the rest
                    reconstructed = os.path.join(reconstructed, "-".join(remaining))
                    break
        else:
            reconstructed = os.path.join(reconstructed, remaining[0])
            break
    return reconstructed


def parse_session(jsonl_path: Path) -> Optional[ClaudeSession]:
    """Parse a JSONL session file into a ClaudeSession."""
    session_id = jsonl_path.stem  # UUID part of filename
    # Remove .jsonl.wakatime suffix if present
    if session_id.endswith(".wakatime"):
        return None

    project_dir = jsonl_path.parent.name
    project_path = _decode_project_dir(project_dir)

    session = ClaudeSession(
        session_id=session_id,
        project_dir=project_dir,
        project_path=project_path,
        jsonl_path=str(jsonl_path),
    )

    try:
        with open(jsonl_path) as f:
            first_ts = None
            last_ts = None
            _pending_commit = False  # True when last assistant msg had git commit
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                # Extract title
                if msg_type == "custom-title":
                    session.title = data.get("customTitle", "")
                    if not session.session_id and data.get("sessionId"):
                        session.session_id = data["sessionId"]

                # Extract session ID (first one wins as primary, but track all for resume detection)
                sid = data.get("sessionId", "")
                if sid and not session.session_id:
                    session.session_id = sid
                if sid and sid not in session.all_session_ids:
                    session.all_session_ids.append(sid)

                # Track timestamps
                ts = data.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                # Track last message role (user or assistant)
                if msg_type in ("user", "assistant"):
                    session.turn_complete = False  # new message resets turn completion
                    if msg_type == "user" and ts:
                        session.last_user_message_at = ts
                    # User replied → commit is no longer the last word
                    if msg_type == "user" and _is_human_turn(data):
                        session.last_commit_sha = ""
                        session.last_commit_summary = ""
                    snippet = _extract_message_text(data)
                    if snippet:
                        session.last_message_role = msg_type
                        session.last_message_text = snippet
                    # Interrupted turns: the "[Request interrupted" user
                    # message means Claude is back at the prompt.
                    if msg_type == "user" and _is_interrupt_marker(data):
                        session.turn_complete = True

                # Turn completion: turn_duration is the primary signal,
                # but idle-only entries (last-prompt, custom-title,
                # file-history-snapshot) also prove the turn ended —
                # covers interrupted turns where turn_duration is never written.
                if (msg_type == "system" and data.get("subtype") in (
                        "turn_duration", "stop_hook_summary")
                        or msg_type in ("last-prompt", "custom-title",
                                        "file-history-snapshot")):
                    session.turn_complete = True

                # Count user-sent messages
                if msg_type == "user":
                    session.message_count += 1

                # Count assistant messages
                if msg_type == "assistant":
                    session.assistant_message_count += 1

                # Extract commit SHA from tool_result after a git commit
                if _pending_commit and msg_type == "user" and "message" in data:
                    sha, summary = _extract_commit_from_result(data)
                    if sha:
                        session.last_commit_sha = sha
                        session.last_commit_summary = summary
                    _pending_commit = False

                # Extract usage and stop_reason from assistant messages
                if msg_type == "assistant" and "message" in data:
                    msg = data["message"]
                    usage = msg.get("usage", {})
                    session.total_input_tokens += usage.get("input_tokens", 0)
                    session.total_input_tokens += usage.get("cache_creation_input_tokens", 0)
                    session.total_input_tokens += usage.get("cache_read_input_tokens", 0)
                    session.total_output_tokens += usage.get("output_tokens", 0)
                    session.last_stop_reason = msg.get("stop_reason") or ""
                    session.last_tool_name = _last_tool_name(msg) or ""
                    # Track if this assistant message ends with a git commit
                    _pending_commit = _last_bash_has_commit(msg)
                    if not session.model and msg.get("model"):
                        session.model = msg["model"]
                    # Scan content blocks for tool usage
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            cat = _TOOL_CATEGORIES.get(block.get("name", ""), "other")
                            session.tool_counts[cat] = session.tool_counts.get(cat, 0) + 1
                            if cat == "mutate":
                                fp = block.get("input", {}).get("file_path", "")
                                if fp and not _is_plan_file(fp):
                                    bn = Path(fp).name
                                    if bn not in session.files_mutated:
                                        session.files_mutated.append(bn)

            session.started_at = first_ts or ""
            session.last_activity = last_ts or first_ts or ""

    except (OSError, json.JSONDecodeError):
        pass

    return session


def refresh_session_tail(session: ClaudeSession, tail_bytes: int = 8192) -> bool:
    """Re-read the tail of a session JSONL to update last_message_role and last_activity.

    Only updates 'last wins' fields — cheap enough to call every few seconds
    for live sessions. Returns True if any tracked field changed.
    """
    path = Path(session.jsonl_path)
    if not path.exists():
        return False

    old_role = session.last_message_role
    old_activity = session.last_activity
    old_stop = session.last_stop_reason

    try:
        size = path.stat().st_size
        offset = max(0, size - tail_bytes)

        with open(path, "rb") as f:
            if offset > 0:
                f.seek(offset)
                f.readline()  # skip partial first line
            content = f.read().decode("utf-8", errors="replace")

        _pending_commit = False
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = data.get("timestamp")
            if ts:
                session.last_activity = ts

            msg_type = data.get("type", "")

            # Extract commit SHA from tool_result after a git commit
            if _pending_commit and msg_type == "user" and "message" in data:
                sha, summary = _extract_commit_from_result(data)
                if sha:
                    session.last_commit_sha = sha
                    session.last_commit_summary = summary
                _pending_commit = False

            if msg_type in ("user", "assistant"):
                session.turn_complete = False
                if msg_type == "user" and ts:
                    session.last_user_message_at = ts
                # User replied → commit is no longer the last word
                if msg_type == "user" and _is_human_turn(data):
                    session.last_commit_sha = ""
                    session.last_commit_summary = ""
                snippet = _extract_message_text(data)
                if snippet:
                    session.last_message_role = msg_type
                    session.last_message_text = snippet
                if msg_type == "user" and _is_interrupt_marker(data):
                    session.turn_complete = True
            if msg_type == "assistant" and "message" in data:
                session.last_stop_reason = data["message"].get("stop_reason") or ""
                session.last_tool_name = _last_tool_name(data["message"]) or ""
                _pending_commit = _last_bash_has_commit(data["message"])
            if (msg_type == "system" and data.get("subtype") in (
                    "turn_duration", "stop_hook_summary")
                    or msg_type in ("last-prompt", "custom-title",
                                    "file-history-snapshot")):
                session.turn_complete = True

            # Track new session IDs from resumed sessions
            sid = data.get("sessionId", "")
            if sid and sid not in session.all_session_ids:
                session.all_session_ids.append(sid)

    except OSError:
        return False

    return (session.last_message_role != old_role
            or session.last_activity != old_activity
            or session.last_stop_reason != old_stop)


def _get_resumed_session_id(pid: int) -> str:
    """Extract the original session ID from a --resume argument in /proc/PID/cmdline."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        args = cmdline.split("\x00")
        for i, arg in enumerate(args):
            if arg == "--resume" and i + 1 < len(args):
                return args[i + 1]
            if arg == "--session-id" and i + 1 < len(args):
                return args[i + 1]
    except OSError:
        pass
    return ""


_live_ids_cache: tuple[float, str, set[str]] = (0.0, "", set())


def get_live_session_ids() -> set[str]:
    """Read ~/.claude/sessions/*.json to find currently-running session IDs.

    Each file contains a JSON object with pid, sessionId, cwd, startedAt.
    We verify the PID is still alive before considering it live.
    Also resolves --resume arguments so the original session ID is included.

    Results are cached for 2 seconds to avoid redundant /proc scans when
    called multiple times within the same poll cycle.
    """
    global _live_ids_cache
    now = time.monotonic()
    dir_str = str(CLAUDE_SESSIONS_DIR)
    if now - _live_ids_cache[0] < 2.0 and _live_ids_cache[1] == dir_str:
        return _live_ids_cache[2]

    live: set[str] = set()
    if not CLAUDE_SESSIONS_DIR.exists():
        _live_ids_cache = (now, dir_str, live)
        return live

    for f in CLAUDE_SESSIONS_DIR.iterdir():
        if not f.suffix == ".json":
            continue
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            session_id = data.get("sessionId")
            if not session_id or not pid:
                continue
            # Check if the process is still running
            try:
                os.kill(pid, 0)
                live.add(session_id)
                # Also add the original session ID if this is a resumed session
                original = _get_resumed_session_id(pid)
                if original:
                    live.add(original)
            except (OSError, ProcessLookupError):
                pass
        except (OSError, json.JSONDecodeError):
            continue

    _live_ids_cache = (time.monotonic(), dir_str, live)
    return live


# Module-level mtime cache: path_str -> (mtime, ClaudeSession)
_parsed_session_cache: dict[str, tuple[float, ClaudeSession]] = {}


def discover_sessions(
    limit: int = 0,
    project_filter: str = "",
    min_messages: int = 1,
) -> list[ClaudeSession]:
    """Discover Claude sessions from ~/.claude/projects/.

    Args:
        limit: Max number of sessions to return (0 = unlimited).
        project_filter: Filter by project directory substring.
        min_messages: Minimum number of assistant messages to include.

    Returns sorted by last_activity (most recent first).
    Uses an mtime-based cache to avoid re-parsing unchanged JSONL files.
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    live_ids = get_live_session_ids()
    sessions: list[ClaudeSession] = []
    seen_paths: set[str] = set()

    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in proj_dir.name.lower():
            continue

        for jsonl_file in proj_dir.glob("*.jsonl"):
            if jsonl_file.name.endswith(".wakatime"):
                continue
            path_str = str(jsonl_file)
            seen_paths.add(path_str)
            try:
                mtime = jsonl_file.stat().st_mtime
            except OSError:
                continue
            cached = _parsed_session_cache.get(path_str)
            if cached and cached[0] == mtime:
                session = copy.copy(cached[1])
                # Reset mutable fields that get set per-call
                session.is_live = False
            else:
                session = parse_session(jsonl_file)
                if session:
                    _parsed_session_cache[path_str] = (mtime, session)
            if session and session.message_count >= min_messages:
                # Match against all session IDs (handles resumed sessions
                # where session file has a new ID but JSONL has the original)
                session.is_live = bool(
                    live_ids & (set(session.all_session_ids) | {session.session_id})
                )
                sessions.append(session)

    # Prune cache entries for deleted files
    stale = set(_parsed_session_cache) - seen_paths
    for k in stale:
        del _parsed_session_cache[k]

    # Sort: live sessions first, then by last activity (most recent first)
    sessions.sort(key=lambda s: (not s.is_live, s.last_activity or ""), reverse=False)
    sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
    sessions.sort(key=lambda s: s.is_live, reverse=True)
    if limit > 0:
        sessions = sessions[:limit]
    return sessions


def find_session(session_id: str) -> Optional[ClaudeSession]:
    """Find a specific session by ID (exact or prefix match)."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return None

    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if jsonl_file.name.endswith(".wakatime"):
                continue
            if jsonl_file.stem == session_id or jsonl_file.stem.startswith(session_id):
                return parse_session(jsonl_file)
    return None


def sessions_for_project(project_path: str) -> list[ClaudeSession]:
    """Find all sessions for a given project path."""
    # Convert path to Claude's directory naming
    normalized = os.path.expanduser(project_path).rstrip("/")
    # The Claude directory name is the path with / replaced by -
    dir_name = normalized.replace("/", "-")

    target_dir = CLAUDE_PROJECTS_DIR / dir_name
    if not target_dir.is_dir():
        return []

    sessions = []
    for jsonl_file in target_dir.glob("*.jsonl"):
        if jsonl_file.name.endswith(".wakatime"):
            continue
        session = parse_session(jsonl_file)
        if session and session.message_count >= 1:
            sessions.append(session)

    sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
    return sessions
