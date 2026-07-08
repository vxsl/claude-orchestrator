"""View base class + asyncio Timer for the tui engine."""

from __future__ import annotations

import asyncio
import sys
import traceback


class Timer:
    """Repeating or one-shot timer running on the asyncio loop.

    pause() takes effect immediately: a tick whose sleep completes while
    paused is skipped, and resume() waits a full interval before firing.
    Callback exceptions are logged to stderr and never kill the loop.

    Pass `registry` (a list) to have the timer append itself and remove
    itself once its task finishes — a fired one-shot or a cancelled timer
    never lingers in its owner's registry.
    """

    def __init__(self, secs: float, fn, *, repeat: bool,
                 registry: list | None = None) -> None:
        self._secs = secs
        self._fn = fn
        self._repeat = repeat
        self.run_when_hidden = False  # set by View.set_interval
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._task = asyncio.get_running_loop().create_task(self._run())
        if registry is not None:
            registry.append(self)
            self._task.add_done_callback(lambda _t: self._discard(registry))

    def _discard(self, registry: list) -> None:
        try:
            registry.remove(self)
        except ValueError:
            pass  # already cleared (cancel_timers / shutdown)

    async def _run(self) -> None:
        while True:
            await self._resumed.wait()
            await asyncio.sleep(self._secs)
            if not self._resumed.is_set():
                continue  # paused mid-sleep: skip this tick
            try:
                self._fn()
            except Exception:
                traceback.print_exc(file=sys.stderr)
            if not self._repeat:
                return

    def pause(self) -> None:
        self._resumed.clear()

    def resume(self) -> None:
        self._resumed.set()

    def cancel(self) -> None:
        self._task.cancel()


class View:
    """Base class for stackable views.

    The base on_show()/on_hide() resume/pause this view's timers —
    subclasses overriding them MUST call super().on_show()/on_hide().
    Timers require the running loop: create them in on_show or later.
    """

    opaque: bool = True  # False = the view below is rendered underneath

    def __init__(self) -> None:
        self.app = None  # set by App.push
        self._timers: list[Timer] = []

    # ── input: return True when the event was consumed ────────────

    def on_key(self, ev) -> bool:
        return False

    def on_mouse(self, ev) -> bool:
        return False

    def on_paste(self, ev) -> bool:
        return False

    # ── lifecycle ─────────────────────────────────────────────────

    def render(self, frame, rect) -> None:
        pass

    def on_resize(self, rect) -> None:
        pass

    def on_show(self) -> None:
        for timer in self._timers:
            timer.resume()

    def on_hide(self) -> None:
        for timer in self._timers:
            if not timer.run_when_hidden:
                timer.pause()

    # ── services ──────────────────────────────────────────────────

    def set_interval(self, secs: float, fn, run_when_hidden: bool = False) -> Timer:
        timer = Timer(secs, fn, repeat=True, registry=self._timers)
        timer.run_when_hidden = run_when_hidden
        return timer

    def set_timer(self, secs: float, fn) -> Timer:
        """One-shot timer (still paused/cancelled with the view); pruned
        from the view's registry once it fires."""
        return Timer(secs, fn, repeat=False, registry=self._timers)

    def cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def dismiss(self, result=None) -> None:
        self.app.pop(result)

    def request_paint(self) -> None:
        if self.app is not None:
            self.app.request_paint()
