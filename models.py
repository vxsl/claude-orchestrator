"""Data models for the orchestrator."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


class Status(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in-progress"
    AWAITING_REVIEW = "awaiting-review"
    DONE = "done"
    BLOCKED = "blocked"


class Category(str, Enum):
    WORK = "work"
    PERSONAL = "personal"
    META = "meta"


STATUS_ICONS = {
    Status.QUEUED: "\u25cb",
    Status.IN_PROGRESS: "\u25cf",
    Status.AWAITING_REVIEW: "\u25c9",
    Status.DONE: "\u2713",
    Status.BLOCKED: "\u2717",
}

STATUS_COLORS = {
    Status.QUEUED: "dim",
    Status.IN_PROGRESS: "yellow",
    Status.AWAITING_REVIEW: "cyan",
    Status.DONE: "green",
    Status.BLOCKED: "red",
}

CATEGORY_COLORS = {
    Category.WORK: "dodger_blue1",
    Category.PERSONAL: "medium_purple",
    Category.META: "dim",
}

STATUS_ORDER = {
    Status.IN_PROGRESS: 0,
    Status.BLOCKED: 1,
    Status.AWAITING_REVIEW: 2,
    Status.QUEUED: 3,
    Status.DONE: 4,
}


@dataclass
class TodoItem:
    """A todo item — potential pending Claude session."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str = ""
    done: bool = False
    archived: bool = False
    context: str = ""  # extra instructions for spawning a session
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    origin: str = "manual"  # "manual" or "crystallized"


@dataclass
class Link:
    """A link to an external resource."""
    kind: str  # "worktree", "ticket", "claude-session", "slack", "file", "url"
    label: str
    value: str  # path, ticket ID, session ID, URL, etc.

    @property
    def display(self) -> str:
        kind_icons = {
            "worktree": "\U0001f333",
            "ticket": "\U0001f3ab",
            "claude-session": "\U0001f916",
            "slack": "\U0001f4ac",
            "file": "\U0001f4c4",
            "url": "\U0001f517",
        }
        icon = kind_icons.get(self.kind, "\u2022")
        return f"{icon} {self.label}: {self.value}"

    @property
    def is_openable(self) -> bool:
        return self.kind in ("url", "file", "worktree", "ticket")


@dataclass
class Workstream:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    status: Status = Status.QUEUED
    category: Category = Category.PERSONAL
    links: list[Link] = field(default_factory=list)
    notes: str = ""
    todos: list[TodoItem] = field(default_factory=list)
    archived: bool = False
    thread_ids: list[str] = field(default_factory=list)
    archived_thread_ids: list[str] = field(default_factory=list)  # deprecated, kept for compat
    archived_sessions: dict[str, str] = field(default_factory=dict)  # session_id → archived_at ISO timestamp
    repo_path: str = ""  # e.g. "/home/kyle/dev/claude-orchestrator"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_user_activity: str = ""  # timestamp of last user message (for stable sorting)
    status_changed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # ── Transient enrichment fields (NOT persisted) ──
    ticket_key: str = field(default="", repr=False)           # e.g. "UB-1234", from branch name
    ticket_summary: str = field(default="", repr=False)       # from Jira cache
    ticket_status: str = field(default="", repr=False)        # from Jira cache
    mr_url: str = field(default="", repr=False)               # from MR cache
    ticket_solve_status: str = field(default="", repr=False)  # from ticket-solve cache

    def touch(self):
        self.updated_at = datetime.now().isoformat()

    def set_status(self, new_status: Status):
        if new_status != self.status:
            self.status = new_status
            self.status_changed_at = datetime.now().isoformat()
            self.touch()

    @property
    def age(self) -> str:
        """Human-readable age since creation."""
        return _relative_time(self.created_at)

    @property
    def staleness(self) -> str:
        """Human-readable time since last update."""
        return _relative_time(self.updated_at)

    @property
    def is_stale(self) -> bool:
        """Not updated in >24h."""
        try:
            dt = datetime.fromisoformat(self.updated_at)
            return (datetime.now() - dt) > timedelta(hours=24)
        except (ValueError, TypeError):
            return False

    @property
    def is_active(self) -> bool:
        return self.status in (Status.IN_PROGRESS, Status.AWAITING_REVIEW)

    def add_link(self, kind: str, value: str, label: str = "") -> Link:
        if not label:
            label = kind
        link = Link(kind=kind, label=label, value=value)
        self.links.append(link)
        self.touch()
        return link

    # Transient fields excluded from persistence
    _TRANSIENT_FIELDS = frozenset({
        "ticket_key", "ticket_summary", "ticket_status",
        "mr_url", "ticket_solve_status",
    })

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["category"] = self.category.value
        for k in self._TRANSIENT_FIELDS:
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Workstream:
        d = dict(d)
        d["status"] = Status(d["status"])
        d["category"] = Category(d["category"])
        d["links"] = [Link(**lnk) for lnk in d.get("links", [])]
        # Migration: add fields that may not exist in old data
        d.setdefault("archived", False)
        d.setdefault("status_changed_at", d.get("updated_at", d.get("created_at", "")))
        d.pop("origin", None)
        # Strip transient enrichment fields if somehow present in saved data
        for k in cls._TRANSIENT_FIELDS:
            d.pop(k, None)
        d.setdefault("thread_ids", [])
        d.setdefault("archived_thread_ids", [])
        # Migrate archived_session_ids list → archived_sessions dict
        if "archived_session_ids" in d:
            old = d.pop("archived_session_ids")
            if old and "archived_sessions" not in d:
                d["archived_sessions"] = {sid: "" for sid in old}
        d.setdefault("archived_sessions", {})
        d.setdefault("last_user_activity", "")
        d.setdefault("repo_path", "")
        d.setdefault("todos", [])
        todos = []
        for t in d["todos"]:
            if isinstance(t, dict):
                t.setdefault("origin", "manual")
                todos.append(TodoItem(**t))
            else:
                todos.append(t)
        d["todos"] = todos
        return cls(**d)


