"""App engine tests via the Headless harness: end-to-end keystrokes, view
stack semantics, escape-delay timing, exclusive workers, painter diff
integration, suspend mode sequencing, visibility gate, crash contract.

(Named test_engine_app.py to avoid confusion with tests/test_app.py.)
"""

import asyncio
import re

import pytest

from tui.app import App
from tui.testing import FakeTermIO, Headless, make_key_event
from tui.view import View

CUP_RE = re.compile(rb"\x1b\[(\d+);(\d+)H")


def cups(data: bytes) -> list[tuple[int, int]]:
    return [(int(r), int(c)) for r, c in CUP_RE.findall(data)]


class EchoView(View):
    """Renders the last key on row 0; q dismisses."""

    def __init__(self, label: str = "echo") -> None:
        super().__init__()
        self.label = label
        self.keys: list[str] = []
        self.pastes: list[str] = []
        self.mice: list = []

    def on_key(self, ev) -> bool:
        if ev.key == "q":
            self.dismiss("quit")
            return True
        self.keys.append(ev.key)
        self.request_paint()
        return True

    def on_paste(self, ev) -> bool:
        self.pastes.append(ev.text)
        return True

    def on_mouse(self, ev) -> bool:
        self.mice.append(ev)
        return True

    def render(self, frame, rect) -> None:
        last = self.keys[-1] if self.keys else "(none)"
        frame.write_markup(0, 0, rect.w, f"{self.label} last key: {last}")


class RowsView(View):
    """Static rows except row 2, which shows a key-press counter."""

    def __init__(self) -> None:
        super().__init__()
        self.counter = 0

    def on_key(self, ev) -> bool:
        self.counter += 1
        self.request_paint()
        return True

    def render(self, frame, rect) -> None:
        for y in range(rect.h):
            if y == 2:
                frame.write_markup(0, y, rect.w, f"counter: {self.counter}")
            else:
                frame.write_markup(0, y, rect.w, f"static row {y}")


# ── end-to-end input → screen ─────────────────────────────────────


@pytest.mark.asyncio
async def test_keystroke_updates_screen():
    async with Headless(App()) as h:
        h.app.push(EchoView())
        await h.pause()
        assert "echo last key: (none)" in h.screen_text()
        await h.press("j")
        assert "echo last key: j" in h.screen_text()
        await h.press("ctrl+d")
        assert "echo last key: ctrl+d" in h.screen_text()


@pytest.mark.asyncio
async def test_keystroke_paints_synchronously_no_timer():
    async with Headless(App()) as h:
        h.app.push(EchoView())
        await h.pause()
        h.app._dispatch_events([make_key_event("x")])
        # no await: the dispatch batch itself painted before returning
        assert "echo last key: x" in h.screen_text()


@pytest.mark.asyncio
async def test_feed_bytes_through_real_decoder():
    async with Headless(App()) as h:
        view = EchoView()
        h.app.push(view)
        await h.feed_bytes(b"j")
        assert view.keys == ["j"]


@pytest.mark.asyncio
async def test_paste_and_mouse_dispatch_to_top_view():
    async with Headless(App()) as h:
        view = EchoView()
        h.app.push(view)
        await h.feed_bytes(b"\x1b[200~hello\x1b[201~")
        assert view.pastes == ["hello"]
        await h.feed_bytes(b"\x1b[<0;5;3M")
        assert len(view.mice) == 1 and view.mice[0].kind == "press"


@pytest.mark.asyncio
async def test_unconsumed_key_falls_through_to_app_hook():
    class HookApp(App):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.unhandled = []

        def on_unhandled_key(self, ev):
            self.unhandled.append(ev.key)

    app = HookApp()
    async with Headless(app) as h:
        h.app.push(View())  # base View consumes nothing
        await h.press("z")
        assert app.unhandled == ["z"]


# ── view stack ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pop_fires_on_result_after_view_is_off_stack():
    async with Headless(App()) as h:
        base = EchoView("base")
        h.app.push(base)
        modal = EchoView("modal")
        seen = []
        h.app.push(modal, lambda res: seen.append((res, h.app.top is base)))
        await h.pause()
        modal.dismiss("picked")
        await h.pause()
        assert seen == [("picked", True)]  # callback ran with modal gone


