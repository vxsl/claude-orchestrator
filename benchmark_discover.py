#!/usr/bin/env python3
"""Benchmark discover_threads to understand cold vs warm performance."""
import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from threads import discover_threads
from sessions import _parsed_session_cache

# Cold start
_parsed_session_cache.clear()
t0 = time.perf_counter()
threads = discover_threads()
cold = time.perf_counter() - t0
print(f"Cold discover_threads: {cold*1000:.1f}ms  ({len(threads)} threads)")

# Warm (cached)
times = []
for _ in range(5):
    t0 = time.perf_counter()
    threads = discover_threads()
    times.append(time.perf_counter() - t0)

import statistics
mean = statistics.mean(times) * 1000
print(f"Warm discover_threads: mean={mean:.1f}ms  (5 runs)")
print(f"  Cache entries: {len(_parsed_session_cache)}")
