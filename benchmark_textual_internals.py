#!/usr/bin/env python3
"""Profile Textual's internal rendering pipeline to find exact bottlenecks."""
import time
import sys
import os
import asyncio
import statistics
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))


async def profile_textual_internals():
    """Instrument Textual's render pipeline methods."""
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Static
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import OptionList
    from textual.widgets.option_list import Option
    from rich.text import Text

    timings = {}

    def instrument(cls, method_name, label):
        """Monkeypatch a method to record timing."""
        original = getattr(cls, method_name)
        times = []
        timings[label] = times

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = original(*args, **kwargs)
            times.append(time.perf_counter() - t0)
            return result

        setattr(cls, method_name, wrapper)
        return original

    # Instrument key Textual internals
    from textual.screen import Screen
    from textual._compositor import Compositor
    from textual.widget import Widget
    from textual._styles_cache import StylesCache

    originals = {}
    originals['screen._compositor_refresh'] = instrument(Screen, '_compositor_refresh', 'Screen._compositor_refresh')
    originals['screen._refresh_layout'] = instrument(Screen, '_refresh_layout', 'Screen._refresh_layout')
    originals['compositor.render_update'] = instrument(Compositor, 'render_update', 'Compositor.render_update')

    # Can't easily instrument render_lines since it's called per-widget,
    # but we can instrument the cache
    originals['widget._render'] = instrument(Widget, '_render', 'Widget._render')

    class BenchApp(App):
        CSS = """
        Screen { background: #000000; }
        #bar { height: 1; }
        #main { height: 1fr; }
        DataTable { width: 3fr; }
        #preview { width: 2fr; padding: 1; }
        #preview-content { width: 100%; }
        #preview-sessions { height: auto; max-height: 16; }
        """

        def compose(self) -> ComposeResult:
            yield Static("Status", id="bar")
            yield Static("View bar", id="vbar")
            yield Static("Filter bar", id="fbar")
            with Horizontal(id="main"):
                yield DataTable(id="table")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview-content")
                    yield OptionList(id="preview-sessions")
            yield Static("Summary", id="summary")

        def on_mount(self):
            table = self.query_one("#table", DataTable)
            table.cursor_type = "row"
            table.add_columns("St", "Name", "Repo", "Sess", "Cat", "Updated")

    async with BenchApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app
        table = app.query_one("#table", DataTable)
        preview = app.query_one("#preview-content", Static)
        olist = app.query_one("#preview-sessions", OptionList)

        def populate():
            table.clear()
            for i in range(42):
                table.add_row(
                    Text("●", style="red"),
                    Text.from_markup(f"Workstream {i}  [dim]▸[/dim]"),
                    Text("~/dev/project", style="#6e7681"),
                    Text(str(i), style="#6e7681"),
                    Text("work", style="#8b949e"),
                    Text("2h ago", style="#6e7681"),
                )
            # Simulate preview update
            preview.update("[bold purple]My Workstream[/bold purple]\n● In Progress  work\n\nDescription here\n\n[bold blue]Activity[/bold blue]\n  3 sessions  · 42 messages  · 1.2M tokens")
            olist.clear_options()
            for j in range(8):
                olist.add_option(Option(f"  Session {j}\n  claude-sonnet · 2h ago · 150k tokens", id=str(j)))

        # Warm up
        populate()
        await pilot.pause()

        # Clear timing data
        for v in timings.values():
            v.clear()

        print("\n=== TEXTUAL INTERNAL PROFILING ===\n")
        print("--- Full table rebuild + preview update ---\n")

        # Measure several full rebuild cycles
        for _ in range(20):
            populate()
            await pilot.pause()

        for label, times in sorted(timings.items()):
            if times:
                count = len(times)
                mean = statistics.mean(times) * 1000
                total = sum(times) * 1000
                p50 = statistics.median(times) * 1000
                p95 = sorted(times)[int(count * 0.95)] * 1000 if count >= 20 else max(times) * 1000
                print(f"  {label:40s}  calls={count:4d}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms  total={total:8.1f}ms")

        # Clear and test cursor movement
        for v in timings.values():
            v.clear()

        print("\n--- Cursor navigation (j/k feel) ---\n")

        populate()
        await pilot.pause()
        for v in timings.values():
            v.clear()

        for _ in range(50):
            table.action_cursor_down()
            await pilot.pause()

        for label, times in sorted(timings.items()):
            if times:
                count = len(times)
                mean = statistics.mean(times) * 1000
                total = sum(times) * 1000
                p50 = statistics.median(times) * 1000
                p95 = sorted(times)[int(count * 0.95)] * 1000 if count >= 20 else max(times) * 1000
                print(f"  {label:40s}  calls={count:4d}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms  total={total:8.1f}ms")

        # Now test with the REAL app
        print("\n\n=== PROFILING WITH REAL APP ===\n")

    # Reset instrumentations
    for key, orig in originals.items():
        cls_name, method = key.rsplit('.', 1)
        if cls_name == 'screen':
            setattr(Screen, method, orig)
        elif cls_name == 'compositor':
            setattr(Compositor, method, orig)
        elif cls_name == 'widget':
            setattr(Widget, method, orig)

    # Now instrument and run the REAL app
    timings2 = {}
    instrument(Screen, '_compositor_refresh', 'Screen._compositor_refresh')
    instrument(Screen, '_refresh_layout', 'Screen._refresh_layout')
    instrument(Compositor, 'render_update', 'Compositor.render_update')
    instrument(Widget, '_render', 'Widget._render')

    from app import OrchestratorApp

    async with OrchestratorApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app

        # Let it fully load
        await pilot.pause()
        await asyncio.sleep(2)
        await pilot.pause()

        # Clear all timing data
        for v in timings.values():
            v.clear()

        print("--- Real app: cursor navigation ---\n")

        from textual.widgets import OptionList
        table = app.query_one("#ws-table", OptionList)
        for _ in range(30):
            table.action_cursor_down()
            await pilot.pause()

        for label, times in sorted(timings.items()):
            if times:
                count = len(times)
                mean = statistics.mean(times) * 1000
                total = sum(times) * 1000
                p50 = statistics.median(times) * 1000
                p95 = sorted(times)[int(count * 0.95)] * 1000 if count >= 20 else max(times) * 1000
                print(f"  {label:40s}  calls={count:4d}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms  total={total:8.1f}ms")

        # Force a full table rebuild
        for v in timings.values():
            v.clear()

        print("\n--- Real app: full table rebuild ---\n")

        for _ in range(10):
            app._do_refresh_ws_table()
            await pilot.pause()

        for label, times in sorted(timings.items()):
            if times:
                count = len(times)
                mean = statistics.mean(times) * 1000
                total = sum(times) * 1000
                p50 = statistics.median(times) * 1000
                print(f"  {label:40s}  calls={count:4d}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  total={total:8.1f}ms")


if __name__ == "__main__":
    asyncio.run(profile_textual_internals())
