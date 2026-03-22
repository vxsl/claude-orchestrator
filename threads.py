"""Auto-threading — cluster Claude sessions into logical threads of thought.

Uses structural signals (project path, time proximity, git branch) to group
sessions without any LLM calls. Manual workstreams get 'pinned' status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from sessions import (
    ClaudeSession,
    discover_sessions,
    CLAUDE_PROJECTS_DIR,
)


# ─── Thread activity states ──────────────────────────────────────────

class ThreadActivity(Enum):
    """Observable activity state of a thread."""
    THINKING = "thinking"              # Claude is actively processing
    AWAITING_INPUT = "awaiting_input"  # Your turn (live session, turn finished)
    RESPONSE_FRESH = "response_fresh"  # Your turn (response within 30 min)
    RESPONSE_READY = "response_ready"  # Your turn (older response)
    IDLE = "idle"                      # Nothing pending


# Priority ordering (lower = more urgent)
_ACTIVITY_PRIORITY = {
    ThreadActivity.THINKING: 0,
    ThreadActivity.AWAITING_INPUT: 1,
    ThreadActivity.RESPONSE_FRESH: 2,
    ThreadActivity.RESPONSE_READY: 3,
    ThreadActivity.IDLE: 4,
}

# Tools that block on user input (polls, questions, plan confirmations)
_INTERACTIVE_TOOLS = frozenset({
    "AskUserQuestion",
    "ExitPlanMode",
})

# How long a finished response is considered "fresh"
FRESH_THRESHOLD = timedelta(minutes=30)

# Cache file for last-seen timestamps per thread
LAST_SEEN_FILE = Path.home() / ".cache" / "claude-orchestrator" / "last-seen.json"


def load_last_seen() -> dict[str, str]:
    """Load {thread_id: iso_timestamp} from cache."""
    try:
        return json.loads(LAST_SEEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_last_seen(data: dict[str, str]) -> None:
    """Persist last-seen timestamps."""
    LAST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(json.dumps(data))


def mark_thread_seen(thread_id: str) -> None:
    """Record that the user has seen this thread right now."""
    data = load_last_seen()
    data[thread_id] = datetime.now().astimezone().isoformat()
    save_last_seen(data)


def session_activity(session: ClaudeSession, last_seen: dict[str, str] | None = None) -> ThreadActivity:
    """Compute activity state for a single session.

    The live/not-live distinction is an implementation detail — users just
    care about: is Claude thinking, is it my turn, or is nothing happening?
    """
    # ── THINKING: live session, Claude is mid-turn ──────────────────
    if session.is_live:
        turn_done = (
            not session.last_message_role          # no messages yet
            or session.turn_complete
            or (session.last_stop_reason and session.last_stop_reason != "tool_use")
            or (session.last_stop_reason == "tool_use"
                and session.last_tool_name in _INTERACTIVE_TOOLS)
        )
        if not turn_done:
            return ThreadActivity.THINKING

    # ── YOUR TURN: live session, Claude's turn is done ────────────────
    # Most urgent — the session is open right now.
    if session.is_live:
        return ThreadActivity.AWAITING_INPUT

    # ── YOUR TURN: non-live, Claude left a response ─────────────────
    if session.last_message_role != "assistant":
        return ThreadActivity.IDLE

    # Check last-seen — if user already viewed this response, dim it
    if last_seen:
        seen_ts = last_seen.get(session.session_id, "")
        if seen_ts:
            try:
                seen_dt = datetime.fromisoformat(seen_ts.replace("Z", "+00:00"))
                activity_dt = datetime.fromisoformat(
                    (session.last_activity or "").replace("Z", "+00:00")
                )
                if seen_dt >= activity_dt:
                    return ThreadActivity.IDLE
            except (ValueError, TypeError):
                pass

    # Fresh vs ready (both mean "your turn", just different visual urgency)
    try:
        activity_dt = datetime.fromisoformat(
            (session.last_activity or "").replace("Z", "+00:00")
        )
        age = datetime.now().astimezone() - activity_dt
        if age <= FRESH_THRESHOLD:
            return ThreadActivity.RESPONSE_FRESH
    except (ValueError, TypeError):
        pass

    return ThreadActivity.RESPONSE_READY

# Clustering thresholds
# Only used for default-branch sessions (master/main). Feature branches always merge.
DEFAULT_BRANCH_GAP = timedelta(minutes=30)
DEFAULT_BRANCHES = frozenset({"master", "main", "HEAD", ""})


@dataclass
class Thread:
    """An auto-discovered cluster of related Claude sessions."""
    thread_id: str                        # Derived from first session ID
    name: str                             # Best-effort heuristic name
    project_path: str                     # Common project path
    sessions: list[ClaudeSession] = field(default_factory=list)
    pinned: bool = False                  # True if manually created workstream
    pinned_ws_id: str = ""                # Link back to manual workstream
    ai_title: str = ""                    # AI-generated title (from Sonnet)
    ai_category: str = ""                 # AI-generated category

    _last_seen: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def is_live(self) -> bool:
        return any(s.is_live for s in self.sessions)

    @property
    def activity(self) -> ThreadActivity:
        """Compute the current activity state of this thread.

        Uses the best (most urgent) activity across all sessions — the
        live/not-live split is handled inside session_activity.
        """
        if not self.sessions:
            return ThreadActivity.IDLE

        best = ThreadActivity.IDLE
        for s in self.sessions:
            act = session_activity(s, self._last_seen)
            if _ACTIVITY_PRIORITY.get(act, 99) < _ACTIVITY_PRIORITY.get(best, 99):
                best = act
        return best

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def last_activity(self) -> str:
        if not self.sessions:
            return ""
        return max(s.last_activity or s.started_at or "" for s in self.sessions)

    @property
    def last_user_activity(self) -> str:
        """Timestamp of last user message across all sessions."""
        if not self.sessions:
            return ""
        vals = [s.last_user_message_at for s in self.sessions if s.last_user_message_at]
        return max(vals) if vals else self.started_at

    @property
    def started_at(self) -> str:
        if not self.sessions:
            return ""
        return min(s.started_at or s.last_activity or "" for s in self.sessions)

    @property
    def total_messages(self) -> int:
        return sum(s.message_count for s in self.sessions)

    @property
    def total_tokens(self) -> int:
        return sum(s.total_input_tokens + s.total_output_tokens for s in self.sessions)

    @property
    def tokens_display(self) -> str:
        t = self.total_tokens
        if t > 1_000_000:
            return f"{t / 1_000_000:.1f}M"
        if t > 1_000:
            return f"{t / 1_000:.1f}k"
        return str(t)

    @property
    def models(self) -> list[str]:
        seen = []
        for s in self.sessions:
            if s.model and s.model not in seen:
                seen.append(s.model)
        return seen

    @property
    def age(self) -> str:
        """Human-readable time since last activity."""
        ts = self.last_activity
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

    @property
    def short_project(self) -> str:
        """Shortened project path for display."""
        home = str(Path.home())
        p = self.project_path
        if p.startswith(home):
            p = "~" + p[len(home):]
        # Show last 2 path components
        parts = p.rstrip("/").split("/")
        if len(parts) > 2:
            return "/".join(parts[-2:])
        return p

    @property
    def display_name(self) -> str:
        """Best name for this thread. AI title > heuristic name > project path."""
        if self.ai_title:
            return self.ai_title
        if self.name:
            return self.name
        return self.short_project


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _extract_git_branch(session: ClaudeSession) -> str:
    """Extract git branch from session's first few JSONL lines (cheap read)."""
    if not session.jsonl_path:
        return ""
    try:
        with open(session.jsonl_path) as f:
            for i, line in enumerate(f):
                if i > 10:  # Only check first 10 lines
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    branch = data.get("gitBranch", "")
                    if branch and branch != "HEAD":
                        return branch
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""


