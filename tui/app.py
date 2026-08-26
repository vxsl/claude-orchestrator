"""App: the engine's asyncio shell — input loop, view stack, paint
scheduler, timers/workers, suspend, clipboard, crash contract.

Frame model: a batch of input events paints synchronously before control
returns to the loop (keystroke-to-screen = one iteration); everything else
requests a coalesced paint via call_soon. Painting is skipped entirely while
`ui_visible` is False and caught up (invalidate + full paint) on the
hidden→visible transition.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import traceback

from .frame import Frame, Painter
from .keys import InputDecoder, KeyEvent, MouseEvent, PasteEvent
from .layout import Rect
from .term import TermIO
from .view import Timer, View

ESCAPE_DELAY = 0.02  # lone-ESC vs alt/CSI grace (replaces TEXTUAL_ESCAPE_DELAY)
PANE_TICK_SECS = 0.05  # 20fps coalesced flush for terminal panes
VISIBILITY_POLL_SECS = 3.0


class App:
    def __init__(self, io=None, visibility_probe=None) -> None:
        # visibility_probe: optional () -> bool, polled every 3s into
        # ui_visible. Kept as an injected callable so the engine never
        # imports app modules (orch wires actions.ui_is_visible).
        self.io = io if io is not None else TermIO()
        self._visibility_probe = visibility_probe
        self.ui_visible = True
        self.current_frame: Frame | None = None  # last-painted frame

        self._stack: list[tuple[View, object]] = []  # (view, on_result)
        self._painter = Painter()
        self._decoder = InputDecoder()
        self._size: tuple[int, int] = (0, 0)
        self._paint_pending = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._done: asyncio.Future | None = None
        self._result = None
        self._started = False
        self._reader_fd: int | None = None
        self._esc_timer: asyncio.TimerHandle | None = None

        self._app_timers: list[Timer] = []
        self._bg_tasks: set[asyncio.Task] = set()
        self._exclusive: dict[str, asyncio.Task] = {}
        self._exclusive_gen: dict[str, int] = {}
        self._panes: set = set()
        self._pane_ticker: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────

    def run(self):
        return asyncio.run(self._main())

    async def _main(self):
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._done = loop.create_future()
        self.io.enter_raw_alt()
        try:
            fd = self.io.input_fileno()
            if fd is not None:
                self._reader_fd = fd
                loop.add_reader(fd, self._on_stdin_readable)
            try:
                loop.add_signal_handler(signal.SIGWINCH, self._on_resize)
            except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                pass  # fake loops / non-main thread / no SIGWINCH
            self._started = True
            if self._stack:
                self._stack[-1][0].on_show()  # views pushed before run()
            self._on_resize()  # initial size + first paint
            if self._visibility_probe is not None:
                self._spawn(self._poll_visibility())
            await self._done
            return self._result
        except Exception:
            # orch launcher crash contract: traceback lands in the crash
            # log, the process exits nonzero, the launcher restores + cats.
            crash_log = os.environ.get("ORCH_CRASH_LOG", "")
            if crash_log:
                with open(crash_log, "w") as f:
                    traceback.print_exc(file=f)
            raise
        finally:
            self.io.restore()
            self._shutdown(loop)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0)  # let cancelled tasks unwind

    def _shutdown(self, loop) -> None:
        if self._reader_fd is not None:
            with contextlib.suppress(Exception):
                loop.remove_reader(self._reader_fd)
            self._reader_fd = None
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(signal.SIGWINCH)
        if self._esc_timer is not None:
            self._esc_timer.cancel()
            self._esc_timer = None
        for view, _ in self._stack:
            view.cancel_timers()
        for timer in self._app_timers:
            timer.cancel()
        self._app_timers.clear()
        for task in list(self._bg_tasks):
            task.cancel()

    def exit(self, result=None) -> None:
        self._result = result
        if self._done is not None and not self._done.done():
            self._done.set_result(None)

    def _crash(self, exc: BaseException) -> None:
        """Route a callback exception into _main so the crash contract holds."""
        if self._done is None:
            raise exc
        if not self._done.done():
            self._done.set_exception(exc)

    # ── input ─────────────────────────────────────────────────────

    def _on_stdin_readable(self) -> None:
        try:
            data = os.read(self._reader_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:  # EOF: stop reading, keep the app running
            if self._reader_fd is not None and self._loop is not None:
                self._loop.remove_reader(self._reader_fd)
                self._reader_fd = None
            return
        self.feed(data)

    def feed(self, data: bytes) -> None:
        """Decode raw bytes and dispatch (the harness's byte entrypoint)."""
        if self._esc_timer is not None:  # more bytes: cancel the grace timer
            self._esc_timer.cancel()
            self._esc_timer = None
        try:
            self._dispatch_events(self._decoder.feed(data))
        except Exception as exc:
            self._crash(exc)
            return
        if self._decoder.pending_escape() and self._loop is not None:
            self._esc_timer = self._loop.call_later(ESCAPE_DELAY, self._flush_escape)

    def _flush_escape(self) -> None:
        self._esc_timer = None
        try:
            self._dispatch_events(self._decoder.flush_escape())
        except Exception as exc:
            self._crash(exc)

    def _dispatch_events(self, events) -> None:
        """Dispatch a batch, then paint synchronously if anything asked."""
        for ev in events:
            self._dispatch(ev)
        if self._paint_pending:
            self._paint()

    def _dispatch(self, ev) -> None:
        top = self.top
        if isinstance(ev, KeyEvent):
            if top is None or not top.on_key(ev):
                self.on_unhandled_key(ev)
        elif isinstance(ev, MouseEvent):
            if top is not None:
                top.on_mouse(ev)
        elif isinstance(ev, PasteEvent):
            if top is not None:
                top.on_paste(ev)

    def on_unhandled_key(self, ev) -> None:
        """App-level hook for keys no view consumed."""

    # ── view stack ────────────────────────────────────────────────

    @property
    def top(self) -> View | None:
        return self._stack[-1][0] if self._stack else None

    def push(self, view: View, on_result=None) -> None:
        prev = self.top
        if prev is not None and self._started:
            prev.on_hide()
        view.app = self
        self._stack.append((view, on_result))
        if self._started:
            if self._size != (0, 0):
                view.on_resize(Rect(0, 0, *self._size))
            view.on_show()
        self.request_paint()

    def pop(self, result=None) -> None:
        if not self._stack:
            return
        view, on_result = self._stack.pop()
        view.on_hide()
        view.cancel_timers()
        new_top = self.top
        if new_top is not None:
            new_top.on_show()
        if on_result is not None:
            on_result(result)  # after the view is off the stack
        if not self._stack:  # (the callback may have pushed something)
            self.exit(result)
        else:
            self.request_paint()

    def replace_top(self, view: View) -> None:
        """Swap the top view without firing on_result (tab switching). The
        outgoing view keeps its timers (paused) — it may be cached and
        re-pushed — and the slot's on_result carries over to the new view."""
        if not self._stack:
            self.push(view)
            return
        old, on_result = self._stack.pop()
        if self._started:
            old.on_hide()
        view.app = self
        self._stack.append((view, on_result))
        if self._started:
            if self._size != (0, 0):
                view.on_resize(Rect(0, 0, *self._size))
            view.on_show()
        self.request_paint()

    # ── painting ──────────────────────────────────────────────────

    def request_paint(self) -> None:
        if self._paint_pending:
            return
        self._paint_pending = True
        if self._loop is not None:
            self._loop.call_soon(self._scheduled_paint)

    def _scheduled_paint(self) -> None:
        if not self._paint_pending:
            return  # a dispatch batch already painted synchronously
        try:
            self._paint()
        except Exception as exc:
            self._crash(exc)

    def paint_now(self) -> None:
        self._paint()

    def _paint(self) -> None:
        self._paint_pending = False
        if not self.ui_visible:
            return  # caught up by _apply_visibility on the way back
        w, h = self._size
        if w <= 0 or h <= 0:
            return
        frame = Frame(w, h)
        start = 0
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0].opaque:
                start = i
                break
        rect = Rect(0, 0, w, h)
        for view, _ in self._stack[start:]:
            view.render(frame, rect)
        self.current_frame = frame
        data = self._painter.paint(frame)
        if data:  # empty when nothing changed (painter skipped the paint)
            self.io.write(data)

    def _on_resize(self) -> None:
        try:
            self._size = self.io.get_size()
        except OSError:
            return
        rect = Rect(0, 0, *self._size)
        try:
            for view, _ in self._stack:
                view.on_resize(rect)
            self._painter.invalidate()
            self._paint()
        except Exception as exc:
            self._crash(exc)

    # ── visibility gate ───────────────────────────────────────────

    async def _poll_visibility(self) -> None:
        while True:
            try:
                visible = bool(self._visibility_probe())
            except Exception:
                visible = True
            self._apply_visibility(visible)
            await asyncio.sleep(VISIBILITY_POLL_SECS)

    def _apply_visibility(self, visible: bool) -> None:
        was = self.ui_visible
        self.ui_visible = visible
        if visible and not was:
            self._painter.invalidate()
            self._paint()  # catch-up
            self.on_became_visible()

    def on_became_visible(self) -> None:
        """Hook: the terminal came back on screen after being hidden.

        Subclasses catch up on whatever their ticks skipped while hidden.
        The engine itself has already repainted by this point.
        """

    # ── terminal-pane ticker ──────────────────────────────────────

    def register_pane(self, pane) -> None:
        self._panes.add(pane)
        self._ensure_pane_ticker()

    def unregister_pane(self, pane) -> None:
        self._panes.discard(pane)

    def _ensure_pane_ticker(self) -> None:
        if self._panes and (self._pane_ticker is None or self._pane_ticker.done()):
            self._pane_ticker = self._spawn(self._pane_tick())

    async def _pane_tick(self) -> None:
        while self._panes:  # stops itself when the registry empties
            await asyncio.sleep(PANE_TICK_SECS)
            dirty = False
            for pane in list(self._panes):
                if getattr(pane, "has_dirty", False):
                    pane.has_dirty = False
                    dirty = True
            if dirty:
                self.request_paint()

    # ── timers & workers ──────────────────────────────────────────

    def set_interval(self, secs: float, fn) -> Timer:
        # App timers never auto-pause; fired/cancelled ones prune themselves.
        return Timer(secs, fn, repeat=True, registry=self._app_timers)

    def set_timer(self, secs: float, fn) -> Timer:
        return Timer(secs, fn, repeat=False, registry=self._app_timers)

    def every(self, secs: float, fn, *, thread: bool = False,
              jitter_start: float = 0.0) -> asyncio.Task:
        """Run fn every `secs` seconds forever (first run after jitter_start).
        thread=True runs fn via asyncio.to_thread; exceptions are logged to
        stderr and the loop continues."""

        async def runner():
            if jitter_start > 0:
                await asyncio.sleep(jitter_start)
            while True:
                try:
                    if thread:
                        await asyncio.to_thread(fn)
                    else:
                        fn()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                await asyncio.sleep(secs)

        return self._spawn(runner())

    def exclusive(self, group: str, coro) -> asyncio.Task:
        """Spawn coro, cancelling any prior task in `group`.

        Cancellation cannot reach a body already inside asyncio.to_thread:
        workers must capture gen(group) at start and drop their result if it
        changed by completion time.
        """
        prev = self._exclusive.get(group)
        if prev is not None and not prev.done():
            prev.cancel()
        self._exclusive_gen[group] = self._exclusive_gen.get(group, 0) + 1
        task = self._spawn(coro)
        self._exclusive[group] = task
        return task

    def gen(self, group: str) -> int:
        """Current generation of an exclusive group, for staleness checks."""
        return self._exclusive_gen.get(group, 0)

    def call_from_thread(self, fn, *args) -> None:
        self._loop.call_soon_threadsafe(fn, *args)

    def _spawn(self, coro) -> asyncio.Task:
        loop = self._loop or asyncio.get_running_loop()
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_task_done)
        return task

    def _bg_task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            traceback.print_exception(task.exception(), file=sys.stderr)

    # ── suspend ───────────────────────────────────────────────────

    @contextlib.contextmanager
    def suspend(self):
        """Leave the alt screen and restore cooked termios while the body
        runs a foreground child; re-enter and fully repaint after. Runs
        synchronously inside the loop by design — the body blocks the loop
        exactly like Textual's App.suspend."""
        loop = self._loop
        if loop is not None and self._reader_fd is not None:
            loop.remove_reader(self._reader_fd)
        if self._esc_timer is not None:
            self._esc_timer.cancel()
            self._esc_timer = None
        if self._pane_ticker is not None:
            self._pane_ticker.cancel()
            self._pane_ticker = None
        self.io.exit_alt_cooked()
        try:
            yield
        finally:
            self.io.enter_raw_alt()
            self._drain_stdin()
            if loop is not None and self._reader_fd is not None:
                loop.add_reader(self._reader_fd, self._on_stdin_readable)
            self._ensure_pane_ticker()
            self._painter.invalidate()
            self.paint_now()

    def _drain_stdin(self) -> None:
        """Discard bytes typed while suspended (they were for the child)."""
        fd = self.io.input_fileno()
        if fd is None:
            return
        try:
            os.set_blocking(fd, False)
            try:
                while os.read(fd, 4096):
                    pass
            finally:
                os.set_blocking(fd, True)
        except OSError:
            pass

    # ── misc ──────────────────────────────────────────────────────

    def copy_to_clipboard(self, text: str) -> None:
        self.io.copy_to_clipboard(text)

    def bell(self) -> None:
        self.io.write(b"\a")
