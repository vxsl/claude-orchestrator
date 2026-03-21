"""File watcher for Claude session directories.

Uses watchdog (cross-platform: inotify on Linux, FSEvents on macOS, ReadDirectoryChanges on Windows)
to detect new/modified sessions in real time instead of polling.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


class _DebouncedHandler(FileSystemEventHandler):
    """Fires callback at most once per `debounce` seconds, on relevant file changes."""

    def __init__(self, callback: Callable[[], None], debounce: float = 1.0):
        super().__init__()
        self._callback = callback
        self._debounce = debounce
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
        self._callback()

    def _is_relevant(self, event: FileSystemEvent) -> bool:
        path = event.src_path
        # New/modified JSONL session files
        if path.endswith(".jsonl") and not path.endswith(".wakatime"):
            return True
        # Live session markers
        if path.endswith(".json") and CLAUDE_SESSIONS_DIR.as_posix() in path:
            return True
        # New project subdirectory created
        if event.is_directory:
            return True
        return False

    def on_created(self, event: FileSystemEvent):
        if self._is_relevant(event):
            self._schedule()

    def on_modified(self, event: FileSystemEvent):
        if self._is_relevant(event):
            self._schedule()

    def on_deleted(self, event: FileSystemEvent):
        if self._is_relevant(event):
            self._schedule()


class SessionWatcher:
    """Watch Claude session directories for changes and invoke a callback.

    Usage:
        watcher = SessionWatcher(on_change=my_callback)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self, on_change: Callable[[], None], debounce: float = 1.0):
        self._handler = _DebouncedHandler(on_change, debounce=debounce)
        self._observer = Observer()
        self._started = False

    def start(self):
        if self._started:
            return

        # Watch projects dir (recursive — subdirs contain JSONL files)
        if CLAUDE_PROJECTS_DIR.exists():
            self._observer.schedule(self._handler, str(CLAUDE_PROJECTS_DIR), recursive=True)

        # Watch sessions dir (flat — JSON liveness markers)
        if CLAUDE_SESSIONS_DIR.exists():
            self._observer.schedule(self._handler, str(CLAUDE_SESSIONS_DIR), recursive=False)

        self._observer.daemon = True
        self._observer.start()
        self._started = True

    def stop(self):
        if not self._started:
            return
        self._observer.stop()
        self._observer.join(timeout=2)
        self._started = False