def _extract_first_message(session: ClaudeSession) -> str:
    """Extract first user message from session (cheap read)."""
    if not session.jsonl_path:
        return ""
    try:
        with open(session.jsonl_path) as f:
            for i, line in enumerate(f):
                if i > 20:  # Only check first 20 lines
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "user" and "message" in data:
                        content = data["message"].get("content", "")
                        if isinstance(content, str):
                            return content[:200]
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text and "[Request interrupted" not in text:
                                        return text[:200]
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""


def _should_merge(a: ClaudeSession, b: ClaudeSession,
                  branch_a: str, branch_b: str) -> bool:
    """Decide if two sessions in the same project should be in the same thread.

    Rules:
    - Same non-default branch → ALWAYS merge (feature branches are intentional)
    - Both on default branch + small time gap → merge (same work session)
    - Different branches → never merge
    """
    a_is_default = branch_a in DEFAULT_BRANCHES
    b_is_default = branch_b in DEFAULT_BRANCHES

    # Same non-default branch → always merge regardless of time
    if not a_is_default and not b_is_default and branch_a == branch_b:
        return True

    # Different non-default branches → never merge
    if not a_is_default and not b_is_default and branch_a != branch_b:
        return False

    # One is feature branch, other is default → don't merge
    if a_is_default != b_is_default:
        return False

    # Both on default branch → merge only if temporally close
    ts_a = _parse_ts(a.last_activity or a.started_at)
    ts_b = _parse_ts(b.started_at or b.last_activity)

    if ts_a is None or ts_b is None:
        return False

    return abs(ts_b - ts_a) <= DEFAULT_BRANCH_GAP