@pytest.mark.asyncio
async def test_replace_top_swaps_without_firing_callback():
    async with Headless(App()) as h:
        base = EchoView("base")
        h.app.push(base)
        fired = []
        a, b = EchoView("a"), EchoView("b")
        h.app.push(a, fired.append)
        h.app.replace_top(b)
        await h.pause()
        assert fired == [] and h.app.top is b
        assert all(v is not a for v, _ in h.app._stack)
        b.dismiss("done")  # the slot's callback carried over to b
        await h.pause()
        assert fired == ["done"]


@pytest.mark.asyncio
async def test_view_interval_pauses_while_covered():
    async with Headless(App()) as h:
        base = EchoView("base")
        h.app.push(base)
        await h.pause()
        ticks = []
        base.set_interval(0.01, lambda: ticks.append(1))
        await h.pause(0.05)
        assert ticks
        modal = EchoView("modal")
        h.app.push(modal)  # base.on_hide pauses its interval immediately
        frozen = len(ticks)
        await h.pause(0.05)
        assert len(ticks) == frozen
        modal.dismiss()
        await h.pause(0.05)
        assert len(ticks) > frozen


@pytest.mark.asyncio
async def test_pop_last_view_exits_app_with_result():
    app = App()
    async with Headless(app) as h:
        view = EchoView()
        app.push(view)
        await h.pause()
        await h.press("q")  # dismisses the only view
        assert await asyncio.wait_for(h._task, 2) == "quit"


@pytest.mark.asyncio
async def test_opaque_modal_hides_base_but_overlay_does_not():
    async with Headless(App(), size=(30, 6)) as h:
        h.app.push(RowsView())
        await h.pause()
        assert "static row 0" in h.screen_text()

        class Overlay(View):
            opaque = False

            def render(self, frame, rect):
                frame.write_markup(0, rect.h - 1, rect.w, "overlay-line")

        overlay = Overlay()
        h.app.push(overlay)
        await h.pause()
        assert "static row 0" in h.screen_text()  # base still rendered
        assert "overlay-line" in h.screen_text()
        h.app.pop()
        modal = EchoView("modal")
        h.app.push(modal)  # opaque: base not rendered
        await h.pause()
        assert "static row 0" not in h.screen_text()
        assert "modal last key" in h.screen_text()


# ── escape-delay handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_lone_escape_waits_for_grace_timer():
    async with Headless(App()) as h:
        view = EchoView()
        h.app.push(view)
        await h.feed_bytes(b"\x1b")
        assert view.keys == []  # ambiguous: nothing dispatched yet
        await h.pause(0.05)  # > ESCAPE_DELAY
        assert view.keys == ["escape"]


@pytest.mark.asyncio
async def test_complete_csi_dispatches_immediately():
    async with Headless(App()) as h:
        view = EchoView()
        h.app.push(view)
        await h.feed_bytes(b"\x1b[A")
        assert view.keys == ["up"]  # no timer wait


@pytest.mark.asyncio
async def test_escape_timer_cancelled_by_following_bytes():
    async with Headless(App()) as h:
        view = EchoView()
        h.app.push(view)
        await h.feed_bytes(b"\x1b")
        await h.feed_bytes(b"[A")  # completes the CSI before the timer
        assert view.keys == ["up"]
        await h.pause(0.05)
        assert view.keys == ["up"]  # no stray escape afterwards


# ── workers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exclusive_generation_drops_superseded_results():
    async with Headless(App()) as h:
        app = h.app
        results = []
        started = asyncio.Event()

        async def worker(tag, gate=None):
            gen = app.gen("g")  # captured at start (spawn-time generation)
            if gate:
                gate.set()
            try:
                await asyncio.sleep(1 if gate else 0)
            except asyncio.CancelledError:
                pass  # simulate a to_thread body that completed anyway
            if gen == app.gen("g"):
                results.append(tag)

        app.exclusive("g", worker("old", started))
        await started.wait()
        new_task = app.exclusive("g", worker("new"))
        await new_task
        await h.pause()
        assert results == ["new"]


@pytest.mark.asyncio
async def test_every_worker_survives_exceptions(capsys):
    async with Headless(App()) as h:
        runs = []

        def job():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("worker-boom")

        task = h.app.every(0.01, job)
        await h.pause(0.08)
        task.cancel()
        assert len(runs) >= 2  # kept running after the exception
        assert "worker-boom" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_call_from_thread_runs_on_loop():
    async with Headless(App()) as h:
        import threading

        hits = []
        t = threading.Thread(
            target=h.app.call_from_thread, args=(hits.append, "ok")
        )
        t.start()
        t.join()
        await h.pause(0.02)
        assert hits == ["ok"]


