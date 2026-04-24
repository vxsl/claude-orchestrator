"""Lightweight perf instrumentation for the orchestrator TUI.

Usage:
  ORCH_PERF=1 orch

When enabled:
- Every @perf_trace-decorated call is timed.
- Calls exceeding PERF_SLOW_MS (default 20ms) are appended to
  PERF_LOG_PATH (default /tmp/orch_perf.log) as they happen.
- On process exit, a full stats report is written to PERF_REPORT_PATH
  (default /tmp/orch_perf_report.txt).

When disabled (ORCH_PERF unset): @perf_trace is a no-op, zero overhead.
"""
from __future__ import annotations

import atexit
import functools
import os
import statistics
import sys
import time
from collections import defaultdict

PERF_ENABLED = os.environ.get("ORCH_PERF") == "1"
PERF_SLOW_MS = float(os.environ.get("ORCH_PERF_SLOW_MS", "20"))
PERF_LOG_PATH = os.environ.get("ORCH_PERF_LOG", "/tmp/orch_perf.log")
PERF_REPORT_PATH = os.environ.get("ORCH_PERF_REPORT", "/tmp/orch_perf_report.txt")


class _Tracker:
    def __init__(self):
        self.timings: dict[str, list[float]] = defaultdict(list)
        self._log_fh = None

    def _log(self, msg: str) -> None:
        if self._log_fh is None:
            try:
                self._log_fh = open(PERF_LOG_PATH, "a", buffering=1)
                self._log_fh.write(
                    f"\n# orch perf log opened pid={os.getpid()} "
                    f"at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
            except OSError:
                return
        try:
            self._log_fh.write(msg + "\n")
        except (OSError, ValueError):
            pass

    def record(self, name: str, elapsed: float) -> None:
        self.timings[name].append(elapsed)
        ms = elapsed * 1000
        if ms >= PERF_SLOW_MS:
            self._log(f"SLOW {ms:7.2f}ms  {name}")

    def write_report(self) -> None:
        if not self.timings:
            return
        try:
            fh = open(PERF_REPORT_PATH, "w")
        except OSError:
            return
        lines = ["# orch perf report\n"]
        # Sort by total time desc — shows what contributes most to wall time.
        entries = []
        for name, times in self.timings.items():
            n = len(times)
            total = sum(times)
            mean = total / n
            p50 = statistics.median(times)
            p95 = sorted(times)[int(n * 0.95)] if n >= 20 else max(times)
            entries.append((total, name, n, mean, p50, p95))
        entries.sort(reverse=True)
        header = (
            f"  {'name':45s}  {'calls':>6s}  {'total':>9s}  "
            f"{'mean':>8s}  {'p50':>8s}  {'p95':>8s}\n"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 3) + "\n")
        for total, name, n, mean, p50, p95 in entries:
            lines.append(
                f"  {name:45s}  {n:6d}  {total*1000:7.1f}ms  "
                f"{mean*1000:6.2f}ms  {p50*1000:6.2f}ms  {p95*1000:6.2f}ms\n"
            )
        fh.writelines(lines)
        fh.close()


_tracker = _Tracker() if PERF_ENABLED else None


def perf_trace(name: str | None = None):
    """Decorator — times the call, logs slow ones. No-op when ORCH_PERF is unset."""
    if not PERF_ENABLED:
        def passthrough(fn):
            return fn
        return passthrough

    def decorator(fn):
        label = name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                _tracker.record(label, time.perf_counter() - t0)

        return wrapper

    return decorator


if PERF_ENABLED:
    @atexit.register
    def _dump_on_exit():
        _tracker.write_report()
        sys.stderr.write(f"[orch-perf] report written to {PERF_REPORT_PATH}\n")
