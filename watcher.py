"""File watcher for Claude session directories.

Uses watchdog (cross-platform: inotify on Linux, FSEvents on macOS, ReadDirectoryChanges on Windows)
to detect new/modified sessions in real time instead of polling.
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


class _LeadingEdgeDebounce:
    """Fires immediately on first call, then suppresses for `window` seconds."""

    def __init__(self, callback: Callable[[], None], window: float = 1.0):
        self._callback = callback
        self._window = window
        self._last_fire: float = 0
        self._lock = threading.Lock()
        self._trailing_timer: threading.Timer | None = None

    def __call__(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_fire
            if elapsed >= self._window:
                # Leading edge: fire immediately
                self._last_fire = now
                self._callback()
            else:
                # Within suppression window — schedule a trailing fire
                # so the last event in a burst isn't lost
                if self._trailing_timer is not None:
                    self._trailing_timer.cancel()
                remaining = self._window - elapsed
                self._trailing_timer = threading.Timer(remaining, self._trailing_fire)
                self._trailing_timer.daemon = True
                self._trailing_timer.start()

    def _trailing_fire(self):
        with self._lock:
            self._trailing_timer = None
            self._last_fire = time.monotonic()
        self._callback()


class _TrailingEdgeDebounce:
    """Fires after `window` seconds of quiet (resets on each call)."""

    def __init__(self, callback: Callable[[], None], window: float = 1.0):
        self._callback = callback
        self._window = window
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._window, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
        self._callback()


class _SplitHandler(FileSystemEventHandler):
    """Routes events to separate callbacks based on event type.

    - Session JSON markers (liveness): leading-edge debounce (fire immediately)
    - JSONL content changes: trailing-edge debounce (fire after quiet period)
    """

    def __init__(
        self,
        on_liveness: Callable[[], None],
        on_content: Callable[[], None],
        liveness_debounce: float = 1.0,
        content_debounce: float = 1.0,
    ):
        super().__init__()
        self._on_liveness = _LeadingEdgeDebounce(on_liveness, window=liveness_debounce)
        self._on_content = _TrailingEdgeDebounce(on_content, window=content_debounce)

    def _classify(self, event: FileSystemEvent) -> str | None:
        path = event.src_path
        # Session liveness markers
        if path.endswith(".json") and CLAUDE_SESSIONS_DIR.as_posix() in path:
            return "liveness"
        # JSONL session files
        if path.endswith(".jsonl") and not path.endswith(".wakatime"):
            return "content"
        # New project subdirectory
        if event.is_directory:
            return "content"
        return None

    def _dispatch(self, event: FileSystemEvent):
        kind = self._classify(event)
        if kind == "liveness":
            self._on_liveness()
        elif kind == "content":
            self._on_content()

    def on_created(self, event: FileSystemEvent):
        self._dispatch(event)

    def on_modified(self, event: FileSystemEvent):
        self._dispatch(event)

    def on_deleted(self, event: FileSystemEvent):
        self._dispatch(event)


class SessionWatcher:
    """Watch Claude session directories for changes and invoke callbacks.

    Two event channels:
    - on_liveness: Fires immediately when session JSON markers change
      (session start/stop). Leading-edge debounce — fast path.
    - on_content: Fires after JSONL content changes settle (1s quiet).
      Trailing-edge debounce — batches rapid writes.

    For backwards compat, on_change is used for both if on_liveness
    is not provided.
    """

    def __init__(
        self,
        on_change: Callable[[], None] | None = None,
        on_liveness: Callable[[], None] | None = None,
        on_content: Callable[[], None] | None = None,
        debounce: float = 1.0,
        content_debounce: float | None = None,
    ):
        liveness_cb = on_liveness or on_change or (lambda: None)
        content_cb = on_content or on_change or (lambda: None)
        self._handler = _SplitHandler(
            on_liveness=liveness_cb,
            on_content=content_cb,
            liveness_debounce=debounce,
            content_debounce=content_debounce if content_debounce is not None else debounce,
        )
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