def _derive_thread_name(sessions: list[ClaudeSession],
                        branches: dict[str, str],
                        messages: dict[str, str]) -> str:
    """Derive a human-readable name for a thread from its sessions."""
    # Prefer custom title from any session
    for s in sessions:
        if s.title:
            return s.title

    # Use non-default branch name if consistent
    branch_names = set()
    for s in sessions:
        b = branches.get(s.session_id, "")
        if b and b not in ("master", "main", "HEAD"):
            branch_names.add(b)
    if len(branch_names) == 1:
        branch = branch_names.pop()
        # Clean up branch name: "UB-6668-implement-new-metric..." → "UB-6668"
        if len(branch) > 30:
            branch = branch[:30].rsplit("-", 1)[0]
        return branch

    # Use first user message (truncated)
    for s in sessions:
        msg = messages.get(s.session_id, "")
        if msg and len(msg) > 5:
            # Take first line, truncate
            first_line = msg.split("\n")[0].strip()
            if first_line.startswith("#"):
                first_line = first_line.lstrip("# ")
            if len(first_line) > 50:
                first_line = first_line[:47] + "..."
            return first_line

    return ""


def discover_threads(min_messages: int = 1) -> list[Thread]:
    """Discover and cluster all Claude sessions into threads.

    This is the main entry point. It:
    1. Discovers all sessions
    2. Groups by project path
    3. Sorts each group chronologically
    4. Merges adjacent sessions using time + branch heuristics
    5. Derives names for each thread

    Returns threads sorted by last activity (most recent first).
    """
    sessions = discover_sessions(min_messages=min_messages)
    if not sessions:
        return []

    last_seen = load_last_seen()

    # Group sessions by project path
    by_project: dict[str, list[ClaudeSession]] = {}
    for s in sessions:
        by_project.setdefault(s.project_path, []).append(s)

    # Extract git branches and first messages (cheap I/O)
    branches: dict[str, str] = {}
    messages: dict[str, str] = {}
    for s in sessions:
        branches[s.session_id] = _extract_git_branch(s)
        messages[s.session_id] = _extract_first_message(s)

    threads: list[Thread] = []

    for project_path, proj_sessions in by_project.items():
        # Sort chronologically
        proj_sessions.sort(key=lambda s: s.started_at or s.last_activity or "")

        # Greedy merge: walk through sorted sessions, extend current cluster
        # or start a new one
        clusters: list[list[ClaudeSession]] = []
        current: list[ClaudeSession] = []

        for s in proj_sessions:
            if not current:
                current = [s]
                continue

            # Check if this session should merge with the current cluster
            last_in_cluster = current[-1]
            branch_last = branches.get(last_in_cluster.session_id, "")
            branch_s = branches.get(s.session_id, "")

            if _should_merge(last_in_cluster, s, branch_last, branch_s):
                current.append(s)
            else:
                clusters.append(current)
                current = [s]

        if current:
            clusters.append(current)

        # Create Thread objects from clusters
        for cluster in clusters:
            thread_id = cluster[0].session_id  # Use first session ID
            name = _derive_thread_name(cluster, branches, messages)

            threads.append(Thread(
                thread_id=thread_id,
                name=name,
                project_path=project_path,
                sessions=cluster,
                _last_seen=last_seen,
            ))

    # Sort by last activity, most recent first. Live threads always on top.
    threads.sort(key=lambda t: t.last_activity or "", reverse=True)
    threads.sort(key=lambda t: t.is_live, reverse=True)

    return threads
