"""View/Timer tests: interval + one-shot firing, pause/resume semantics,
callback exceptions, on_show/on_hide auto pause, dismiss delegation."""

import asyncio

import pytest

from tui.view import Timer, View


class Counter:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1


# ── Timer ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interval_fires_repeatedly():
    tick = Counter()
    t = Timer(0.01, tick, repeat=True)
    await asyncio.sleep(0.08)
    t.cancel()
    assert tick.n >= 2


@pytest.mark.asyncio
async def test_one_shot_fires_once():
    tick = Counter()
    Timer(0.01, tick, repeat=False)
    await asyncio.sleep(0.08)
    assert tick.n == 1


@pytest.mark.asyncio
async def test_pause_is_immediate_and_resume_restarts():
    tick = Counter()
    t = Timer(0.01, tick, repeat=True)
    await asyncio.sleep(0.05)
    t.pause()
    frozen = tick.n  # pause is synchronous: nothing can fire after it
    await asyncio.sleep(0.05)
    assert tick.n == frozen
    t.resume()
    await asyncio.sleep(0.05)
    t.cancel()
    assert tick.n > frozen


@pytest.mark.asyncio
async def test_cancel_stops_firing():
    tick = Counter()
    t = Timer(0.01, tick, repeat=True)
    await asyncio.sleep(0.03)
    t.cancel()
    frozen = tick.n
    await asyncio.sleep(0.05)
    assert tick.n == frozen


@pytest.mark.asyncio
async def test_callback_exception_logged_not_fatal(capsys):
    calls = Counter()

    def boom():
        calls()
        raise RuntimeError("timer-boom")

    t = Timer(0.01, boom, repeat=True)
    await asyncio.sleep(0.08)
    t.cancel()
    assert calls.n >= 2  # kept firing after the exception
    assert "timer-boom" in capsys.readouterr().err


# ── View timer lifecycle ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_hide_pauses_and_on_show_resumes_view_timers():
    view = View()
    tick = Counter()
    view.set_interval(0.01, tick)
    await asyncio.sleep(0.05)
    assert tick.n >= 1
    view.on_hide()
    frozen = tick.n
    await asyncio.sleep(0.05)
    assert tick.n == frozen
    view.on_show()
    await asyncio.sleep(0.05)
    view.cancel_timers()
    assert tick.n > frozen


@pytest.mark.asyncio
async def test_run_when_hidden_timer_survives_on_hide():
    view = View()
    tick = Counter()
    view.set_interval(0.01, tick, run_when_hidden=True)
    view.on_hide()
    await asyncio.sleep(0.05)
    view.cancel_timers()
    assert tick.n >= 1


@pytest.mark.asyncio
async def test_fired_one_shot_prunes_itself_from_view_registry():
    view = View()
    tick = Counter()
    view.set_timer(0.01, tick)
    assert len(view._timers) == 1
    await asyncio.sleep(0.05)
    assert tick.n == 1
    assert view._timers == []  # no leak on long-lived views


@pytest.mark.asyncio
async def test_cancel_timers_stops_everything_and_clears():
    view = View()
    tick = Counter()
    view.set_interval(0.01, tick)
    view.set_timer(0.01, tick)
    view.cancel_timers()
    await asyncio.sleep(0.04)
    assert tick.n == 0
    assert view._timers == []


# ── delegation ────────────────────────────────────────────────────


def test_dismiss_delegates_to_app_pop():
    class FakeApp:
        def __init__(self):
            self.popped = []

        def pop(self, result=None):
            self.popped.append(result)

    view = View()
    view.app = FakeApp()
    view.dismiss("res")
    view.dismiss()
    assert view.app.popped == ["res", None]


def test_request_paint_without_app_is_noop():
    View().request_paint()  # must not raise


def test_base_input_handlers_do_not_consume():
    view = View()
    assert view.on_key(None) is False
    assert view.on_mouse(None) is False
    assert view.on_paste(None) is False
