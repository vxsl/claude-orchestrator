"""Bridge between the Rust session engine daemon and the Python TUI.

Provides a pipe-based notification channel so the TUI gets woken up
instantly when the Rust daemon writes new data to SQLite, without polling.

Usage in the Textual app:

    from session_bridge import SessionBridge

    bridge = SessionBridge()
    bridge.start(callback=self._on_sessions_changed)
    # ... later:
    bridge.stop()
"""

from __future__ import annotations

import os
import select
import threading
from typing import Callable, Optional


def _default_pipe_path() -> str:
    uid = os.getuid()
    return f"/tmp/orch-session-engine.{uid}.pipe"


class SessionBridge:
    """Listens on the Rust daemon's notification pipe and calls back on changes."""

    def __init__(self, pipe_path: Optional[str] = None):
        self.pipe_path = pipe_path or _default_pipe_path()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pipe_fd: Optional[int] = None

    @property
    def available(self) -> bool:
        """True if the notification pipe exists (daemon is running)."""
        return os.path.exists(self.pipe_path)

    def start(self, callback: Callable[[], None]) -> bool:
        """Start listening for notifications in a background thread.

        Returns True if the pipe was opened successfully.
        The callback will be invoked (from a background thread) each time
        the Rust daemon signals that new data is available.
        """
        if not self.available:
            return False

        self._stop_event.clear()

        try:
            # Open FIFO in read-only non-blocking mode
            self._pipe_fd = os.open(
                self.pipe_path, os.O_RDONLY | os.O_NONBLOCK
            )
        except OSError:
            return False

        self._thread = threading.Thread(
            target=self._listen_loop,
            args=(callback,),
            daemon=True,
            name="session-bridge",
        )
        self._thread.start()
        return True

    def stop(self):
        """Stop listening and close the pipe."""
        self._stop_event.set()
        if self._pipe_fd is not None:
            try:
                os.close(self._pipe_fd)
            except OSError:
                pass
            self._pipe_fd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _listen_loop(self, callback: Callable[[], None]):
        """Background thread: select() on pipe fd, invoke callback on data."""
        fd = self._pipe_fd
        if fd is None:
            return

        while not self._stop_event.is_set():
            try:
                # Wait up to 1s for data on the pipe
                ready, _, _ = select.select([fd], [], [], 1.0)
                if ready:
                    # Drain any pending bytes
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        # EOF — writer (Rust daemon) closed the pipe.
                        # Break to avoid busy-spinning on a dead FIFO.
                        break
                    callback()
            except (OSError, ValueError):
                break
