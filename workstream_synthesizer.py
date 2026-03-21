"""Workstream synthesizer — groups thread clusters into workstreams via Haiku.

Takes auto-discovered thread clusters and uses a lightweight LLM call to:
1. Group related threads across projects into unified workstreams
2. Match threads to existing manual workstreams
3. Generate names, categories, and descriptions for new workstreams

Results are cached so the LLM is only called when new threads appear.
Discovered workstreams are stored separately from manual ones (data.json),
and can be "pinned" to promote them to persistent user data.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import Category, Origin, Status, Workstream
from threads import Thread, _extract_first_message, _extract_git_branch

CACHE_DIR = Path.home() / ".cache" / "claude-orchestrator"
ASSIGNMENTS_FILE = CACHE_DIR / "workstream-assignments.json"
DISCOVERED_FILE = CACHE_DIR / "discovered-workstreams.json"

# Max threads per LLM call
BATCH_SIZE = 15


# ─── Cache I/O ──────────────────────────────────────────────────────

def _load_assignments() -> dict:
    """Load thread→workstream assignment cache.

    Structure: {thread_id: workstream_id}
    """
    if not ASSIGNMENTS_FILE.exists():
        return {}
    try:
        data = json.loads(ASSIGNMENTS_FILE.read_text())
        return data.get("assignments", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_assignments(assignments: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ASSIGNMENTS_FILE.write_text(json.dumps({"assignments": assignments}, indent=2))


def _load_discovered() -> list[dict]:
    """Load AI-discovered workstream definitions."""
    if not DISCOVERED_FILE.exists():
        return []
    try:
        data = json.loads(DISCOVERED_FILE.read_text())
        return data.get("workstreams", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_discovered(workstreams: list[dict]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DISCOVERED_FILE.write_text(json.dumps({"workstreams": workstreams}, indent=2))


# ─── Context building ───────────────────────────────────────────────

def _thread_context(thread: Thread) -> str:
    """Build a compact context string for one thread cluster."""
    parts = [f"project:{thread.project_path}"]

    # Branches
    branches = set()
    for s in thread.sessions[:5]:
        b = _extract_git_branch(s)
        if b and b not in ("HEAD", ""):
            branches.add(b)
    if branches:
        parts.append(f"branch:{','.join(sorted(branches))}")

    parts.append(f"sessions:{thread.session_count}")

    # Heuristic or AI name
    if thread.ai_title:
        parts.append(f'"{thread.ai_title}"')
    elif thread.name:
        parts.append(f'"{thread.name}"')

    # First messages (most informative)
    msgs = []
    for s in thread.sessions[:3]:
        msg = _extract_first_message(s)
        if msg and len(msg) > 5:
            msgs.append(msg[:120])
    if msgs:
        parts.append(f"msgs: {' | '.join(msgs)}")

    return " | ".join(parts)


def _build_prompt(
    unassigned: list[Thread],
    manual_workstreams: list[Workstream],
) -> str:
    """Build the synthesis prompt."""

    # Existing manual workstreams for context
    existing_lines = []
    for ws in manual_workstreams:
        dirs = []
        for link in ws.links:
            if link.kind in ("worktree", "file"):
                dirs.append(os.path.expanduser(link.value))
        dir_str = ", ".join(dirs) if dirs else "no dirs"
        existing_lines.append(
            f'WS:{ws.id} | "{ws.name}" | {ws.category.value} | dirs: {dir_str}'
        )

    # Unassigned threads
    thread_lines = []
    for t in unassigned:
        thread_lines.append(f"THREAD:{t.thread_id} | {_thread_context(t)}")

    existing_section = "\n".join(existing_lines) if existing_lines else "(none)"
    thread_section = "\n".join(thread_lines)

    return f"""Group these Claude Code session clusters into workstreams for a developer dashboard.

A workstream is a single sustained goal or line of work. It may span multiple repos, branches, and time periods. Examples: "UB-6668 metric-centric time handling", "dotfiles cleanup", "xmonad workspace config".

[EXISTING WORKSTREAMS — assign threads to these if they clearly match]
{existing_section}

[UNASSIGNED THREAD CLUSTERS — group these into workstreams]
{thread_section}

Rules:
1. If a thread clearly belongs to an existing workstream, assign it: {{"action":"assign","workstream_id":"...","thread_ids":[...]}}
2. Otherwise group related threads into a new workstream: {{"action":"create","name":"...","category":"work|personal|meta","thread_ids":[...],"description":"one sentence"}}
3. Threads about the same ticket/feature across repos = ONE workstream
4. Use ticket IDs in names when present (e.g. "UB-6732 time range fix")
5. Names: 3-8 words, lowercase unless proper nouns/ticket IDs
6. Every thread ID must appear in exactly one group
7. Don't over-merge — separate unrelated work even if in the same repo