# ── painter integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_row_change_emits_exactly_one_cup():
    async with Headless(App(), size=(40, 8)) as h:
        h.app.push(RowsView())
        await h.pause()
        mark = len(h.io.written)
        await h.press("x")  # only row 2 (counter) changes
        assert cups(bytes(h.io.written[mark:])) == [(3, 1)]


@pytest.mark.asyncio
async def test_resize_repaints_at_new_size():
    async with Headless(App(), size=(40, 8)) as h:
        h.app.push(RowsView())
        await h.pause()
        h.io.cols, h.io.rows = 20, 4
        h.app._on_resize()
        assert h.app.current_frame.width == 20
        assert h.app.current_frame.height == 4


@pytest.mark.asyncio
async def test_pane_ticker_coalesces_dirty_flags_into_paint():
    async with Headless(App(), size=(30, 6)) as h:
        view = RowsView()
        h.app.push(view)
        await h.pause()

        class Pane:
            has_dirty = False

        pane = Pane()
        h.app.register_pane(pane)
        mark = len(h.io.written)
        view.counter += 1  # make the requested paint actually change a row
        pane.has_dirty = True
        await h.pause(0.1)  # > PANE_TICK_SECS
        assert pane.has_dirty is False  # ticker cleared the flag
        assert len(h.io.written) > mark  # and requested a paint
        h.app.unregister_pane(pane)
        await h.pause(0.1)  # ticker stops itself once empty
        assert h.app._pane_ticker.done()


@pytest.mark.asyncio
async def test_noop_repaint_writes_no_bytes():
    async with Headless(App(), size=(30, 6)) as h:
        h.app.push(RowsView())
        await h.pause()
        mark = len(h.io.written)
        h.app.paint_now()  # identical frame: painter returns b"", io skipped
        assert len(h.io.written) == mark


# ── suspend ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suspend_mode_sequence_and_full_repaint():
    async with Headless(App(), size=(30, 6)) as h:
        h.app.push(RowsView())
        await h.pause()
        h.io.calls.clear()
        mark = len(h.io.written)
        with h.app.suspend():
            h.io.calls.append("body")
        assert h.io.calls == ["exit_alt_cooked", "body", "enter_raw_alt"]
        assert len(cups(bytes(h.io.written[mark:]))) == 6  # invalidated


# ── visibility gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hidden_ui_skips_paints_and_catches_up_on_visible():
    async with Headless(App(), size=(30, 6)) as h:
        h.app.push(RowsView())
        await h.pause()
        h.app._apply_visibility(False)
        mark = len(h.io.written)
        await h.press("x")
        assert len(h.io.written) == mark  # nothing painted while hidden
        h.app._apply_visibility(True)
        assert len(cups(bytes(h.io.written[mark:]))) == 6  # full catch-up
        assert "counter: 1" in h.screen_text()


@pytest.mark.asyncio
async def test_visibility_probe_is_polled():
    calls = []

    def probe():
        calls.append(1)
        return True

    async with Headless(App(visibility_probe=probe)) as h:
        await h.pause(0.02)
        assert calls  # first poll happens at startup


# ── crash contract ────────────────────────────────────────────────


class BoomView(View):
    def on_key(self, ev) -> bool:
        raise RuntimeError("view-boom")


@pytest.mark.asyncio
async def test_view_crash_writes_log_restores_and_reraises(tmp_path, monkeypatch):
    log = tmp_path / "crash.log"
    monkeypatch.setenv("ORCH_CRASH_LOG", str(log))
    app = App(io=FakeTermIO())
    task = asyncio.get_running_loop().create_task(app._main())
    await asyncio.sleep(0)
    app.push(BoomView())
    await asyncio.sleep(0)
    app.feed(b"x")  # on_key raises → routed into _main
    with pytest.raises(RuntimeError, match="view-boom"):
        await asyncio.wait_for(task, 2)
    assert "view-boom" in log.read_text()
    assert app.io.calls[-1] == "restore"  # crash path still restored the tty
    await asyncio.sleep(0)


# ── misc ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clipboard_and_bell_go_through_io():
    async with Headless(App()) as h:
        h.app.copy_to_clipboard("snippet")
        assert h.io.clipboard == ["snippet"]
        mark = len(h.io.written)
        h.app.bell()
        assert bytes(h.io.written[mark:]) == b"\a"


@pytest.mark.asyncio
async def test_key_dispatch_with_empty_stack_is_safe():
    async with Headless(App()) as h:
        await h.press("x")  # no views: falls to on_unhandled_key no-op
