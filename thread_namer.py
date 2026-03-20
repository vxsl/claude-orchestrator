"""Lightweight Sonnet-powered thread naming and categorization.

Uses `claude -p --model sonnet` for cheap, fast title generation.
Results are cached in ~/.cache/claude-orchestrator/thread-names.json
so each thread is only processed once.
"""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from threads import Thread
from sessions import ClaudeSession

CACHE_DIR = Path.home() / ".cache" / "claude-orchestrator"
CACHE_FILE = CACHE_DIR / "thread-names.json"

# Max threads to title in one batch (to limit cost/latency)
BATCH_SIZE = 15


@dataclass
class ThreadMeta:
    """AI-generated metadata for a thread."""
    title: str
    category: str  # "work", "personal", "meta"


def _thread_fingerprint(thread: Thread) -> str:
    """Stable fingerprint for a thread based on its content.

    Changes if sessions are added/removed from the cluster.
    """
    session_ids = sorted(s.session_id for s in thread.sessions)
    raw = f"{thread.project_path}:{','.join(session_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_cache() -> dict[str, dict]:
    """Load the title cache. Returns {fingerprint: {title, category}}."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict]):
    """Save the title cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _extract_context(thread: Thread) -> str:
    """Build a minimal context string for the LLM from a thread."""
    parts = []
    parts.append(f"Project: {thread.project_path}")

    # Git branches
    branches = set()
    for s in thread.sessions:
        # Re-extract from the thread — we already have this from discovery
        pass
    if thread.name and thread.name not in ("", thread.short_project):
        parts.append(f"Current heuristic name: {thread.name}")

    # First user messages from sessions (most informative signal)
    from threads import _extract_first_message
    msgs = []
    for s in thread.sessions[:5]:  # Cap at 5 sessions
        msg = _extract_first_message(s)
        if msg and len(msg) > 5:
            msgs.append(msg[:150])

    if msgs:
        parts.append("First messages from sessions:")
        for m in msgs:
            parts.append(f"  - {m}")

    parts.append(f"Sessions: {thread.session_count}, Last active: {thread.age}")

    return "\n".join(parts)


def _build_prompt(threads_context: list[tuple[str, str]]) -> str:
    """Build a prompt for batch thread naming.

    Args:
        threads_context: list of (fingerprint, context_string)
    """
    thread_blocks = []
    for i, (fp, ctx) in enumerate(threads_context):
        thread_blocks.append(f"[THREAD {fp}]\n{ctx}")

    return f"""You are categorizing Claude Code sessions into meaningful threads for a developer dashboard.

For each thread below, provide:
1. A SHORT title (3-8 words max) that captures the train of thought / goal
2. A category: "work" (job tasks, tickets, PRs), "personal" (side projects, configs, tools), or "meta" (tooling about the workflow itself)

Rules:
- Titles should be what a human would call this line of work, not a description of what Claude did
- Use ticket IDs if present (e.g. "UB-6732 time range fix")
- For config/dotfile work, name the thing being configured (e.g. "nvim comment keybind")
- "wip" branches should be named by the actual work being done, based on the messages
- Keep titles lowercase unless they contain proper nouns or ticket IDs

Respond with ONLY a JSON object mapping fingerprint to {{"title": "...", "category": "..."}}.

{chr(10).join(thread_blocks)}"""


def name_threads_batch(threads: list[Thread]) -> dict[str, ThreadMeta]:
    """Name a batch of threads using Sonnet.

    Returns {fingerprint: ThreadMeta} for successfully named threads.
    """
    if not threads:
        return {}

    contexts = []
    for t in threads[:BATCH_SIZE]:
        fp = _thread_fingerprint(t)
        ctx = _extract_context(t)
        contexts.append((fp, ctx))

    prompt = _build_prompt(contexts)

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku",
             "--no-session-persistence",
             "--output-format", "json",
             "--max-budget-usd", "0.05",
             "--allowedTools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            return {}

        # Parse the JSON output — claude --output-format json wraps in a result object
        try:
            outer = json.loads(result.stdout)
            # Extract text content from the response
            text = ""
            if isinstance(outer, dict) and "result" in outer:
                text = outer["result"]
            elif isinstance(outer, str):
                text = outer
            else:
                text = result.stdout
        except json.JSONDecodeError:
            text = result.stdout

        # Find the JSON object in the response text
        # It might be wrapped in markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code block
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(text)

        results = {}
        for fp, meta in data.items():
            if isinstance(meta, dict) and "title" in meta:
                results[fp] = ThreadMeta(
                    title=meta["title"],
                    category=meta.get("category", "personal"),
                )
        return results

    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError):
        return {}


def apply_cached_names(threads: list[Thread]) -> list[Thread]:
    """Apply cached AI-generated names to threads. Non-mutating."""
    cache = _load_cache()

    for thread in threads:
        fp = _thread_fingerprint(thread)
        if fp in cache:
            meta = cache[fp]
            thread.ai_title = meta.get("title", "")
            thread.ai_category = meta.get("category", "")

    return threads


def name_uncached_threads(threads: list[Thread]) -> int:
    """Find threads without cached names and name them with Sonnet.

    Returns the number of newly named threads.
    """
    cache = _load_cache()
    uncached = []

    for t in threads:
        fp = _thread_fingerprint(t)
        if fp not in cache:
            uncached.append(t)

    if not uncached:
        return 0

    # Name in batches
    named_count = 0
    for i in range(0, len(uncached), BATCH_SIZE):
        batch = uncached[i:i + BATCH_SIZE]
        results = name_threads_batch(batch)

        for fp, meta in results.items():
            cache[fp] = {"title": meta.title, "category": meta.category}
            named_count += 1

    _save_cache(cache)
    return named_count
