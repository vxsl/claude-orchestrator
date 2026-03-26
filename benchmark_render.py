#!/usr/bin/env python3
"""Benchmark the actual rendering pipeline to find bottlenecks.

This measures wall-clock time for the key operations that happen
during normal TUI usage:
  1. Table rebuild (clear + add_row for all items)
  2. Preview pane render
  3. Bar updates
  4. Rich markup generation
  5. Textual's internal render cycle
"""
import time
import sys
import os
import statistics
from collections import defaultdict

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))


def bench(label, fn, n=100):
    """Run fn n times, report stats."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    mean = statistics.mean(times)
    p50 = statistics.median(times)
    p95 = sorted(times)[int(n * 0.95)]
    total = sum(times)
    print(f"  {label:50s}  mean={mean*1000:7.3f}ms  p50={p50*1000:7.3f}ms  p95={p95*1000:7.3f}ms  total={total*1000:8.1f}ms")
    return mean


def bench_state_operations():
    """Benchmark pure state/data operations (no Textual)."""
    print("\n=== STATE & DATA OPERATIONS ===\n")

    from models import Store
    from state import AppState
    from threads import discover_threads, session_activity
    from thread_namer import apply_cached_names
    from rendering import (
        _best_activity, _ws_indicators, _worktree_styled, _activity_icon,
        _render_session_option, _is_session_seen, _rich_escape,
        _status_markup, _category_markup, _token_color_markup,
        STATUS_THEME, CATEGORY_THEME, C_DIM, C_CYAN, C_YELLOW, C_GREEN, C_BLUE, C_PURPLE,
    )
    from models import STATUS_ICONS, _relative_time
    from actions import ws_directories

    store = Store()
    state = AppState(store)

    # Load sessions like the app does on startup
    t0 = time.perf_counter()
    threads = discover_threads()
    t_discover = time.perf_counter() - t0
    print(f"  discover_threads()                                time={t_discover*1000:.1f}ms  ({len(threads)} threads)")

    t0 = time.perf_counter()
    apply_cached_names(threads)
    t_names = time.perf_counter() - t0
    print(f"  apply_cached_names()                              time={t_names*1000:.1f}ms")

    sessions = []
    for t in threads:
        sessions.extend(t.sessions)
    sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
    print(f"  Total sessions: {len(sessions)}")
    print(f"  Total workstreams: {len(store.active)}")

    state.update_sessions(sessions, threads)

    # Benchmark get_unified_items
    bench("state.get_unified_items()", lambda: state.get_unified_items())

    # Benchmark sessions_for_ws (called per row)
    items = state.get_unified_items()
    if items:
        bench("state.sessions_for_ws() x1", lambda: state.sessions_for_ws(items[0]))
        def all_sessions():
            for ws in items:
                state.sessions_for_ws(ws)
        bench(f"state.sessions_for_ws() x{len(items)} (all)", all_sessions)

    # Benchmark get_last_seen
    bench("state.get_last_seen()", lambda: state.get_last_seen())

    # Benchmark rendering helpers
    last_seen = state.get_last_seen()
    if sessions:
        s = sessions[0]
        bench("session_activity() x1", lambda: session_activity(s, last_seen))

    if items:
        ws = items[0]
        ws_sessions = state.sessions_for_ws(ws)
        bench("_best_activity()", lambda: _best_activity(ws_sessions, last_seen))
        bench("_ws_indicators()", lambda: _ws_indicators(ws, tmux_check=state.ws_has_tmux))
        bench("_worktree_styled()", lambda: _worktree_styled(ws))
        bench("_rich_escape(ws.name)", lambda: _rich_escape(ws.name))

        if ws_sessions:
            s = ws_sessions[0]
            act = session_activity(s, last_seen)
            seen = _is_session_seen(s, last_seen)
            bench(
                "_render_session_option()",
                lambda: _render_session_option(s, act, 0, title_width=35, seen=seen),
            )

    # Benchmark Rich Text creation (this is what happens per table row)
    from rich.text import Text
    bench("Text('hello', style='red')", lambda: Text("hello", style="red"))
    bench("Text.from_markup('[bold]hi[/bold]')", lambda: Text.from_markup("[bold]hi[/bold]"))

    # Benchmark full row data construction (minus Textual)
    if items:
        def build_one_row():
            ws = items[0]
            ws_sessions = state.sessions_for_ws(ws)
            status_cell = Text(STATUS_ICONS[ws.status], style=STATUS_THEME[ws.status])
            indicators = _ws_indicators(ws, tmux_check=state.ws_has_tmux)
            name_str = _rich_escape(ws.name)
            if indicators:
                name_str += "  " + indicators
            name_cell = Text.from_markup(name_str)
            wt_text, wt_color = _worktree_styled(ws)
            repo_cell = Text(wt_text, style=wt_color or C_DIM)
            sess_count = len(ws_sessions) if ws_sessions else 0
            sess_cell = Text(str(sess_count) if sess_count else "", style=C_DIM)
            cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
            updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)
            return (status_cell, name_cell, repo_cell, sess_cell, cat_cell, updated_cell)

        bench("build_one_row() (all cells for 1 ws)", build_one_row)

        def build_all_rows():
            for ws in items:
                ws_sessions = state.sessions_for_ws(ws)
                status_cell = Text(STATUS_ICONS[ws.status], style=STATUS_THEME[ws.status])
                indicators = _ws_indicators(ws, tmux_check=state.ws_has_tmux)
                name_str = _rich_escape(ws.name)
                if indicators:
                    name_str += "  " + indicators
                name_cell = Text.from_markup(name_str)
                wt_text, wt_color = _worktree_styled(ws)
                repo_cell = Text(wt_text, style=wt_color or C_DIM)
                sess_count = len(ws_sessions) if ws_sessions else 0
                sess_cell = Text(str(sess_count) if sess_count else "", style=C_DIM)
                cat_cell = Text(ws.category.value, style=CATEGORY_THEME[ws.category])
                updated_cell = Text(_relative_time(ws.updated_at), style=C_DIM)

        bench(f"build_all_rows() x{len(items)} items", build_all_rows)


def bench_textual_datatable():
    """Benchmark Textual DataTable operations in isolation."""
    print("\n=== TEXTUAL DATATABLE OPERATIONS ===\n")

    from textual.widgets import DataTable
    from rich.text import Text

    # We can't easily bench DataTable without an app context,
    # but we can measure the overhead of creating Rich objects
    # that go into it.

    # Measure creating 50 rows of data
    def create_50_rows():
        rows = []
        for i in range(50):
            rows.append((
                Text("●", style="red"),
                Text.from_markup(f"[bold]Workstream {i}[/bold]  [dim]▸[/dim]"),
                Text("~/dev/project", style="#6e7681"),
                Text(str(i), style="#6e7681"),
                Text("work", style="#8b949e"),
                Text("2h ago", style="#6e7681"),
            ))
        return rows

    bench("create_50_rows (50 rows of Rich Text)", create_50_rows)


def bench_textual_render():
    """Benchmark Textual's actual rendering using a headless app."""
    print("\n=== TEXTUAL HEADLESS RENDER ===\n")

    import asyncio
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Static
    from rich.text import Text

    class BenchApp(App):
        CSS = "Screen { background: #000000; }"

        def compose(self) -> ComposeResult:
            yield Static("Status bar", id="bar")
            yield DataTable(id="table")

        def on_mount(self):
            table = self.query_one("#table", DataTable)
            table.cursor_type = "row"
            table.add_columns("St", "Name", "Repo", "Sess", "Cat", "Updated")

    async def run_bench():
        app = BenchApp()

        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#table", DataTable)

            # Benchmark: add 50 rows
            def add_rows():
                table.clear()
                for i in range(50):
                    table.add_row(
                        Text("●", style="red"),
                        Text.from_markup(f"Workstream {i}"),
                        Text("~/dev/project", style="#6e7681"),
                        Text(str(i), style="#6e7681"),
                        Text("work", style="#8b949e"),
                        Text("2h ago", style="#6e7681"),
                    )

            # Warm up
            add_rows()
            await pilot.pause()

            # Time the add_rows operation
            times = []
            for _ in range(20):
                t0 = time.perf_counter()
                add_rows()
                times.append(time.perf_counter() - t0)
                await pilot.pause()

            mean = statistics.mean(times) * 1000
            p50 = statistics.median(times) * 1000
            p95 = sorted(times)[int(len(times) * 0.95)] * 1000
            print(f"  {'table.clear() + 50x add_row()':50s}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

            # Time: update Static bar
            bar = app.query_one("#bar", Static)
            times = []
            for i in range(50):
                t0 = time.perf_counter()
                bar.update(f"[bold]ORCH[/bold]  {i} streams  ● 3  ◉ 2  ○ 1")
                times.append(time.perf_counter() - t0)
                await pilot.pause()

            mean = statistics.mean(times) * 1000
            p50 = statistics.median(times) * 1000
            p95 = sorted(times)[int(len(times) * 0.95)] * 1000
            print(f"  {'Static.update() with markup':50s}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

            # Time: full render cycle after table rebuild
            times = []
            for _ in range(20):
                add_rows()
                t0 = time.perf_counter()
                await pilot.pause()  # This triggers the actual render
                times.append(time.perf_counter() - t0)

            mean = statistics.mean(times) * 1000
            p50 = statistics.median(times) * 1000
            p95 = sorted(times)[int(len(times) * 0.95)] * 1000
            print(f"  {'pilot.pause() after table rebuild':50s}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

            # Time: cursor movement (what j/k feels like)
            add_rows()
            await pilot.pause()
            times = []
            for _ in range(50):
                t0 = time.perf_counter()
                table.action_cursor_down()
                await pilot.pause()
                times.append(time.perf_counter() - t0)

            mean = statistics.mean(times) * 1000
            p50 = statistics.median(times) * 1000
            p95 = sorted(times)[int(len(times) * 0.95)] * 1000
            print(f"  {'cursor_down + render':50s}  mean={mean:7.3f}ms  p50={p50:7.3f}ms  p95={p95:7.3f}ms")

    asyncio.run(run_bench())


if __name__ == "__main__":
    print("=" * 80)
    print("  ORCHESTRATOR TUI PERFORMANCE BENCHMARK")
    print("=" * 80)

    bench_state_operations()
    bench_textual_datatable()
    bench_textual_render()

    print("\nDone.")
