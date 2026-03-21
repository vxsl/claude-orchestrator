"""Lightweight periodic description re-evaluation for workstreams.

Uses Haiku to refresh workstream descriptions when new session activity
has occurred. Rate-limited to once per 6 hours per workstream, and only
triggered when the workstream's session context has materially changed.

Cache: ~/.cache/claude-orchestrator/description-evals.json
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from models import Workstream, Store
from sessions import ClaudeSession
from threads import Thread, _extract_first_message

CACHE_DIR = Path.home() / ".cache" / "claude-orchestrator"
CACHE_FILE = CACHE_DIR / "description-evals.json"

# Minimum time between re-evaluations per workstream
COOLDOWN = timedelta(hours=6)

# Max workstreams per LLM call
BATCH_SIZE = 10


# ─── Cache I/O ──────────────────────────────────────────────────────

def _load_cache() -> dict[str, dict]:
    """Load {ws_id: {context_hash, evaluated_at, description}}."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ─── Context hashing ────────────────────────────────────────────────

def _ws_context_hash(ws: Workstream, sessions: list[ClaudeSession]) -> str:
    """Fingerprint a workstream's session context.

    Changes when new sessions are linked or session content changes.
    """
    parts = [ws.name]
    # Sort sessions by ID for stability
    for s in sorted(sessions, key=lambda s: s.session_id):
        msg = _extract_first_message(s)
        parts.append(f"{s.session_id}:{msg[:100] if msg else ''}")
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ws_session_context(ws: Workstream, sessions: list[ClaudeSession]) -> str:
    """Build a compact summary of what's happening in a workstream's sessions."""
    lines = [f"{len(sessions)} sessions"]
    # Most recent sessions first, cap at 8
    recent = sorted(sessions, key=lambda s: s.last_activity or "", reverse=True)[:8]
    for s in recent:
        msg = _extract_first_message(s)
        if msg and len(msg) > 5:
            lines.append(f"- {msg[:150]}")
    return "\n".join(lines)


# ─── LLM call ───────────────────────────────────────────────────────

def _build_prompt(workstreams_context: list[dict]) -> str:
    """Build a batch prompt for description re-evaluation.

    Each entry: {id, name, current_description, session_summary}
    """
    blocks = []
    for ctx in workstreams_context:
        block = f"""[WS:{ctx['id']}]
Name: {ctx['name']}
Current description: {ctx['current_description'] or '(none)'}
Recent session activity:
{ctx['session_summary']}"""
        blocks.append(block)

    return f"""You maintain one-sentence descriptions for workstreams on a developer dashboard.

For each workstream below, look at the recent session activity and decide:
- If the current description still accurately captures the workstream's purpose, respond with "keep"
- If the work has evolved or the description is missing/stale, write a new one-sentence description

Rules:
- Descriptions should capture the GOAL or PURPOSE, not list individual tasks
- Keep them concise: one sentence, under 120 characters
- Don't be overly specific about implementation details — focus on the "what" and "why"
- If the current description is good, just say "keep" — don't change for the sake of changing

Respond with ONLY a JSON object mapping workstream ID to either "keep" or the new description string.

{chr(10).join(blocks)}"""


def _call_llm(prompt: str) -> dict[str, str]:
    """Call Haiku and parse the JSON response."""
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku",
             "--no-session-persistence",
             "--output-format", "json",
             "--max-budget-usd", "0.02",
             "--allowedTools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            return {}

        try:
            outer = json.loads(result.stdout)
            text = outer.get("result", "") if isinstance(outer, dict) else result.stdout
        except json.JSONDecodeError:
            text = result.stdout

        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {}

    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return {}


# ─── Main logic ──────────────────────────────────────────────────────

def _find_sessions_for_ws(ws: Workstream, all_sessions: list[ClaudeSession]) -> list[ClaudeSession]:
    """Lightweight session matching — by directory and explicit links."""
    found = []
    seen = set()

    # Explicit claude-session links
    for link in ws.links:
        if link.kind == "claude-session":
            for s in all_sessions:
                if (s.session_id == link.value or s.session_id.startswith(link.value)) \
                        and s.session_id not in seen:
                    found.append(s)
                    seen.add(s.session_id)

    # Directory matching
    ws_dirs = set()
    if ws.repo_path:
        ws_dirs.add(ws.repo_path.rstrip("/"))
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value).rstrip("/")
            ws_dirs.add(expanded)

    if ws_dirs:
        for s in all_sessions:
            if s.session_id not in seen and s.project_path.rstrip("/") in ws_dirs:
                found.append(s)
                seen.add(s.session_id)

    # Thread-based matching
    if ws.thread_ids:
        tid_set = set(ws.thread_ids)
        for s in all_sessions:
            if s.session_id not in seen:
                # Sessions carry a thread_id if they were clustered
                sid = getattr(s, 'thread_id', None)
                if sid and sid in tid_set:
                    found.append(s)
                    seen.add(s.session_id)

    return found


def refresh_descriptions(
    store: Store,
    all_sessions: list[ClaudeSession],
) -> int:
    """Re-evaluate descriptions for workstreams with changed context.

    Returns the number of descriptions updated.
    """
    cache = _load_cache()
    now = datetime.now()

    # Find workstreams that need evaluation
    candidates = []
    for ws in store.active:
        if ws.archived:
            continue

        sessions = _find_sessions_for_ws(ws, all_sessions)
        if not sessions:
            continue

        ctx_hash = _ws_context_hash(ws, sessions)
        cached = cache.get(ws.id, {})

        # Skip if context hasn't changed
        if cached.get("context_hash") == ctx_hash:
            continue

        # Skip if evaluated recently (cooldown)
        last_eval = cached.get("evaluated_at", "")
        if last_eval:
            try:
                last_dt = datetime.fromisoformat(last_eval)
                if now - last_dt < COOLDOWN:
                    continue
            except (ValueError, TypeError):
                pass

        session_summary = _ws_session_context(ws, sessions)

        candidates.append({
            "ws": ws,
            "id": ws.id,
            "name": ws.name,
            "current_description": ws.description,
            "session_summary": session_summary,
            "context_hash": ctx_hash,
        })

    if not candidates:
        return 0

    # Batch into one LLM call
    batch = candidates[:BATCH_SIZE]
    prompt = _build_prompt(batch)
    results = _call_llm(prompt)

    if not results:
        return 0

    updated = 0
    for ctx in batch:
        ws = ctx["ws"]
        new_desc = results.get(ws.id, "keep")

        # Update cache regardless (tracks that we evaluated)
        cache[ws.id] = {
            "context_hash": ctx["context_hash"],
            "evaluated_at": now.isoformat(),
            "description": ws.description,
        }

        if isinstance(new_desc, str) and new_desc.lower() != "keep" and new_desc != ws.description:
            ws.description = new_desc
            store.update(ws)
            cache[ws.id]["description"] = new_desc
            updated += 1

    _save_cache(cache)
    return updated
