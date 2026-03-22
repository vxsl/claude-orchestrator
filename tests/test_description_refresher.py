"""Tests for description_refresher.py — lightweight description re-evaluation."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from models import Category, Store, Workstream
from sessions import ClaudeSession
from description_refresher import (
    _ws_context_hash,
    _load_cache,
    _save_cache,
    refresh_descriptions,
    COOLDOWN,
)


# ─── Fixtures ────────────────────────────────────────────────────────

def _make_session(sid="sess-001", project="/home/kyle/dev/foo", last_activity="2026-03-21T10:00:00"):
    return ClaudeSession(
        session_id=sid,
        project_dir="-home-kyle-dev-foo",
        project_path=project,
        last_activity=last_activity,
        message_count=5,
    )


@pytest.fixture
def store(tmp_path):
    return Store(path=tmp_path / "test_data.json")


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("description_refresher.CACHE_FILE", tmp_path / "description-evals.json")
    return tmp_path


# ─── Context hash tests ─────────────────────────────────────────────

def test_context_hash_stable():
    """Same inputs produce same hash."""
    ws = Workstream(name="test ws")
    sessions = [_make_session("a"), _make_session("b")]
    h1 = _ws_context_hash(ws, sessions)
    h2 = _ws_context_hash(ws, sessions)
    assert h1 == h2


def test_context_hash_changes_with_new_session():
    """Adding a session changes the hash."""
    ws = Workstream(name="test ws")
    sessions_a = [_make_session("a")]
    sessions_b = [_make_session("a"), _make_session("b")]
    h1 = _ws_context_hash(ws, sessions_a)
    h2 = _ws_context_hash(ws, sessions_b)
    assert h1 != h2


def test_context_hash_independent_of_order():
    """Session order shouldn't affect the hash."""
    ws = Workstream(name="test ws")
    sessions_a = [_make_session("a"), _make_session("b")]
    sessions_b = [_make_session("b"), _make_session("a")]
    assert _ws_context_hash(ws, sessions_a) == _ws_context_hash(ws, sessions_b)


# ─── Cache tests ────────────────────────────────────────────────────

def test_cache_roundtrip(cache_dir):
    data = {"ws1": {"context_hash": "abc123", "evaluated_at": "2026-03-21T10:00:00", "description": "test"}}
    _save_cache(data)
    loaded = _load_cache()
    assert loaded == data


def test_load_empty_cache(cache_dir):
    assert _load_cache() == {}


# ─── Refresh logic tests ────────────────────────────────────────────

def test_skip_when_no_sessions(store, cache_dir):
    """Workstreams with no matching sessions are skipped."""
    ws = Workstream(name="lonely ws", repo_path="/nonexistent")
    store.add(ws)

    with patch("description_refresher._call_llm") as mock_llm:
        count = refresh_descriptions(store, [])
        assert count == 0
        mock_llm.assert_not_called()


def test_skip_when_context_unchanged(store, cache_dir):
    """Workstreams with unchanged context hash are skipped."""
    ws = Workstream(name="stable ws", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]
    ctx_hash = _ws_context_hash(ws, sessions)

    # Pre-populate cache with current hash
    _save_cache({ws.id: {
        "context_hash": ctx_hash,
        "evaluated_at": datetime.now().isoformat(),
        "description": ws.description,
    }})

    with patch("description_refresher._call_llm") as mock_llm:
        count = refresh_descriptions(store, sessions)
        assert count == 0
        mock_llm.assert_not_called()


def test_skip_when_cooldown_active(store, cache_dir):
    """Workstreams within cooldown period are skipped even with new context."""
    ws = Workstream(name="busy ws", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    # Pre-populate cache with a DIFFERENT hash but recent evaluation
    _save_cache({ws.id: {
        "context_hash": "old_hash",
        "evaluated_at": datetime.now().isoformat(),  # Just evaluated
        "description": ws.description,
    }})

    with patch("description_refresher._call_llm") as mock_llm:
        count = refresh_descriptions(store, sessions)
        assert count == 0
        mock_llm.assert_not_called()


def test_refresh_updates_description(store, cache_dir):
    """Description is updated when LLM returns a new one."""
    ws = Workstream(name="evolving ws", description="old desc", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    with patch("description_refresher._call_llm") as mock_llm:
        mock_llm.return_value = {ws.id: "new description of the work"}
        count = refresh_descriptions(store, sessions)

    assert count == 1
    # Reload from store to verify persistence
    store.load()
    updated = store.get(ws.id)
    assert updated.description == "new description of the work"


def test_refresh_keeps_description_on_keep(store, cache_dir):
    """Description unchanged when LLM says 'keep'."""
    ws = Workstream(name="stable ws", description="good desc", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    with patch("description_refresher._call_llm") as mock_llm:
        mock_llm.return_value = {ws.id: "keep"}
        count = refresh_descriptions(store, sessions)

    assert count == 0
    store.load()
    assert store.get(ws.id).description == "good desc"


def test_refresh_updates_cache_on_keep(store, cache_dir):
    """Cache is updated even when description is kept (tracks evaluation time)."""
    ws = Workstream(name="stable ws", description="good desc", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    with patch("description_refresher._call_llm") as mock_llm:
        mock_llm.return_value = {ws.id: "keep"}
        refresh_descriptions(store, sessions)

    cache = _load_cache()
    assert ws.id in cache
    assert cache[ws.id]["context_hash"] == _ws_context_hash(ws, sessions)


def test_refresh_after_cooldown_expired(store, cache_dir):
    """Workstream is re-evaluated after cooldown expires."""
    ws = Workstream(name="ws", description="old", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    # Pre-populate cache with expired cooldown and different hash
    expired = (datetime.now() - COOLDOWN - timedelta(minutes=1)).isoformat()
    _save_cache({ws.id: {
        "context_hash": "old_hash",
        "evaluated_at": expired,
        "description": "old",
    }})

    with patch("description_refresher._call_llm") as mock_llm:
        mock_llm.return_value = {ws.id: "refreshed description"}
        count = refresh_descriptions(store, sessions)

    assert count == 1
    store.load()
    assert store.get(ws.id).description == "refreshed description"


def test_refresh_skips_archived(store, cache_dir):
    """Archived workstreams are not evaluated."""
    ws = Workstream(name="done ws", repo_path="/home/kyle/dev/foo", archived=True)
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    with patch("description_refresher._call_llm") as mock_llm:
        count = refresh_descriptions(store, sessions)
        assert count == 0
        mock_llm.assert_not_called()


def test_llm_failure_is_graceful(store, cache_dir):
    """LLM returning empty doesn't crash or update descriptions."""
    ws = Workstream(name="ws", description="original", repo_path="/home/kyle/dev/foo")
    store.add(ws)

    sessions = [_make_session("s1", "/home/kyle/dev/foo")]

    with patch("description_refresher._call_llm") as mock_llm:
        mock_llm.return_value = {}
        count = refresh_descriptions(store, sessions)

    assert count == 0
    store.load()
    assert store.get(ws.id).description == "original"
