"""Profiling harness for the orchestrator TUI.

Instruments key codepaths to measure actual wall-clock time spent
in rendering, layout, and data operations.
"""
import time
import functools
import statistics
from collections import defaultdict


class PerfTracker:
    """Lightweight performance instrumentation."""

    _instance = None

    def __init__(self):
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.counts: dict[str, int] = defaultdict(int)
        self._start_times: dict[str, float] = {}

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start(self, name: str):
        self._start_times[name] = time.perf_counter()

    def stop(self, name: str):
        if name in self._start_times:
            elapsed = time.perf_counter() - self._start_times.pop(name)
            self.timings[name].append(elapsed)
            self.counts[name] += 1
            return elapsed
        return 0.0

    def track(self, name: str):
        """Context manager for timing a block."""
        class _Timer:
            def __init__(self, tracker, name):
                self.tracker = tracker
                self.name = name
            def __enter__(self):
                self.tracker.start(self.name)
                return self
            def __exit__(self, *args):
                self.tracker.stop(self.name)
        return _Timer(self, name)

    def report(self) -> str:
        lines = ["\n=== PERFORMANCE REPORT ===\n"]
        for name in sorted(self.timings.keys()):
            times = self.timings[name]
            count = len(times)
            total = sum(times)
            mean = statistics.mean(times)
            if count > 1:
                p50 = statistics.median(times)
                p95 = sorted(times)[int(count * 0.95)] if count >= 20 else max(times)
                lines.append(
                    f"  {name:40s}  calls={count:4d}  "
                    f"total={total*1000:8.1f}ms  "
                    f"mean={mean*1000:6.2f}ms  "
                    f"p50={p50*1000:6.2f}ms  "
                    f"p95={p95*1000:6.2f}ms"
                )
            else:
                lines.append(
                    f"  {name:40s}  calls={count:4d}  "
                    f"total={total*1000:8.1f}ms"
                )
        lines.append("")
        return "\n".join(lines)

    def reset(self):
        self.timings.clear()
        self.counts.clear()
        self._start_times.clear()


def timed(name: str = None):
    """Decorator to time a function."""
    def decorator(fn):
        label = name or f"{fn.__module__}.{fn.__qualname__}"
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tracker = PerfTracker.get()
            tracker.start(label)
            try:
                return fn(*args, **kwargs)
            finally:
                tracker.stop(label)
        return wrapper
    return decorator
