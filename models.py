"""Data models for the orchestrator."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


class Category(str, Enum):
    WORK = "work"
    PERSONAL = "personal"


CATEGORY_COLORS = {
    Category.WORK: "dodger_blue1",
    Category.PERSONAL: "medium_purple",
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
    report: str = ""  # implementer's writeback (auto-mode loop)


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
    category: Category = Category.PERSONAL
    links: list[Link] = field(default_factory=list)
    notes: str = ""
    todos: list[TodoItem] = field(default_factory=list)
    archived: bool = False
    thread_ids: list[str] = field(default_factory=list)
    archived_thread_ids: list[str] = field(default_factory=list)  # deprecated, kept for compat
    archived_sessions: dict[str, str] = field(default_factory=dict)  # session_id → archived_at ISO timestamp
    shelved_sessions: dict[str, str] = field(default_factory=dict)  # session_id → shelved_at ISO timestamp
    deleted_sessions: dict[str, str] = field(default_factory=dict)  # session_id → deleted_at ISO timestamp (trash)
    repo_path: str = ""  # e.g. "/home/kyle/dev/claude-orchestrator"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_user_activity: str = ""  # timestamp of last user message (for stable sorting)
    auto_done_reason: str = ""  # set by `orch distill done` to signal auto-mode loop should exit
    auto_next_todo_ids: list[str] = field(default_factory=list)  # set by `orch distill next` to dispatch one or more pending todos (concurrent batch when >1)
    auto_dispatched_todo_ids: list[str] = field(default_factory=list)  # todo IDs the active loop has already dispatched (in-memory skip set, persisted so CLI can refuse re-dispatch instead of silently dropping it)
    # ── Persisted auto-mode runtime state ────────────────────────────
    # These fields let a second orch instance (or the CLI over ssh) see
    # and control an active loop without sharing the owner's memory.
    # The owning orch process is the only writer for everything EXCEPT
    # auto_cancel_requested, which any process can set; the owner polls
    # it and self-cancels.
    auto_running: bool = False             # true while a loop is active in some process
    auto_pid: int = 0                      # PID of the owning orch process (for stale-state detection)
    auto_started_at: str = ""              # ISO timestamp of the active loop's start
    auto_iteration: int = 0                # last-completed iteration count
    auto_current_todo_id: str = ""         # first todo of the current batch (informational)
    auto_coord_sid: str = ""               # coordinator's tmux session id
    auto_impl_sids: list[str] = field(default_factory=list)  # implementer tmux session ids spawned this run
    auto_cancel_requested: bool = False    # set by any process; owner polls and exits

    def __post_init__(self):
        # Sanitize name: strip whitespace, fix "UB-XXXX: UB-XXXX" redundancy
        if self.name:
            self.name = self.name.strip()
            # Fix "TICKET: TICKET" pattern (e.g. "UB-6636: UB-6636")
            if ": " in self.name:
                prefix, suffix = self.name.split(": ", 1)
                if suffix.strip() == prefix.strip():
                    self.name = prefix.strip()
        if self.description:
            self.description = self.description.strip()

    # ── Transient enrichment fields (NOT persisted) ──
    ticket_key: str = field(default="", repr=False)           # e.g. "UB-1234", from branch name
    ticket_summary: str = field(default="", repr=False)       # from Jira cache
    ticket_status: str = field(default="", repr=False)        # from Jira cache
    mr_url: str = field(default="", repr=False)               # from MR cache
    ticket_solve_status: str = field(default="", repr=False)  # from ticket-solve cache

    def touch(self):
        self.updated_at = datetime.now().isoformat()

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
        return not self.archived

    @property
    def auto_pid_alive(self) -> bool:
        """True if `auto_pid` belongs to a running process on this host.

        Used by the UI to distinguish "loop is running" from "loop's owner
        crashed and left auto_running=True stuck on." Cheap — a single
        signal-0 syscall. Cross-host: meaningless if auto_pid was set on
        a different host, but that case isn't supported yet anyway.
        """
        import os as _os
        if not self.auto_running or self.auto_pid <= 0:
            return False
        try:
            _os.kill(self.auto_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

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
        d["category"] = self.category.value
        for k in self._TRANSIENT_FIELDS:
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Workstream:
        d = dict(d)
        # Migration: "meta" category removed — map to "work"
        raw_cat = d.get("category", "personal")
        if raw_cat == "meta":
            raw_cat = "work"
        d["category"] = Category(raw_cat)
        d["links"] = [Link(**lnk) for lnk in d.get("links", [])]
        # Migration: add fields that may not exist in old data
        d.setdefault("archived", False)
        d.pop("status", None)  # removed field — ignore from old data
        d.pop("status_changed_at", None)  # removed field
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
        # Migrate deferred_sessions → shelved_sessions
        if "deferred_sessions" in d and "shelved_sessions" not in d:
            d["shelved_sessions"] = d.pop("deferred_sessions")
        d.setdefault("shelved_sessions", {})
        d.setdefault("deleted_sessions", {})
        d.setdefault("last_user_activity", "")
        d.setdefault("repo_path", "")
        d.setdefault("todos", [])
        d.setdefault("auto_done_reason", "")
        # Migrate legacy scalar auto_next_todo_id (string) → auto_next_todo_ids (list).
        legacy_next = d.pop("auto_next_todo_id", "")
        if "auto_next_todo_ids" not in d:
            d["auto_next_todo_ids"] = [legacy_next] if legacy_next else []
        d.setdefault("auto_dispatched_todo_ids", [])
        # New persisted runtime-state fields — default everything to inactive.
        d.setdefault("auto_running", False)
        d.setdefault("auto_pid", 0)
        d.setdefault("auto_started_at", "")
        d.setdefault("auto_iteration", 0)
        d.setdefault("auto_current_todo_id", "")
        d.setdefault("auto_coord_sid", "")
        d.setdefault("auto_impl_sids", [])
        d.setdefault("auto_cancel_requested", False)
        todos = []
        for t in d["todos"]:
            if isinstance(t, dict):
                t.setdefault("origin", "manual")
                t.setdefault("report", "")
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
    """JSON file store with cross-process concurrency safety.

    Concurrency model: every save acquires an exclusive fcntl flock on a
    sibling lock file, re-reads disk under the lock, merges external
    changes into in-memory state, then writes the result via atomic rename.
    This prevents two processes (or two Store instances in the same
    process) from clobbering each other's writes.

    Merge semantics:
      - Workstreams added on disk we never saw → pulled into memory.
      - Workstreams in memory we know we deleted (via `remove`) → stay
        deleted; not resurrected.
      - Todos added on disk we never saw → pulled into the matching ws.
      - Per-todo `done` and `report` are forward-only: if disk has them
        set and our in-memory copy doesn't, disk wins. This protects an
        implementer's writeback from being clobbered by a stale coordinator.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / "dev" / "claude-orchestrator" / "data.json"
        self.workstreams: list[Workstream] = []
        self._known_todo_ids: set[str] = set()  # todo IDs seen at last load/save
        self._known_ws_ids: set[str] = set()    # ws IDs seen at last load/save
        self._removed_ws_ids: set[str] = set()  # ws IDs explicitly removed since last load
        self._loaded_mtime: float = 0.0  # mtime at last successful load
        self.load()

    @contextmanager
    def _flock(self):
        """Exclusive lock for save() — serializes concurrent writers.

        The lock file is a sibling, not data.json itself. data.json is
        replaced via atomic rename on each save, which would invalidate
        a flock held directly on it. The sibling persists across renames.
        """
        lock_path = self.path.parent / f".{self.path.name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def load(self, *, force: bool = False):
        """Load workstreams from disk. Skips re-read when mtime is unchanged."""
        if not self.path.exists():
            self.workstreams = []
            self._known_todo_ids = set()
            self._known_ws_ids = set()
            self._removed_ws_ids = set()
            self._loaded_mtime = 0.0
            return
        try:
            current_mtime = self.path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        if not force and current_mtime == self._loaded_mtime and self.workstreams:
            return  # disk hasn't changed since our last load
        try:
            data = json.loads(self.path.read_text())
            self.workstreams = [Workstream.from_dict(w) for w in data.get("workstreams", [])]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"Warning: could not load {self.path}: {e}")
            self.workstreams = []
        self._known_todo_ids = {t.id for ws in self.workstreams for t in ws.todos}
        self._known_ws_ids = {ws.id for ws in self.workstreams}
        self._removed_ws_ids = set()
        self._loaded_mtime = current_mtime

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._flock():
            self._merge_external_state()
            data = {"workstreams": [w.to_dict() for w in self.workstreams]}
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, self.path)
        self._known_todo_ids = {t.id for ws in self.workstreams for t in ws.todos}
        self._known_ws_ids = {ws.id for ws in self.workstreams}
        self._removed_ws_ids = set()  # everything written now reflects intent
        try:
            self._loaded_mtime = self.path.stat().st_mtime
        except OSError:
            self._loaded_mtime = 0.0

    def _merge_external_state(self):
        """Read disk under flock; pull in external changes before writing.

        Three classes of external state we preserve:
          - Workstreams added by other processes (not in our memory, not
            in our _known_ws_ids, not in _removed_ws_ids → pull in).
          - Todos added by other processes to a workstream we share.
          - Monotonic completion on shared todos: disk's `done=True` or
            non-empty `report` wins over our in-memory empty values
            (defends implementer writeback against coordinator clobber).

        Workstreams we explicitly removed (in _removed_ws_ids) stay
        removed — disk's copy is NOT resurrected. Workstreams we never
        explicitly removed but are missing from our memory ARE pulled
        back from disk — that's the defense against the wholesale-stub-
        clobber pattern (a stale-snapshot save would otherwise wipe
        every workstream we didn't directly mutate).

        Optimization: when disk's mtime matches our last load/save mtime,
        no external writer has touched the file, so nothing to merge.
        Skipping the merge in that case also prevents our own monotonic
        forward rule from undoing legitimate in-process toggles (e.g.
        a todo marked done then un-done by the same Store instance).
        """
        if not self.path.exists():
            return
        try:
            current_mtime = self.path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        if current_mtime == self._loaded_mtime and self._loaded_mtime != 0.0:
            return  # disk unchanged since our last touch — nothing external to pull in
        try:
            disk_data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        disk_ws_list = disk_data.get("workstreams", [])
        disk_ws_map = {wd.get("id", ""): wd for wd in disk_ws_list if wd.get("id")}

        # 1) Pull in workstreams that exist on disk but not in memory,
        #    unless we explicitly removed them.
        mem_ws_ids = {ws.id for ws in self.workstreams}
        for wid, wd in disk_ws_map.items():
            if wid in mem_ws_ids:
                continue
            if wid in self._removed_ws_ids:
                continue
            try:
                new_ws = Workstream.from_dict(wd)
            except (KeyError, TypeError, ValueError):
                continue
            self.workstreams.append(new_ws)
            self._known_ws_ids.add(wid)
            for t in new_ws.todos:
                self._known_todo_ids.add(t.id)

        # 2) For workstreams in both memory and disk: merge todos.
        for ws in self.workstreams:
            disk_wd = disk_ws_map.get(ws.id)
            if not disk_wd:
                continue
            mem_todos_by_id = {t.id: t for t in ws.todos}
            for td in disk_wd.get("todos", []):
                if not isinstance(td, dict) or not td.get("id"):
                    continue
                tid = td["id"]
                if tid in mem_todos_by_id:
                    # Field-level merge for forward-only completion.
                    mem_t = mem_todos_by_id[tid]
                    if td.get("done") and not mem_t.done:
                        mem_t.done = True
                    disk_report = td.get("report", "") or ""
                    if disk_report and not (mem_t.report or ""):
                        mem_t.report = disk_report
                    continue
                # Disk has a todo we don't. Pull in unless we explicitly
                # forgot it (was known at load, then removed in memory).
                if tid in self._known_todo_ids:
                    continue
                td.setdefault("origin", "manual")
                try:
                    ws.todos.append(TodoItem(**td))
                except (KeyError, TypeError):
                    continue
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
        self._removed_ws_ids.add(ws_id)
        self.workstreams = [w for w in self.workstreams if w.id != ws_id]
        self.save()

    def prune_orphan_archived(self) -> int:
        """Drop archived workstreams whose only links are to deleted paths and
        which carry no user content (notes/todos/description/session refs).

        Defensive against the prunable-worktree-runaway pattern that bloated
        data.json with tens of thousands of empty placeholders. Returns the
        number of workstreams dropped. Caller is responsible for calling save().
        """
        def is_orphan(w: Workstream) -> bool:
            if not w.archived:
                return False
            if w.thread_ids or w.notes or w.todos:
                return False
            if w.description or w.archived_sessions:
                return False
            if w.shelved_sessions or w.deleted_sessions:
                return False
            wt_links = [l for l in w.links if l.kind in ("worktree", "file")]
            if not wt_links:
                return False
            for link in wt_links:
                expanded = os.path.expanduser(link.value).rstrip("/")
                if os.path.isdir(expanded):
                    return False  # at least one path still exists
            return True

        before = len(self.workstreams)
        self.workstreams = [w for w in self.workstreams if not is_orphan(w)]
        return before - len(self.workstreams)

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
                # Additive merge: preserve session state entries from the current
                # in-memory ws that the incoming ws doesn't have.  This protects
                # against stale/ghost screens (which hold old ws objects) clobbering
                # archive/shelve/delete state that was written after they were created.
                #
                # When the user explicitly un-archives a session, `existing` IS `ws`
                # (same Python object, since the previous update() set workstreams[i]=ws),
                # so the merge loop sees the same dict on both sides and adds nothing.
                # Only stale screens (different object, missing entries) get merged.
                if existing is not ws:
                    for field in ("archived_sessions", "shelved_sessions", "deleted_sessions"):
                        existing_dict: dict = getattr(existing, field, {})
                        ws_dict: dict = getattr(ws, field, {})
                        for sid, ts in existing_dict.items():
                            if sid not in ws_dict:
                                ws_dict[sid] = ts
                self.workstreams[i] = ws
                break
        self.save()

    def by_category(self, cat: Category) -> list[Workstream]:
        return [w for w in self.workstreams if w.category == cat]

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
        stale_only: bool = False,
        search: str = "",
        include_archived: bool = False,
    ) -> list[Workstream]:
        """Flexible filtering."""
        streams = self.workstreams if include_archived else self.active
        if category:
            streams = [w for w in streams if w.category == category]
        if stale_only:
            streams = [w for w in streams if w.is_stale]
        if search:
            q = search.lower()
            streams = [w for w in streams if q in w.name.lower() or q in w.description.lower()]
        return streams

    def sorted(
        self,
        streams: list[Workstream],
        sort_by: str = "updated",
    ) -> list[Workstream]:
        """Sort workstreams."""
        if sort_by == "updated":
            return sorted(streams, key=lambda w: w.updated_at, reverse=True)
        elif sort_by == "created":
            return sorted(streams, key=lambda w: w.created_at, reverse=True)
        elif sort_by == "category":
            return sorted(streams, key=lambda w: (w.category.value, w.updated_at), reverse=True)
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

