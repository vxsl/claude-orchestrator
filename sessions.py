"""Claude session discovery — scan ~/.claude/projects/ for sessions and parse JSONL data."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


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
    model: str = ""
    jsonl_path: str = ""
    is_live: bool = False

    @property
    def cost_estimate(self) -> float:
        """Rough cost estimate in USD. Uses Opus pricing as conservative estimate."""
        # Claude Opus: $15/1M input, $75/1M output (approximate)
        input_cost = (self.total_input_tokens / 1_000_000) * 15
        output_cost = (self.total_output_tokens / 1_000_000) * 75
        return input_cost + output_cost

    @property
    def cost_display(self) -> str:
        c = self.cost_estimate
        if c < 0.01:
            return "<$0.01"
        return f"${c:.2f}"

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

                # Extract session ID
                if data.get("sessionId") and not session.session_id:
                    session.session_id = data["sessionId"]

                # Track timestamps
                ts = data.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                # Extract usage from assistant messages
                if msg_type == "assistant" and "message" in data:
                    session.message_count += 1
                    msg = data["message"]
                    usage = msg.get("usage", {})
                    session.total_input_tokens += usage.get("input_tokens", 0)
                    session.total_input_tokens += usage.get("cache_creation_input_tokens", 0)
                    session.total_input_tokens += usage.get("cache_read_input_tokens", 0)
                    session.total_output_tokens += usage.get("output_tokens", 0)
                    if not session.model and msg.get("model"):
                        session.model = msg["model"]

            session.started_at = first_ts or ""
            session.last_activity = last_ts or first_ts or ""

    except (OSError, json.JSONDecodeError):
        pass

    return session


def get_live_session_ids() -> set[str]:
    """Read ~/.claude/sessions/*.json to find currently-running session IDs.

    Each file contains a JSON object with pid, sessionId, cwd, startedAt.
    We verify the PID is still alive before considering it live.
    """
    live: set[str] = set()
    if not CLAUDE_SESSIONS_DIR.exists():
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
            except (OSError, ProcessLookupError):
                pass
        except (OSError, json.JSONDecodeError):
            continue

    return live


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
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    live_ids = get_live_session_ids()
    sessions: list[ClaudeSession] = []

    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in proj_dir.name.lower():
            continue

        for jsonl_file in proj_dir.glob("*.jsonl"):
            if jsonl_file.name.endswith(".wakatime"):
                continue
            session = parse_session(jsonl_file)
            if session and session.message_count >= min_messages:
                session.is_live = session.session_id in live_ids
                sessions.append(session)

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
