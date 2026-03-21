"""Notification feed — load, filter, and dismiss desktop notifications.

Pure Python module, no Textual dependency. Notifications are written by the
Claude Code stop hook as JSONL lines and read here for display in the TUI.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


CACHE_DIR = Path.home() / ".cache" / "claude-orchestrator"
NOTIFICATIONS_FILE = CACHE_DIR / "notifications.jsonl"
DISMISSED_FILE = CACHE_DIR / "notifications-dismissed.json"

# Don't load notifications older than this
MAX_AGE = timedelta(hours=72)

# Freshness thresholds for color coding
FRESH_THRESHOLD = timedelta(minutes=30)
RECENT_THRESHOLD = timedelta(hours=4)


@dataclass
class Notification:
    """A single desktop notification event."""
    id: str               # Stable hash-based ID
    timestamp: str        # ISO 8601
    cwd: str              # Project directory path
    title: str            # Project basename
    message: str          # Summary line from Claude
    session_id: str = ""  # Claude session ID (may be empty)
    dismissed: bool = False

    @property
    def dt(self) -> datetime:
        """Parse timestamp to datetime."""
        try:
            ts = self.timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @property
    def age(self) -> timedelta:
        """Age since notification fired."""
        return datetime.now(timezone.utc) - self.dt

    @property
    def freshness(self) -> str:
        """'fresh' (< 30 min), 'recent' (< 4 hours), or 'old'."""
        a = self.age
        if a <= FRESH_THRESHOLD:
            return "fresh"
        if a <= RECENT_THRESHOLD:
            return "recent"
        return "old"


def _notif_id(timestamp: str, cwd: str, message: str) -> str:
    """Generate a stable short ID from notification content."""
    raw = f"{timestamp}:{cwd}:{message}"
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def _load_dismissed() -> set[str]:
    """Load set of dismissed notification IDs."""
    try:
        data = json.loads(DISMISSED_FILE.read_text())
        return set(data) if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _save_dismissed(dismissed: set[str]) -> None:
    """Persist dismissed notification IDs."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DISMISSED_FILE.write_text(json.dumps(sorted(dismissed)))


def load_notifications(max_age: timedelta = MAX_AGE) -> list[Notification]:
    """Read notifications from JSONL, filtering by age and marking dismissed ones."""
    if not NOTIFICATIONS_FILE.exists():
        return []

    cutoff = datetime.now(timezone.utc) - max_age
    dismissed = _load_dismissed()
    notifications: list[Notification] = []

    for line in NOTIFICATIONS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = data.get("timestamp", "")
        cwd = data.get("cwd", "")
        message = data.get("message", "")
        nid = _notif_id(ts, cwd, message)

        notif = Notification(
            id=nid,
            timestamp=ts,
            cwd=cwd.rstrip("/"),
            title=data.get("title", ""),
            message=message,
            session_id=data.get("session_id", ""),
            dismissed=nid in dismissed,
        )

        if notif.dt >= cutoff:
            notifications.append(notif)

    # Newest first
    notifications.sort(key=lambda n: n.timestamp, reverse=True)
    return notifications


def notifications_for_dirs(
    notifications: list[Notification], dirs: set[str]
) -> list[Notification]:
    """Filter notifications matching any of the given directory paths."""
    normalized = {d.rstrip("/") for d in dirs}
    return [n for n in notifications if n.cwd in normalized]


def dismiss_notification(notif_id: str) -> None:
    """Mark a notification as dismissed."""
    dismissed = _load_dismissed()
    dismissed.add(notif_id)
    _save_dismissed(dismissed)


def undismiss_notification(notif_id: str) -> None:
    """Remove dismissal for a notification."""
    dismissed = _load_dismissed()
    dismissed.discard(notif_id)
    _save_dismissed(dismissed)


def dismiss_all_for_dirs(notifications: list[Notification], dirs: set[str]) -> None:
    """Dismiss all notifications matching the given directories."""
    matching = notifications_for_dirs(notifications, dirs)
    if not matching:
        return
    dismissed = _load_dismissed()
    for n in matching:
        dismissed.add(n.id)
    _save_dismissed(dismissed)
