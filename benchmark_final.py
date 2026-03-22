#!/usr/bin/env python3
"""Final benchmark comparing before/after performance for the real app."""
import asyncio
import time
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from textual.widgets import OptionList


async def bench_real_app():
    from app import OrchestratorApp

    print("\n=== REAL APP PERFORMANCE (with all optimizations) ===\n")

    async with OrchestratorApp().run_test(size=(120, 40)) as pilot:
        app = pilot.app

        # Let it fully load
        await pilot.pause()
        await asyncio.sleep(2)
        await pilot.pause()

        table = app.query_one("#ws-table", OptionList)
        print(f"  Rows: {table.option_count}")

        # Bench: cursor down (should be fast — only 2 lines dirty)
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            table.action_cursor_down()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  cursor_down + render              mean={mean:7.2f}ms  p50={p50:7.2f}ms  p95={p95:7.2f}ms")

        # Bench: cursor up (same pattern, going back)
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            table.action_cursor_up()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  cursor_up + render                mean={mean:7.2f}ms  p50={p50:7.2f}ms  p95={p95:7.2f}ms")

        # Bench: full table rebuild
        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            app._do_refresh_ws_table()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        print(f"  full table rebuild + render        mean={mean:7.2f}ms  p50={p50:7.2f}ms")

        # Bench: view switching
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            app.action_next_view()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        print(f"  view switch                        mean={mean:7.2f}ms")

        # Switch back to workstreams
        while app.state.view_mode.value != "workstreams":
            app.action_next_view()
            await pilot.pause()

        # Bench: preview update (cursor to new workstream)
        table.highlighted = 0
        await pilot.pause()
        times = []
        for i in range(min(10, table.option_count - 1)):
            t0 = time.perf_counter()
            table.action_cursor_down()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        print(f"  cursor + preview update           mean={mean:7.2f}ms  p50={p50:7.2f}ms")


async def bench_datatable_comparison():
    """Compare with DataTable baseline for reference."""
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Static
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import OptionList
    from rich.text import Text

    print("\n=== DATATABLE BASELINE (for comparison) ===\n")

    class BaselineApp(App):
        CSS = """
        Screen { background: #000; }
        #bar { height: 1; }
        #main { height: 1fr; }
        DataTable { width: 3fr; }
        #preview { width: 2fr; padding: 1; }
        """

        def compose(self) -> ComposeResult:
            yield Static("Status", id="bar")
            yield Static("View", id="vbar")
            yield Static("Filter", id="fbar")
            with Horizontal(id="main"):
                yield DataTable(id="table")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview-content")
                    yield OptionList(id="preview-sessions")
            yield Static("Summary", id="summary")

        def on_mount(self):
            t = self.query_one("#table", DataTable)
            t.cursor_type = "row"
            t.add_columns("St", "Name", "Repo", "Sess", "Cat", "Updated")
            for i in range(42):
                t.add_row(
                    Text("●", style="red"),
                    Text(f"Workstream {i}", style="bold"),
                    Text("~/dev/proj", style="#6e7681"),
                    Text(str(i), style="#6e7681"),
                    Text("work", style="#8b949e"),
                    Text("2h", style="#6e7681"),
                )

    async with BaselineApp().run_test(size=(120, 40)) as pilot:
        table = pilot.app.query_one("#table", DataTable)
        await pilot.pause()

        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            table.action_cursor_down()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  DataTable cursor_down + render    mean={mean:7.2f}ms  p50={p50:7.2f}ms  p95={p95:7.2f}ms")


async def main():
    print("=" * 70)
    print("  ORCHESTRATOR TUI — PERFORMANCE COMPARISON")
    print("=" * 70)

    await bench_datatable_comparison()
    await bench_real_app()

    print()


if __name__ == "__main__":
    asyncio.run(main())