def _relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now().astimezone()
        if dt.tzinfo is None:
            dt = dt.astimezone()
        delta = now - dt
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
        if days < 30:
            return f"{days}d ago"
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "unknown"


class Store:
    """Simple JSON file store with backup support."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / "dev" / "claude-orchestrator" / "data.json"
        self.workstreams: list[Workstream] = []
        self._known_todo_ids: set[str] = set()  # todo IDs seen at last load
        self.load()

    def load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.workstreams = [Workstream.from_dict(w) for w in data.get("workstreams", [])]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"Warning: could not load {self.path}: {e}")
                self.workstreams = []
        else:
            self.workstreams = []
        self._known_todo_ids = {t.id for ws in self.workstreams for t in ws.todos}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Merge in externally-added todos (e.g. from CLI crystallize) before writing,
        # so the in-memory state doesn't clobber them.
        self._merge_external_todos()
        data = {"workstreams": [w.to_dict() for w in self.workstreams]}
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        # Update known IDs so next save knows about everything we just wrote
        self._known_todo_ids = {t.id for ws in self.workstreams for t in ws.todos}

    def _merge_external_todos(self):
        """Merge todos added externally (by CLI) into in-memory workstreams.

        Only merges todos that are on disk but NOT in memory AND were NOT known
        at load time (i.e. they were added by another process after we loaded).
        Todos that were known at load but removed from memory (deleted by us)
        are not resurrected.
        """
        if not self.path.exists():
            return
        try:
            disk_data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        disk_ws_map = {}
        for wd in disk_data.get("workstreams", []):
            disk_ws_map[wd.get("id", "")] = wd
        for ws in self.workstreams:
            disk_wd = disk_ws_map.get(ws.id)
            if not disk_wd:
                continue
            mem_ids = {t.id for t in ws.todos}
            for td in disk_wd.get("todos", []):
                if not isinstance(td, dict) or not td.get("id"):
                    continue
                tid = td["id"]
                # Only merge if: not in memory AND not previously known (truly external)
                if tid not in mem_ids and tid not in self._known_todo_ids:
                    td.setdefault("origin", "manual")
                    ws.todos.append(TodoItem(**td))
                    self._known_todo_ids.add(tid)

    def backup(self) -> Path:
        """Create a timestamped backup of the data file."""
        if not self.path.exists():
            return self.path
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"data_{ts}.json"
        shutil.copy2(self.path, backup_path)
        # Keep only last 20 backups
        backups = sorted(backup_dir.glob("data_*.json"))
        for old in backups[:-20]:
            old.unlink()
        return backup_path

    def add(self, ws: Workstream) -> Workstream:
        self.workstreams.append(ws)
        self.save()
        return ws

    def remove(self, ws_id: str):
        self.backup()
        self.workstreams = [w for w in self.workstreams if w.id != ws_id]
        self.save()

    def get(self, ws_id: str) -> Optional[Workstream]:
        """Get by exact ID or prefix match."""
        # Exact match first
        exact = next((w for w in self.workstreams if w.id == ws_id), None)
        if exact:
            return exact
        # Prefix match
        matches = [w for w in self.workstreams if w.id.startswith(ws_id)]
        if len(matches) == 1:
            return matches[0]
        return None

    def update(self, ws: Workstream):
        ws.touch()
        for i, existing in enumerate(self.workstreams):
            if existing.id == ws.id:
                self.workstreams[i] = ws
                break
        self.save()

    def by_category(self, cat: Category) -> list[Workstream]:
        return [w for w in self.workstreams if w.category == cat]

    def by_status(self, status: Status) -> list[Workstream]:
        return [w for w in self.workstreams if w.status == status]

    # --- New query methods ---

    @property
    def active(self) -> list[Workstream]:
        """Non-archived workstreams."""
        return [w for w in self.workstreams if not w.archived]

    @property
    def archived(self) -> list[Workstream]:
        """Archived workstreams."""
        return [w for w in self.workstreams if w.archived]

    def search(self, query: str) -> list[Workstream]:
        """Search by name or description substring (case-insensitive)."""
        q = query.lower()
        return [w for w in self.active if q in w.name.lower() or q in w.description.lower()]

    def stale(self, hours: int = 24) -> list[Workstream]:
        """Workstreams not updated in the given hours."""
        cutoff = datetime.now() - timedelta(hours=hours)
        results = []
        for w in self.active:
            try:
                if datetime.fromisoformat(w.updated_at) < cutoff:
                    results.append(w)
            except (ValueError, TypeError):
                pass
        return results

    def filtered(
        self,
        category: Optional[Category] = None,
        status: Optional[Status] = None,
        active_only: bool = False,
        stale_only: bool = False,
        search: str = "",
        include_archived: bool = False,
    ) -> list[Workstream]:
        """Flexible filtering."""
        streams = self.workstreams if include_archived else self.active
        if category:
            streams = [w for w in streams if w.category == category]
        if status:
            streams = [w for w in streams if w.status == status]
        if active_only:
            streams = [w for w in streams if w.is_active]
        if stale_only:
            streams = [w for w in streams if w.is_stale]
        if search:
            q = search.lower()
            streams = [w for w in streams if q in w.name.lower() or q in w.description.lower()]
        return streams

    def sorted(
        self,
        streams: list[Workstream],
        sort_by: str = "status",
    ) -> list[Workstream]:
        """Sort workstreams."""
        if sort_by == "status":
            return sorted(streams, key=lambda w: (STATUS_ORDER.get(w.status, 99), w.updated_at))
        elif sort_by == "updated":
            return sorted(streams, key=lambda w: w.updated_at, reverse=True)
        elif sort_by == "created":
            return sorted(streams, key=lambda w: w.created_at, reverse=True)
        elif sort_by == "category":
            return sorted(streams, key=lambda w: (w.category.value, STATUS_ORDER.get(w.status, 99)))
        elif sort_by == "name":
            return sorted(streams, key=lambda w: w.name.lower())
        elif sort_by == "activity":
            return sorted(streams, key=lambda w: w.last_user_activity or w.updated_at, reverse=True)
        return streams

    def archive(self, ws_id: str) -> bool:
        ws = self.get(ws_id)
        if ws:
            ws.archived = True
            self.update(ws)
            return True
        return False

    def unarchive(self, ws_id: str) -> bool:
        ws = self.get(ws_id)
        if ws:
            ws.archived = False
            self.update(ws)
            return True
        return False

    def archive_done(self) -> int:
        """Archive all done workstreams. Returns count."""
        count = 0
        for ws in self.workstreams:
            if ws.status == Status.DONE and not ws.archived:
                ws.archived = True
                count += 1
        if count:
            self.save()
        return count
