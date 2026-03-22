#!/usr/bin/env python3
"""Compare FastTable vs DataTable performance."""
import asyncio
import time
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from rich.text import Text


async def bench_datatable():
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable

    class DTApp(App):
        CSS = "Screen { background: #000; }"
        def compose(self) -> ComposeResult:
            yield DataTable(id="t")
        def on_mount(self):
            t = self.query_one("#t", DataTable)
            t.cursor_type = "row"
            t.add_columns("St", "Name", "Repo", "Sess", "Cat", "Updated")

    async with DTApp().run_test(size=(120, 40)) as pilot:
        table = pilot.app.query_one("#t", DataTable)

        def populate():
            table.clear()
            for i in range(42):
                table.add_row(
                    Text("●", style="red"),
                    Text(f"Workstream {i}", style="bold"),
                    Text("~/dev/project", style="#6e7681"),
                    Text(str(i), style="#6e7681"),
                    Text("work", style="#8b949e"),
                    Text("2h ago", style="#6e7681"),
                )

        populate()
        await pilot.pause()

        # Bench: cursor down + render
        times = []
        for _ in range(40):
            t0 = time.perf_counter()
            table.action_cursor_down()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  DataTable cursor_down + render      mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

        # Bench: full rebuild + render
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            populate()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  DataTable rebuild + render           mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")


async def bench_fasttable():
    from textual.app import App, ComposeResult
    from fast_table import FastTable

    class FTApp(App):
        CSS = "Screen { background: #000; }"
        def compose(self) -> ComposeResult:
            yield FastTable(id="t")
        def on_mount(self):
            t = self.query_one("#t", FastTable)
            t.add_columns("St", "Name", "Repo", "Sess", "Cat", "Updated")

    async with FTApp().run_test(size=(120, 40)) as pilot:
        table = pilot.app.query_one("#t", FastTable)

        def populate():
            table.clear()
            for i in range(42):
                table.add_row(
                    Text("●", style="red"),
                    Text(f"Workstream {i}", style="bold"),
                    Text("~/dev/project", style="#6e7681"),
                    Text(str(i), style="#6e7681"),
                    Text("work", style="#8b949e"),
                    Text("2h ago", style="#6e7681"),
                )

        populate()
        table.refresh()
        await pilot.pause()

        # Bench: cursor down + render
        times = []
        for _ in range(40):
            t0 = time.perf_counter()
            table.action_cursor_down()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  FastTable cursor_down + render       mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

        # Bench: full rebuild + render
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            populate()
            table.refresh()
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  FastTable rebuild + render            mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")


async def main():
    print("\n=== TABLE WIDGET COMPARISON ===\n")
    await bench_datatable()
    await bench_fasttable()
    print()


if __name__ == "__main__":
    asyncio.run(main())
