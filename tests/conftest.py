"""Shared fixtures for orchestrator tests."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from models import Category, Link, Store, Workstream


@pytest.fixture
def tmp_store(tmp_path):
    """Create a Store backed by a temp file."""
    return Store(path=tmp_path / "test_data.json")


@pytest.fixture
def sample_ws():
    """A simple test workstream."""
    return Workstream(
        name="Test workstream",
        description="A test description",
        category=Category.WORK,
    )


@pytest.fixture
def populated_store(tmp_path):
    """Store pre-populated with diverse workstreams."""
    store = Store(path=tmp_path / "test_data.json")

    ws1 = Workstream(name="Active work item", description="Doing work",
                     category=Category.WORK)
    ws2 = Workstream(name="Blocked task", description="Stuck on something",
                     category=Category.WORK)
    ws3 = Workstream(name="Personal project", description="Fun stuff",
                     category=Category.PERSONAL)
    ws4 = Workstream(name="Done item", description="Completed",
                     category=Category.WORK)
    ws5 = Workstream(name="Meta tooling", description="Orchestrator improvements",
                     category=Category.WORK)
    ws6 = Workstream(name="Review needed", description="PR open",
                     category=Category.WORK)

    # Make ws3 stale (updated >24h ago)
    old_time = (datetime.now() - timedelta(hours=48)).isoformat()
    ws3.updated_at = old_time

    for ws in [ws1, ws2, ws3, ws4, ws5, ws6]:
        store.add(ws)

    return store


@pytest.fixture
def ws_with_links():
    """Workstream with various link types."""
    ws = Workstream(name="Linked workstream", category=Category.WORK)
    ws.add_link("worktree", "~/work/repos/project", "project")
    ws.add_link("ticket", "UB-1234", "UB-1234")
    ws.add_link("file", "~/workstreams/notes.md", "notes")
    ws.add_link("url", "https://github.com/org/repo/pull/42", "PR #42")
    return ws


@pytest.fixture
def sample_session_jsonl(tmp_path):
    """Create a sample JSONL session file for testing."""
    project_dir = tmp_path / ".claude" / "projects" / "-tmp-test-project"
    project_dir.mkdir(parents=True)

    session_file = project_dir / "abc12345-6789-0000-1111-222233334444.jsonl"
    lines = [
        json.dumps({
            "type": "custom-title",
            "customTitle": "Test Session",
            "sessionId": "abc12345-6789-0000-1111-222233334444",
            "timestamp": "2026-03-20T10:00:00Z",
        }),
        json.dumps({
            "type": "user",
            "message": {"content": "first question"},
            "timestamp": "2026-03-20T10:00:30Z",
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 300,
                },
            },
            "timestamp": "2026-03-20T10:01:00Z",
        }),
        json.dumps({
            "type": "user",
            "message": {"content": "follow up"},
            "timestamp": "2026-03-20T10:03:00Z",
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 1000,
                },
            },
            "timestamp": "2026-03-20T10:05:00Z",
        }),
    ]
    session_file.write_text("\n".join(lines) + "\n")
    return tmp_path / ".claude" / "projects", session_file