Respond with ONLY a JSON array of actions."""


# ─── LLM call ───────────────────────────────────────────────────────

def _call_llm(prompt: str) -> list[dict]:
    """Call Haiku via claude CLI and parse the JSON response."""
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku",
             "--no-session-persistence",
             "--output-format", "json",
             "--max-budget-usd", "0.05",
             "--allowedTools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=120,
        )

        if result.returncode != 0:
            return []

        # Parse claude JSON output
        try:
            outer = json.loads(result.stdout)
            text = outer.get("result", "") if isinstance(outer, dict) else result.stdout
        except json.JSONDecodeError:
            text = result.stdout

        # Strip markdown code blocks if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        actions = json.loads(text)
        if isinstance(actions, list):
            return actions
        return []

    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return []


# ─── Synthesis logic ─────────────────────────────────────────────────

def synthesize_workstreams(
    threads: list[Thread],
    manual_workstreams: list[Workstream],
) -> int:
    """Run the synthesizer for unassigned threads.

    Returns the number of newly assigned threads.
    """
    assignments = _load_assignments()

    # Find threads that aren't assigned yet
    unassigned = [t for t in threads if t.thread_id not in assignments]

    if not unassigned:
        return 0

    # Process in batches
    total_assigned = 0

    for i in range(0, len(unassigned), BATCH_SIZE):
        batch = unassigned[i:i + BATCH_SIZE]
        prompt = _build_prompt(batch, manual_workstreams)
        actions = _call_llm(prompt)

        if not actions:
            continue

        # Load existing discovered workstreams
        discovered = _load_discovered()
        discovered_by_id = {ws["id"]: ws for ws in discovered}

        for action in actions:
            act = action.get("action")
            thread_ids = action.get("thread_ids", [])

            if act == "assign":
                # Assign threads to an existing manual workstream
                ws_id = action.get("workstream_id", "")
                for tid in thread_ids:
                    assignments[tid] = ws_id
                    total_assigned += 1

            elif act == "create":
                # Create a new discovered workstream
                import uuid
                ws_id = str(uuid.uuid4())[:8]
                ws_dict = {
                    "id": ws_id,
                    "name": action.get("name", "untitled"),
                    "description": action.get("description", ""),
                    "category": action.get("category", "personal"),
                    "status": "in-progress",
                    "origin": "discovered",
                    "thread_ids": thread_ids,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                }
                discovered_by_id[ws_id] = ws_dict

                for tid in thread_ids:
                    assignments[tid] = ws_id
                    total_assigned += 1

        _save_discovered(list(discovered_by_id.values()))

    _save_assignments(assignments)
    return total_assigned


def get_discovered_workstreams(threads: list[Thread]) -> list[Workstream]:
    """Load discovered workstreams from cache and attach thread data.

    Returns Workstream objects with origin=DISCOVERED, ready for display.
    """
    discovered = _load_discovered()
    assignments = _load_assignments()
    thread_map = {t.thread_id: t for t in threads}
    result = []

    for ws_dict in discovered:
        try:
            # Build thread_ids from both the ws definition and assignment cache
            thread_ids = set(ws_dict.get("thread_ids", []))
            # Also find threads assigned to this ws via the assignment cache
            ws_id = ws_dict["id"]
            for tid, assigned_ws_id in assignments.items():
                if assigned_ws_id == ws_id:
                    thread_ids.add(tid)

            # Determine live status from thread data
            is_live = any(
                thread_map[tid].is_live
                for tid in thread_ids
                if tid in thread_map
            )

            # Build the most recent activity timestamp
            last_activity = ""
            last_user_act = ""
            for tid in thread_ids:
                if tid in thread_map:
                    t = thread_map[tid]
                    if t.last_activity > last_activity:
                        last_activity = t.last_activity
                    if t.last_user_activity > last_user_act:
                        last_user_act = t.last_user_activity

            ws = Workstream(
                id=ws_dict["id"],
                name=ws_dict.get("name", ""),
                description=ws_dict.get("description", ""),
                status=Status(ws_dict.get("status", "in-progress")),
                category=Category(ws_dict.get("category", "personal")),
                origin=Origin.DISCOVERED,
                thread_ids=list(thread_ids),
                created_at=ws_dict.get("created_at", ""),
                updated_at=last_activity or ws_dict.get("updated_at", ""),
                last_user_activity=last_user_act or last_activity or ws_dict.get("updated_at", ""),
            )
            # Inject live status as a dynamic attribute
            ws._is_live = is_live
            result.append(ws)
        except (KeyError, ValueError):
            continue

    return result


def get_assigned_thread_ids() -> set[str]:
    """Get all thread IDs that have been assigned to a workstream."""
    assignments = _load_assignments()
    return set(assignments.keys())


def pin_workstream(ws_id: str, store) -> bool:
    """Promote a discovered workstream to the user's data.json.

    Returns True if successful.
    """
    discovered = _load_discovered()
    ws_dict = None
    remaining = []

    for d in discovered:
        if d["id"] == ws_id:
            ws_dict = d
        else:
            remaining.append(d)

    if not ws_dict:
        return False

    # Create a proper Workstream and add to store
    ws = Workstream(
        id=ws_dict["id"],
        name=ws_dict.get("name", ""),
        description=ws_dict.get("description", ""),
        status=Status(ws_dict.get("status", "in-progress")),
        category=Category(ws_dict.get("category", "personal")),
        origin=Origin.MERGED,
        thread_ids=ws_dict.get("thread_ids", []),
    )
    store.add(ws)

    # Remove from discovered cache
    _save_discovered(remaining)
    return True


def dismiss_workstream(ws_id: str) -> bool:
    """Remove a discovered workstream and unassign its threads.

    Returns True if successful.
    """
    discovered = _load_discovered()
    remaining = []
    dismissed_thread_ids = set()

    for d in discovered:
        if d["id"] == ws_id:
            dismissed_thread_ids.update(d.get("thread_ids", []))
        else:
            remaining.append(d)

    if not dismissed_thread_ids:
        return False

    # Remove thread assignments
    assignments = _load_assignments()
    for tid in dismissed_thread_ids:
        assignments.pop(tid, None)

    _save_discovered(remaining)
    _save_assignments(assignments)
    return True
