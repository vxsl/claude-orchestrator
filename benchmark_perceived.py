#!/usr/bin/env python3
"""Measure perceived input latency using key presses through the pilot."""
import asyncio
import time
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


async def bench_key_press():
    from app import OrchestratorApp

    print("\n=== PERCEIVED INPUT LATENCY ===\n")

    async with OrchestratorApp().run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(2)
        await pilot.pause()

        # Measure j key presses (actual user input path)
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            await pilot.press("j")
            await pilot.pause()
            times.append(time.perf_counter() - t0)

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        p95 = sorted(times)[int(len(times) * 0.95)] * 1000
        print(f"  j key → render complete           mean={mean:7.2f}ms  p50={p50:7.2f}ms  p95={p95:7.2f}ms")

        # Measure rapid j key presses (holding j)
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            await pilot.press("j")
            # Don't wait for render — measure how fast we can process input
            times.append(time.perf_counter() - t0)
        await pilot.pause()  # flush at end

        mean = statistics.mean(times) * 1000
        p50 = statistics.median(times) * 1000
        print(f"  j key (input only, no render)     mean={mean:7.2f}ms  p50={p50:7.2f}ms")

        # Measure Enter → screen push
        times = []
        for _ in range(3):
            # Move to a valid row first
            from textual.widgets import OptionList
            table = pilot.app.query_one("#ws-table", OptionList)
            table.highlighted = 0
            await pilot.pause()

            t0 = time.perf_counter()
            await pilot.press("enter")
            await pilot.pause()
            elapsed = time.perf_counter() - t0
            times.append(elapsed)

            # Dismiss the detail screen
            await pilot.press("escape")
            await pilot.pause()

        mean = statistics.mean(times) * 1000
        print(f"  Enter → DetailScreen push         mean={mean:7.2f}ms")


asyncio.run(bench_key_press())
