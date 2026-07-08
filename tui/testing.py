"""Headless test harness: FakeTermIO + the Headless driver (replaces Pilot).

    async with Headless(App()) as h:
        h.app.push(view)
        await h.press("j", "ctrl+d")
        assert "row 2" in h.screen_text()
"""

from __future__ import annotations

import asyncio

from .app import App
from .keys import KeyEvent, key_name_for_char


class FakeTermIO:
    """Duck-typed TermIO for tests: fixed size, no input fd, records every
    write and mode call. Mutate .cols/.rows then call app._on_resize() to
    simulate a terminal resize."""

    def __init__(self, cols: int = 120, rows: int = 40) -> None:
        self.cols = cols
        self.rows = rows
        self.written = bytearray()
        self.calls: list[str] = []  # mode-method call record, in order
        self.clipboard: list[str] = []

    def enter_raw_alt(self) -> None:
        self.calls.append("enter_raw_alt")

    def exit_alt_cooked(self) -> None:
        self.calls.append("exit_alt_cooked")

    def restore(self) -> None:
        self.calls.append("restore")

    def write(self, data: bytes) -> None:
        self.written += data

    def get_size(self) -> tuple[int, int]:
        return (self.cols, self.rows)

    def input_fileno(self) -> None:
        return None  # headless: App skips the stdin reader

    def copy_to_clipboard(self, text: str) -> None:
        self.clipboard.append(text)


_NAMED_CHARS = {"space": " ", "enter": "\r", "tab": "\t"}


def make_key_event(name: str) -> KeyEvent:
    """Synthetic KeyEvent from a Textual-style key name ('j', 'up',
    'ctrl+d'). raw is b'' — synthetic events aren't PTY passthrough."""
    if len(name) == 1:
        return KeyEvent(key_name_for_char(name), name, b"")
    return KeyEvent(name, _NAMED_CHARS.get(name), b"")


class Headless:
    """Runs app._main() as a task with a FakeTermIO injected before start.
    `app` may be an App instance or a zero-arg factory."""

    def __init__(self, app, size: tuple[int, int] = (120, 40)) -> None:
        if not isinstance(app, App):
            app = app()
        self.app = app
        self.io = FakeTermIO(*size)
        app.io = self.io
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Headless:
        self._task = asyncio.get_running_loop().create_task(self.app._main())
        await self.pause()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.app.exit()
        try:
            await asyncio.wait_for(self._task, timeout=5)  # surfaces crashes
        finally:
            await asyncio.sleep(0)  # let cancelled background tasks unwind

    @property
    def top(self):
        return self.app.top

    async def press(self, *names: str) -> None:
        """Dispatch synthetic KeyEvents as one batch (paints synchronously)."""
        self.app._dispatch_events([make_key_event(name) for name in names])
        await self.pause()

    async def feed_bytes(self, data: bytes) -> None:
        """Bytes through the real decoder path, incl. escape-timer arming."""
        self.app.feed(data)
        await self.pause()

    def screen_text(self) -> str:
        frame = self.app.current_frame
        return "\n".join(frame.plain_lines()) if frame is not None else ""

    async def pause(self, delay: float = 0) -> None:
        """Sleep `delay`, then drain scheduled call_soon callbacks."""
        if delay:
            await asyncio.sleep(delay)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
