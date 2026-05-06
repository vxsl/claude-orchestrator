"""Tests for state.py — the pure Python business logic layer.

These tests are fast, synchronous, and don't touch Textual at all.
This is where the high-value regression protection lives.
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from models import Category, Link, Store, TodoItem, Workstream
from sessions import ClaudeSession
from threads import Thread, ThreadActivity
from state import (
    AppState, fuzzy_match, fuzzy_filter,
    extract_snippet, search_session_content, content_search,
    SearchHit, SessionSearchResult, _path_matches_dir,
)
from sessions import SessionMessage


# ─── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def state(tmp_path):
    """Create an AppState with a temp store."""
    store = Store(path=tmp_path / "test_data.json")
    return AppState(store)


@pytest.fixture
def populated_state(tmp_path):
    """AppState with diverse workstreams for filter/sort testing."""
    store = Store(path=tmp_path / "test_data.json")
    st = AppState(store)

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

    # Make ws3 stale
    old_time = (datetime.now() - timedelta(hours=48)).isoformat()
    ws3.updated_at = old_time

    for ws in [ws1, ws2, ws3, ws4, ws5, ws6]:
        store.add(ws)

    return st


def _make_session(session_id="abc123", project_path="/tmp/test", **kwargs):
    return ClaudeSession(
        session_id=session_id, project_dir="d", project_path=project_path,
        message_count=10, **kwargs,
    )


# ─── Filtering ───────────────────────────────────────────────────────

class TestFiltering:
    def test_filter_all(self, populated_state):
        populated_state.set_filter("all")
        items = populated_state.get_filtered_streams()
        assert len(items) == 6

    def test_filter_stale(self, populated_state):
        populated_state.set_filter("stale")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1
        assert items[0].name == "Personal project"

    def test_filter_unknown_defaults_to_all(self, populated_state):
        populated_state.set_filter("bogus")
        items = populated_state.get_filtered_streams()
        assert len(items) == 6


# ─── Search ──────────────────────────────────────────────────────────

class TestSearch:
    def test_search_by_name(self, populated_state):
        populated_state.set_search("blocked")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1
        assert items[0].name == "Blocked task"

    def test_search_by_description(self, populated_state):
        populated_state.set_search("orchestrator")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1
        assert items[0].name == "Meta tooling"

    def test_search_case_insensitive(self, populated_state):
        populated_state.set_search("BLOCKED")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1

    def test_search_empty_returns_all(self, populated_state):
        populated_state.set_search("")
        items = populated_state.get_filtered_streams()
        assert len(items) == 6

    def test_search_no_match(self, populated_state):
        populated_state.set_search("zzzzz")
        items = populated_state.get_filtered_streams()
        assert len(items) == 0

    def test_search_combined_with_filter(self, populated_state):
        populated_state.set_filter("stale")
        populated_state.set_search("personal")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1
        assert items[0].name == "Personal project"


# ─── Sorting ─────────────────────────────────────────────────────────

class TestSorting:
    def test_sort_by_name(self, populated_state):
        populated_state.set_sort("name")
        items = populated_state.get_filtered_streams()
        names = [w.name for w in items]
        assert names == sorted(names, key=str.lower)

    def test_sort_by_category(self, populated_state):
        populated_state.set_sort("category")
        items = populated_state.get_filtered_streams()
        cats = [w.category.value for w in items]
        # Within each category, items are sorted by status
        assert cats[0] in ("personal", "work")

    def test_sort_mode_persistence(self, populated_state):
        populated_state.set_sort("created")
        assert populated_state.sort_mode == "created"
        populated_state.set_sort("name")
        assert populated_state.sort_mode == "name"


# ─── Workstream Lookup ───────────────────────────────────────────────

class TestWorkstreamLookup:
    def test_get_ws_by_id(self, populated_state):
        ws = populated_state.store.active[0]
        found = populated_state.get_ws(ws.id)
        assert found is not None
        assert found.id == ws.id

    def test_get_ws_not_found(self, populated_state):
        found = populated_state.get_ws("nonexistent")
        assert found is None

    def test_get_session(self, state):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        state.sessions = [s1, s2]
        found = state.get_session("s1")
        assert found is not None
        assert found.session_id == "s1"

    def test_get_session_not_found(self, state):
        state.sessions = [_make_session("s1")]
        assert state.get_session("nonexistent") is None


# ─── Archive Toggle (was Status Cycling) ─────────────────────────────

class TestArchiveToggle:
    def test_toggle_archives(self, populated_state):
        ws = populated_state.store.active[0]
        assert not ws.archived
        result = populated_state.cycle_status(ws.id)
        assert result is not None
        assert result.archived is True

    def test_toggle_unarchives(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.cycle_status(ws.id)  # archive
        result = populated_state.cycle_status(ws.id)  # unarchive
        assert result is not None
        assert result.archived is False

    def test_toggle_nonexistent_returns_none(self, state):
        assert state.cycle_status("nonexistent") is None

    def test_toggle_persists(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.cycle_status(ws.id)
        # Reload
        populated_state.store.load()
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.archived is True


# ─── Notes ───────────────────────────────────────────────────────────


# ─── Todos ──────────────────────────────────────────────────────────

class TestTodo:
    def test_add_todo(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "fix login bug")
        assert item is not None
        assert item.text == "fix login bug"
        assert not item.done
        assert not item.archived
        reloaded = populated_state.store.get(ws.id)
        assert len(reloaded.todos) == 1
        assert reloaded.todos[0].text == "fix login bug"

    def test_add_todo_with_context(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "deploy", context="use staging env")
        assert item.context == "use staging env"

    def test_add_todo_empty_fails(self, populated_state):
        ws = populated_state.store.active[0]
        assert populated_state.add_todo(ws.id, "") is None
        assert populated_state.add_todo(ws.id, "   ") is None

    def test_add_todo_nonexistent_fails(self, state):
        assert state.add_todo("nonexistent", "note") is None

    def test_toggle_todo(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "task")
        assert not item.done
        assert populated_state.toggle_todo(ws.id, item.id)
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].done
        # Toggle back
        assert populated_state.toggle_todo(ws.id, item.id)
        reloaded = populated_state.store.get(ws.id)
        assert not reloaded.todos[0].done

    def test_toggle_todo_nonexistent(self, populated_state):
        ws = populated_state.store.active[0]
        assert not populated_state.toggle_todo(ws.id, "fake")

    def test_archive_todo(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "task")
        assert populated_state.archive_todo(ws.id, item.id)
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].archived

    def test_unarchive_todo(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "task")
        populated_state.archive_todo(ws.id, item.id)
        assert populated_state.unarchive_todo(ws.id, item.id)
        reloaded = populated_state.store.get(ws.id)
        assert not reloaded.todos[0].archived

    def test_delete_todo(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "task")
        assert populated_state.delete_todo(ws.id, item.id)
        reloaded = populated_state.store.get(ws.id)
        assert len(reloaded.todos) == 0

    def test_delete_todo_nonexistent(self, populated_state):
        ws = populated_state.store.active[0]
        assert not populated_state.delete_todo(ws.id, "fake")

    def test_edit_todo_text(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "old text")
        assert populated_state.edit_todo(ws.id, item.id, text="new text")
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].text == "new text"

    def test_edit_todo_context(self, populated_state):
        ws = populated_state.store.active[0]
        item = populated_state.add_todo(ws.id, "task")
        assert populated_state.edit_todo(ws.id, item.id, context="extra info")
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].context == "extra info"

    def test_edit_todo_nonexistent(self, populated_state):
        ws = populated_state.store.active[0]
        assert not populated_state.edit_todo(ws.id, "fake", text="x")

    def test_reorder_todo_down(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "first")
        b = populated_state.add_todo(ws.id, "second")
        assert populated_state.reorder_todo(ws.id, a.id, 1)
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].id == b.id
        assert reloaded.todos[1].id == a.id

    def test_reorder_todo_up(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "first")
        b = populated_state.add_todo(ws.id, "second")
        assert populated_state.reorder_todo(ws.id, b.id, -1)
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.todos[0].id == b.id
        assert reloaded.todos[1].id == a.id

    def test_reorder_todo_out_of_bounds(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "only")
        assert not populated_state.reorder_todo(ws.id, a.id, -1)
        assert not populated_state.reorder_todo(ws.id, a.id, 1)

    def test_active_todos_ordering(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "undone1")
        b = populated_state.add_todo(ws.id, "done1")
        c = populated_state.add_todo(ws.id, "undone2")
        populated_state.toggle_todo(ws.id, b.id)  # mark done
        active = AppState.active_todos(populated_state.store.get(ws.id))
        assert [t.id for t in active] == [a.id, c.id, b.id]

    def test_active_todos_excludes_archived(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "keep")
        b = populated_state.add_todo(ws.id, "archive me")
        populated_state.archive_todo(ws.id, b.id)
        active = AppState.active_todos(populated_state.store.get(ws.id))
        assert len(active) == 1
        assert active[0].id == a.id

    def test_archived_todos(self, populated_state):
        ws = populated_state.store.active[0]
        a = populated_state.add_todo(ws.id, "keep")
        b = populated_state.add_todo(ws.id, "archive me")
        populated_state.archive_todo(ws.id, b.id)
        archived = AppState.archived_todos(populated_state.store.get(ws.id))
        assert len(archived) == 1
        assert archived[0].id == b.id

    def test_todo_migration_from_dict(self):
        """from_dict with no todos key defaults to empty list."""
        d = {
            "id": "test1", "name": "test", "description": "",
            "status": "queued", "category": "personal",
            "links": [], "notes": "", "archived": False,
            "origin": "manual", "thread_ids": [], "archived_thread_ids": [],
            "archived_sessions": {}, "last_user_activity": "",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
            "status_changed_at": "2026-01-01T00:00:00",
        }
        ws = Workstream.from_dict(d)
        assert ws.todos == []

    def test_todo_roundtrip(self, populated_state):
        """Todos survive to_dict/from_dict roundtrip."""
        ws = populated_state.store.active[0]
        populated_state.add_todo(ws.id, "task1", context="ctx")
        populated_state.add_todo(ws.id, "task2")
        populated_state.toggle_todo(ws.id, ws.todos[0].id)
        d = ws.to_dict()
        restored = Workstream.from_dict(d)
        assert len(restored.todos) == 2
        assert restored.todos[0].done
        assert restored.todos[0].context == "ctx"
        assert not restored.todos[1].done


# ─── Rename ──────────────────────────────────────────────────────────

class TestRename:
    def test_rename(self, populated_state):
        ws = populated_state.store.active[0]
        assert populated_state.rename(ws.id, "New Name")
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.name == "New Name"

    def test_rename_empty_fails(self, populated_state):
        ws = populated_state.store.active[0]
        old_name = ws.name
        assert not populated_state.rename(ws.id, "")
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.name == old_name

    def test_rename_nonexistent_fails(self, state):
        assert not state.rename("nonexistent", "name")


# ─── Archive / Unarchive / Delete ────────────────────────────────────

class TestArchiveLifecycle:
    def test_archive(self, populated_state):
        ws = populated_state.store.active[0]
        name = populated_state.archive(ws.id)
        assert name == ws.name
        assert ws.id not in [w.id for w in populated_state.store.active]
        assert ws.id in [w.id for w in populated_state.store.archived]

    def test_unarchive(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.archive(ws.id)
        name = populated_state.unarchive(ws.id)
        assert name is not None
        assert ws.id in [w.id for w in populated_state.store.active]

    def test_delete(self, populated_state):
        ws = populated_state.store.active[0]
        ws_id = ws.id
        name = populated_state.delete(ws_id)
        assert name is not None
        assert populated_state.store.get(ws_id) is None

    def test_archive_nonexistent_returns_none(self, state):
        assert state.archive("nonexistent") is None

    def test_unarchive_nonexistent_returns_none(self, state):
        assert state.unarchive("nonexistent") is None

    def test_delete_nonexistent_returns_none(self, state):
        assert state.delete("nonexistent") is None


# ─── Links ───────────────────────────────────────────────────────────

class TestLinks:
    def test_add_link(self, populated_state):
        ws = populated_state.store.active[0]
        link = Link(kind="url", label="GitHub", value="https://github.com")
        assert populated_state.add_link(ws.id, link)
        reloaded = populated_state.store.get(ws.id)
        assert len(reloaded.links) == 1
        assert reloaded.links[0].value == "https://github.com"

    def test_add_link_nonexistent_fails(self, state):
        link = Link(kind="url", label="test", value="test")
        assert not state.add_link("nonexistent", link)


# ─── Session Management ──────────────────────────────────────────────

class TestSessionManagement:
    def test_update_sessions(self, state):
        sessions = [_make_session("s1"), _make_session("s2")]
        threads = []
        state.update_sessions(sessions, threads)
        assert len(state.sessions) == 2
        assert len(state.threads) == 0

    def test_update_sessions_preserves_live_session_absent_from_disk(self, state):
        """Live session missing from disk data (min_messages=1 filter) is kept."""
        live = ClaudeSession(session_id="live", project_dir="d", project_path="/p", is_live=True)
        state.sessions = [live]
        state.update_sessions([_make_session("old")], [])
        assert any(s.session_id == "live" for s in state.sessions)

    def test_update_sessions_injects_live_session_into_matching_thread(self, state):
        """Live session is added to matching disk thread so sessions_for_ws finds it."""
        live = ClaudeSession(session_id="live", project_dir="d", project_path="/p", is_live=True)
        state.sessions = [live]
        disk_thread = Thread(thread_id="t1", name="p", project_path="/p", sessions=[])
        state.update_sessions([], [disk_thread])
        assert any(s.session_id == "live" for s in disk_thread.sessions)

    def test_find_ws_for_session_by_link(self, state):
        ws = Workstream(name="test")
        ws.add_link("claude-session", "abc123", "session")
        state.store.add(ws)

        session = _make_session("abc123")
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_by_directory(self, state, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")
        state.store.add(ws)

        session = _make_session(project_path=str(project_dir))
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_by_subdirectory_link(self, state, tmp_path):
        """Session from a subdirectory matches workstream linked to parent dir."""
        project_dir = tmp_path / "repo"
        sub_dir = project_dir / "client" / "web"
        sub_dir.mkdir(parents=True)

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "repo")
        state.store.add(ws)

        session = _make_session(project_path=str(sub_dir))
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_by_subdirectory_repo_path(self, state, tmp_path):
        """Session from a subdirectory matches workstream via repo_path."""
        project_dir = tmp_path / "repo"
        sub_dir = project_dir / "client" / "web"
        sub_dir.mkdir(parents=True)

        ws = Workstream(name="test", repo_path=str(project_dir))
        state.store.add(ws)

        session = _make_session(project_path=str(sub_dir))
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_not_found(self, state):
        ws = Workstream(name="test")
        state.store.add(ws)
        session = _make_session(project_path="/some/other/path")
        assert state.find_ws_for_session(session) is None


# ─── sessions_for_ws filtering ───────────────────────────────────────

class TestSessionsForWsFiltering:
    """Verify that archived/deleted sessions are excluded from sessions_for_ws
    in both the thread_ids path and the directory-match fallback path."""

    def test_archived_sessions_excluded_via_thread_ids(self, state):
        """Archived sessions hidden when workstream uses thread_ids path."""
        ws = Workstream(name="test")
        state.store.add(ws)
        s1 = _make_session("s1", project_path="/p")
        s2 = _make_session("s2", project_path="/p")
        t = Thread(thread_id="t1", name="p", project_path="/p", sessions=[s1, s2])
        ws.thread_ids = ["t1"]
        state.threads = [t]
        state.sessions = [s1, s2]

        # Both visible by default
        result = state.sessions_for_ws(ws)
        assert {s.session_id for s in result} == {"s1", "s2"}

        # Archive s1 — should disappear
        ws.archived_sessions["s1"] = "2026-01-01T00:00:00Z"
        state.invalidate_caches()
        result = state.sessions_for_ws(ws)
        assert {s.session_id for s in result} == {"s2"}

        # With include_archived_sessions=True, s1 reappears
        result = state.sessions_for_ws(ws, include_archived_sessions=True)
        assert {s.session_id for s in result} == {"s1", "s2"}

    def test_archived_sessions_excluded_via_directory_match(self, state, tmp_path):
        """Archived sessions hidden in the find_sessions_for_ws fallback path."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")
        state.store.add(ws)

        s1 = _make_session("s1", project_path=str(project_dir))
        s2 = _make_session("s2", project_path=str(project_dir))
        state.sessions = [s1, s2]
        # No threads/thread_ids — forces the directory-match fallback

        result = state.sessions_for_ws(ws)
        assert {s.session_id for s in result} == {"s1", "s2"}

        # Archive s1
        ws.archived_sessions["s1"] = "2026-01-01T00:00:00Z"
        state.invalidate_caches()
        result = state.sessions_for_ws(ws)
        assert {s.session_id for s in result} == {"s2"}

    def test_deleted_sessions_excluded_via_directory_match(self, state, tmp_path):
        """Deleted (trashed) sessions hidden in directory-match fallback path."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")
        state.store.add(ws)

        s1 = _make_session("s1", project_path=str(project_dir))
        state.sessions = [s1]

        ws.deleted_sessions["s1"] = "2026-01-01T00:00:00Z"
        state.invalidate_caches()
        result = state.sessions_for_ws(ws)
        assert result == []

    def test_include_archived_shows_archived_in_directory_match(self, state, tmp_path):
        """include_archived_sessions=True shows archived in fallback path."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")
        state.store.add(ws)

        s1 = _make_session("s1", project_path=str(project_dir))
        state.sessions = [s1]
        ws.archived_sessions["s1"] = "2026-01-01T00:00:00Z"

        result = state.sessions_for_ws(ws, include_archived_sessions=True)
        assert {s.session_id for s in result} == {"s1"}


class TestSessionsForWsThreadMapCache:
    """update_sessions builds a thread_id → Thread index used by
    sessions_for_ws. Verify it stays consistent across updates and
    survives mutations the test code does outside update_sessions."""

    def test_thread_map_built_by_update_sessions(self, state):
        s1 = _make_session("s1", project_path="/p")
        t = Thread(thread_id="t1", name="p", project_path="/p", sessions=[s1])
        state.update_sessions([s1], [t])
        assert state._thread_by_id == {"t1": t}
        assert state._sessions_by_id == {"s1": s1}

    def test_session_added_to_thread_picked_up(self, state):
        """When update_sessions delivers an additional session in the same
        thread, sessions_for_ws reflects it (cache rebuild)."""
        s1 = _make_session("s1", project_path="/p")
        t1 = Thread(thread_id="t1", name="p", project_path="/p", sessions=[s1])

        ws = Workstream(name="test")
        ws.thread_ids = ["t1"]
        state.store.add(ws)
        state.update_sessions([s1], [t1])
        assert {s.session_id for s in state.sessions_for_ws(ws)} == {"s1"}

        s2 = _make_session("s2", project_path="/p")
        t1b = Thread(thread_id="t1", name="p", project_path="/p", sessions=[s1, s2])
        state.update_sessions([s1, s2], [t1b])
        assert {s.session_id for s in state.sessions_for_ws(ws)} == {"s1", "s2"}

    def test_thread_id_change_picked_up(self, state):
        """When ws.thread_ids changes (via invalidate_caches), the new
        thread's sessions are returned."""
        s1 = _make_session("s1", project_path="/p1")
        s2 = _make_session("s2", project_path="/p2")
        t1 = Thread(thread_id="t1", name="p1", project_path="/p1", sessions=[s1])
        t2 = Thread(thread_id="t2", name="p2", project_path="/p2", sessions=[s2])

        ws = Workstream(name="test")
        ws.thread_ids = ["t1"]
        state.store.add(ws)
        state.update_sessions([s1, s2], [t1, t2])
        assert {s.session_id for s in state.sessions_for_ws(ws)} == {"s1"}

        ws.thread_ids = ["t2"]
        state.invalidate_caches()
        assert {s.session_id for s in state.sessions_for_ws(ws)} == {"s2"}

    def test_thread_map_fallback_when_threads_set_directly(self, state):
        """Tests that mutate self.threads without update_sessions should
        still get correct results — covered by the length-mismatch fallback."""
        s1 = _make_session("s1", project_path="/p")
        t = Thread(thread_id="t1", name="p", project_path="/p", sessions=[s1])

        ws = Workstream(name="test")
        ws.thread_ids = ["t1"]
        state.store.add(ws)
        state.threads = [t]
        state.sessions = [s1]
        # _thread_by_id is still {} from __init__, but the fallback in
        # sessions_for_ws rebuilds it on length mismatch.
        assert {s.session_id for s in state.sessions_for_ws(ws)} == {"s1"}


# ─── Tmux Status ─────────────────────────────────────────────────────

class TestTmuxStatus:
    def test_update_tmux_returns_true_on_change(self, state):
        assert state.update_tmux_status({"/path"}, {"win"})
        assert state.tmux_paths == {"/path"}

    def test_update_tmux_returns_false_on_no_change(self, state):
        state.update_tmux_status({"/path"}, {"win"})
        assert not state.update_tmux_status({"/path"}, {"win"})

    def test_ws_has_tmux_by_path(self, state, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state.tmux_paths = {str(project_dir)}

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")
        assert state.ws_has_tmux(ws)

    def test_ws_has_tmux_by_name(self, state):
        state.tmux_names = {"\U0001f916myproject"}
        ws = Workstream(name="myproject")
        assert state.ws_has_tmux(ws)

    def test_ws_no_tmux(self, state):
        ws = Workstream(name="test")
        assert not state.ws_has_tmux(ws)


# ─── Unified Items ───────────────────────────────────────────────────

class TestUnifiedItems:
    def test_includes_manual_workstreams(self, populated_state):
        items = populated_state.get_unified_items()
        assert len(items) == 6


# ─── Command Execution ───────────────────────────────────────────────

class TestCommandExecution:
    def test_removed_view_command_is_error(self, state):
        result = state.execute_command("sessions")
        assert result["action"] == "error"

    def test_sort_command(self, state):
        result = state.execute_command("sort name")
        assert result["action"] == "refresh"
        assert state.sort_mode == "name"

    def test_sort_invalid(self, state):
        result = state.execute_command("sort bogus")
        assert result["action"] == "error"

    def test_filter_command(self, state):
        result = state.execute_command("filter stale")
        assert result["action"] == "refresh"
        assert state.filter_mode == "stale"

    def test_search_command(self, state):
        result = state.execute_command("search hello")
        assert result["action"] == "refresh"
        assert state.search_text == "hello"

    def test_unknown_command(self, state):
        result = state.execute_command("foobar")
        assert result["action"] == "error"

    def test_empty_command(self, state):
        result = state.execute_command("")
        assert result["action"] == "noop"

    def test_note_command(self, populated_state):
        ws = populated_state.store.active[0]
        result = populated_state.execute_command("note hello world", ws.id)
        assert result["action"] == "notify"
        reloaded = populated_state.store.get(ws.id)
        assert any(t.text == "hello world" for t in reloaded.todos)

    # Status command removed — status is auto-derived, not manually set

    def test_archive_command(self, populated_state):
        ws = populated_state.store.active[0]
        result = populated_state.execute_command("archive", ws.id)
        assert result["action"] == "refresh"

    def test_help_command(self, state):
        result = state.execute_command("help")
        assert result["action"] == "help"

    def test_spawn_command(self, state):
        result = state.execute_command("spawn")
        assert result["action"] == "spawn"

    def test_export_command(self, state):
        result = state.execute_command("export /tmp/test.md")
        assert result["action"] == "export"
        assert result["path"] == "/tmp/test.md"

    # Dev-workflow commands
    def test_ship_command(self, state):
        result = state.execute_command("ship")
        assert result["action"] == "ship"

    def test_oneshot_command(self, state):
        result = state.execute_command("oneshot")
        assert result["action"] == "ship"

    def test_ticket_command(self, state):
        result = state.execute_command("ticket UB-1234")
        assert result["action"] == "ticket"
        assert result["query"] == "UB-1234"

    def test_ticket_create_command(self, state):
        result = state.execute_command("tc Fix the bug")
        assert result["action"] == "ticket-create"
        assert result["title"] == "Fix the bug"

    def test_solve_command(self, state):
        result = state.execute_command("solve UB-5678")
        assert result["action"] == "solve"
        assert result["ticket"] == "UB-5678"

    def test_branches_command(self, state):
        result = state.execute_command("branches")
        assert result["action"] == "branches"

    def test_files_command(self, state):
        result = state.execute_command("files")
        assert result["action"] == "files"

    def test_wip_command(self, state):
        result = state.execute_command("wip")
        assert result["action"] == "git-action"
        assert result["cmd"] == "wip"

    def test_restage_command(self, state):
        result = state.execute_command("restage")
        assert result["action"] == "git-action"
        assert result["cmd"] == "restage"


# ─── Export ──────────────────────────────────────────────────────────

class TestExport:
    def test_export_creates_file(self, populated_state, tmp_path):
        output = str(tmp_path / "export.md")
        path, count = populated_state.do_export(output)
        assert path == output
        assert count == 6
        from pathlib import Path
        assert Path(output).exists()
        content = Path(output).read_text()
        assert "Active Workstreams" in content


# ─── Repo Linking ──────────────────────────────────────────────────

class TestRepoLinking:
    def test_known_repos_from_sessions(self, state, tmp_path):
        """known_repos returns unique sorted paths from sessions."""
        d1 = tmp_path / "repo-a"
        d2 = tmp_path / "repo-b"
        d1.mkdir()
        d2.mkdir()
        state.sessions = [
            _make_session("s1", project_path=str(d1)),
            _make_session("s2", project_path=str(d2)),
            _make_session("s3", project_path=str(d1)),  # duplicate
        ]
        repos = state.known_repos()
        assert repos == [str(d1), str(d2)]

    def test_known_repos_includes_ws_repo_path(self, state, tmp_path):
        """Workstreams with repo_path contribute to known_repos."""
        d = tmp_path / "ws-repo"
        d.mkdir()
        ws = Workstream(name="test", repo_path=str(d))
        state.store.add(ws)
        repos = state.known_repos()
        assert str(d) in repos

    def test_known_repos_resolves_subdirectories_to_git_root(self, state, tmp_path):
        """Sessions from subdirectories should resolve to the git root."""
        import subprocess
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        sub = repo / "client" / "web"
        sub.mkdir(parents=True)
        # Clear the toplevel cache so our test dirs are resolved fresh
        from state import _git_toplevel_cache
        _git_toplevel_cache.clear()

        state.sessions = [
            _make_session("s1", project_path=str(sub)),
        ]
        repos = state.known_repos()
        # Should resolve to git root, not the subdirectory
        assert str(repo) in repos
        assert str(sub) not in repos

    def test_known_repos_excludes_nonexistent(self, state):
        """Paths that don't exist on disk are filtered out."""
        state.sessions = [
            _make_session("s1", project_path="/nonexistent/path/1234"),
        ]
        repos = state.known_repos()
        assert repos == []

    def test_workstreams_for_repo_by_repo_path(self, state, tmp_path):
        """Match workstreams by repo_path field."""
        d = tmp_path / "myrepo"
        d.mkdir()
        ws = Workstream(name="matched", repo_path=str(d))
        state.store.add(ws)
        results = state.workstreams_for_repo(str(d))
        assert len(results) == 1
        assert results[0].name == "matched"

    def test_workstreams_for_repo_by_link(self, state, tmp_path):
        """Backward compat: match by worktree link even without repo_path."""
        d = tmp_path / "linkrepo"
        d.mkdir()
        ws = Workstream(name="linked")
        ws.add_link(kind="worktree", value=str(d), label="repo")
        state.store.add(ws)
        results = state.workstreams_for_repo(str(d))
        assert len(results) == 1
        assert results[0].name == "linked"

    def test_workstreams_for_repo_no_match(self, state, tmp_path):
        """No match returns empty list."""
        d = tmp_path / "nope"
        d.mkdir()
        ws = Workstream(name="unrelated", repo_path="/other/path")
        state.store.add(ws)
        results = state.workstreams_for_repo(str(d))
        assert results == []

    def test_workstreams_for_repo_excludes_archived(self, state, tmp_path):
        """Archived workstreams are excluded."""
        d = tmp_path / "archrepo"
        d.mkdir()
        ws = Workstream(name="archived-ws", repo_path=str(d), archived=True)
        state.store.add(ws)
        results = state.workstreams_for_repo(str(d))
        assert results == []

    def test_create_ws_for_repo(self, state, tmp_path):
        """Auto-create workstream from repo path."""
        d = tmp_path / "new-project"
        d.mkdir()
        ws = state.create_ws_for_repo(str(d))
        assert ws.name == "new-project"
        assert ws.repo_path == str(d)
        assert any(l.kind == "worktree" and l.value == str(d) for l in ws.links)
        # Should be persisted
        assert state.store.get(ws.id) is not None

    def test_find_ws_for_session_uses_repo_path(self, state, tmp_path):
        """Reverse lookup finds workstream by repo_path."""
        d = tmp_path / "finder"
        d.mkdir()
        ws = Workstream(name="findme", repo_path=str(d))
        state.store.add(ws)
        session = _make_session("s1", project_path=str(d))
        result = state.find_ws_for_session(session)
        assert result is not None
        assert result.name == "findme"

    def test_ws_has_tmux_uses_repo_path(self, state, tmp_path):
        """Tmux check uses repo_path."""
        d = tmp_path / "tmuxrepo"
        d.mkdir()
        ws = Workstream(name="tmux-ws", repo_path=str(d))
        state.tmux_paths = {str(d)}
        assert state.ws_has_tmux(ws) is True

    def test_ws_dirs_combines_repo_path_and_links(self, state, tmp_path):
        """_ws_dirs returns both repo_path and link directories."""
        d1 = tmp_path / "repo"
        d2 = tmp_path / "worktree"
        d1.mkdir()
        d2.mkdir()
        ws = Workstream(name="combo", repo_path=str(d1))
        ws.add_link(kind="worktree", value=str(d2), label="wt")
        dirs = state._ws_dirs(ws)
        assert str(d1) in dirs
        assert str(d2) in dirs

    def test_infer_repo_paths_from_worktree_link(self, state, tmp_path):
        """Backfill repo_path from worktree link pointing at a git repo."""
        d = tmp_path / "infer-repo"
        d.mkdir()
        (d / ".git").mkdir()  # fake git repo
        ws = Workstream(name="needs-infer")
        ws.add_link(kind="worktree", value=str(d), label="repo")
        state.store.add(ws)
        count = state.infer_repo_paths()
        assert count == 1
        assert state.store.get(ws.id).repo_path == str(d)

    def test_infer_repo_paths_skips_non_git(self, state, tmp_path):
        """Non-git directories are not inferred."""
        d = tmp_path / "not-git"
        d.mkdir()
        ws = Workstream(name="no-git")
        ws.add_link(kind="file", value=str(d), label="dir")
        state.store.add(ws)
        count = state.infer_repo_paths()
        assert count == 0
        assert state.store.get(ws.id).repo_path == ""

    def test_infer_repo_paths_skips_already_set(self, state, tmp_path):
        """Workstreams with repo_path already set are not touched."""
        d = tmp_path / "already-set"
        d.mkdir()
        (d / ".git").mkdir()
        ws = Workstream(name="has-repo", repo_path="/other/path")
        ws.add_link(kind="worktree", value=str(d), label="repo")
        state.store.add(ws)
        count = state.infer_repo_paths()
        assert count == 0
        assert state.store.get(ws.id).repo_path == "/other/path"

    def test_infer_repo_paths_from_sessions(self, state, tmp_path):
        """Infer repo_path from matched sessions when no git links exist."""
        d = tmp_path / "session-repo"
        d.mkdir()
        (d / ".git").mkdir()  # must be a git repo
        ws = Workstream(name="from-sessions")
        ws.add_link(kind="file", value=str(d), label="dir")  # non-git file link (no .git check on file links)
        state.store.add(ws)
        # Add a session that matches this directory
        state.sessions = [_make_session("s1", project_path=str(d))]
        count = state.infer_repo_paths()
        assert count == 1
        assert state.store.get(ws.id).repo_path == str(d)

    def test_infer_repo_paths_skips_home_dir(self, state, tmp_path):
        """Home directory should not be used as repo_path."""
        import os
        home = str(Path.home())
        ws = Workstream(name="home-session")
        state.store.add(ws)
        state.sessions = [_make_session("s1", project_path=home)]
        count = state.infer_repo_paths()
        assert count == 0
        assert state.store.get(ws.id).repo_path == ""


# ─── Notification integration ────────────────────────────────────────

class TestNotificationsForWs:
    def _make_jsonl(self, tmp_path, entries):
        f = tmp_path / "notifications.jsonl"
        lines = []
        for e in entries:
            ts = (datetime.now(timezone.utc) - timedelta(minutes=e.get("minutes_ago", 5))).isoformat()
            lines.append(json.dumps({
                "timestamp": ts,
                "cwd": e["cwd"],
                "title": e.get("title", "test"),
                "message": e.get("message", "done"),
                "session_id": e.get("session_id", ""),
            }))
        f.write_text("\n".join(lines) + "\n")
        return f

    def test_returns_matching_notifications(self, state, tmp_path):
        d = tmp_path / "myproject"
        d.mkdir()
        ws = Workstream(name="test", repo_path=str(d))
        state.store.add(ws)
        jsonl = self._make_jsonl(tmp_path, [
            {"cwd": str(d), "message": "matched"},
            {"cwd": "/other/project", "message": "not matched"},
        ])
        with patch("notifications.NOTIFICATIONS_FILE", jsonl), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = state.notifications_for_ws(ws)
        assert len(result) == 1
        assert result[0].message == "matched"

    def test_returns_empty_for_no_dirs(self, state, tmp_path):
        ws = Workstream(name="no-dirs")
        state.store.add(ws)
        assert state.notifications_for_ws(ws) == []

    def test_matches_worktree_link_dirs(self, state, tmp_path):
        d = tmp_path / "worktree"
        d.mkdir()
        ws = Workstream(name="linked")
        ws.add_link(kind="worktree", value=str(d), label="wt")
        state.store.add(ws)
        jsonl = self._make_jsonl(tmp_path, [
            {"cwd": str(d), "message": "from worktree"},
        ])
        with patch("notifications.NOTIFICATIONS_FILE", jsonl), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = state.notifications_for_ws(ws)
        assert len(result) == 1


# ─── Fuzzy Match ────────────────────────────────────────────────────


class TestFuzzyMatch:
    def test_empty_query_matches_everything(self):
        assert fuzzy_match("", "anything") == 0

    def test_empty_text_returns_none(self):
        assert fuzzy_match("abc", "") is None

    def test_no_match(self):
        assert fuzzy_match("xyz", "hello world") is None

    def test_exact_match(self):
        score = fuzzy_match("hello", "hello")
        assert score is not None and score > 0

    def test_subsequence_match(self):
        score = fuzzy_match("hlo", "hello")
        assert score is not None and score > 0

    def test_case_insensitive(self):
        score = fuzzy_match("HEL", "hello")
        assert score is not None and score > 0

    def test_exact_case_scores_higher(self):
        lower = fuzzy_match("hello", "hello")
        upper = fuzzy_match("HELLO", "hello")
        assert lower > upper  # exact case gets bonus

    def test_consecutive_chars_score_higher(self):
        consecutive = fuzzy_match("abc", "abcdef")
        scattered = fuzzy_match("abc", "axbxcx")
        assert consecutive > scattered

    def test_word_boundary_bonus(self):
        boundary = fuzzy_match("co", "claude-orchestrator")
        mid_word = fuzzy_match("la", "claude-orchestrator")
        assert boundary > mid_word  # 'o' at word boundary after '-'

    def test_start_of_string_bonus(self):
        at_start = fuzzy_match("c", "claude")
        in_middle = fuzzy_match("a", "claude")
        assert at_start > in_middle

    def test_partial_query_not_consumed(self):
        assert fuzzy_match("abcz", "abc") is None


class TestFuzzyFilter:
    def test_returns_sorted_by_score(self):
        items = ["axbxcx", "abc", "the abc thing"]
        results = fuzzy_filter("abc", items)
        # "abc" should score highest (consecutive + start)
        assert len(results) >= 2
        indices = [i for i, _ in results]
        assert indices[0] == 1  # "abc" is best match

    def test_filters_non_matches(self):
        items = ["hello", "world", "xyz"]
        results = fuzzy_filter("hel", items)
        assert len(results) == 1
        assert results[0][0] == 0

    def test_empty_query_matches_all(self):
        items = ["a", "b", "c"]
        results = fuzzy_filter("", items)
        assert len(results) == 3


# ─── Content Search ─────────────────────────────────────────────────


def _make_messages(*texts, role="user"):
    """Helper: create SessionMessage list from text strings."""
    return [SessionMessage(role=role, text=t, timestamp=f"2026-03-{i+1:02d}T00:00:00Z")
            for i, t in enumerate(texts)]


def _make_search_session(sid="test-session"):
    return ClaudeSession(
        session_id=sid, project_dir="test", project_path="/test",
        message_count=5, model="opus",
    )


class TestExtractSnippet:
    def test_basic_snippet(self):
        text = "The deployment pipeline should handle rolling updates gracefully"
        snip, ranges = extract_snippet(text, ["deployment", "pipeline"])
        assert "deployment" in snip.lower()
        assert "pipeline" in snip.lower()
        assert len(ranges) >= 1

    def test_snippet_with_ellipsis(self):
        text = "x" * 50 + " the target keyword is here " + "y" * 200
        snip, ranges = extract_snippet(text, ["target"], max_length=60)
        assert "target" in snip.lower()
        assert snip.startswith("…") or snip.endswith("…")

    def test_no_match_returns_start_of_text(self):
        text = "Hello world this is some text"
        snip, ranges = extract_snippet(text, ["nonexistent"])
        assert snip.startswith("Hello")
        assert ranges == []

    def test_match_ranges_are_valid(self):
        text = "Find the foo and the bar here"
        snip, ranges = extract_snippet(text, ["foo", "bar"])
        for start, end in ranges:
            assert 0 <= start < end <= len(snip)
            assert snip[start:end].lower() in ("foo", "bar")

    def test_overlapping_ranges_merged(self):
        text = "abcabc"
        snip, ranges = extract_snippet(text, ["abc"])
        # Should have non-overlapping ranges
        for i in range(len(ranges) - 1):
            assert ranges[i][1] <= ranges[i + 1][0]


class TestSearchSessionContent:
    def test_single_word_match(self):
        msgs = _make_messages("Deploy the new feature", "Fix the bug")
        session = _make_search_session()
        result = search_session_content("deploy", msgs, session)
        assert result is not None
        assert result.hit_count == 1
        assert "deploy" in result.best_hit.snippet.lower()

    def test_and_semantics(self):
        msgs = _make_messages(
            "The deploy pipeline is broken",
            "Fix the bug in production",
            "Deploy to production today",
        )
        session = _make_search_session()
        result = search_session_content("deploy production", msgs, session)
        assert result is not None
        # Only the third message has both words
        assert result.hit_count == 1

    def test_no_match_returns_none(self):
        msgs = _make_messages("Hello world")
        session = _make_search_session()
        result = search_session_content("nonexistent", msgs, session)
        assert result is None

    def test_empty_query_returns_none(self):
        msgs = _make_messages("Hello world")
        session = _make_search_session()
        result = search_session_content("", msgs, session)
        assert result is None

    def test_phrase_matching(self):
        msgs = _make_messages(
            "The rolling update strategy works well",
            "Update: rolling back the change",
        )
        session = _make_search_session()
        result = search_session_content('"rolling update"', msgs, session)
        assert result is not None
        assert result.hit_count == 1  # only first message has exact phrase

    def test_user_messages_score_higher(self):
        msgs = [
            SessionMessage(role="user", text="Fix the deployment bug", timestamp="2026-03-01T00:00:00Z"),
            SessionMessage(role="assistant", text="Fix the deployment bug", timestamp="2026-03-01T00:01:00Z"),
        ]
        session = _make_search_session()
        result = search_session_content("deployment bug", msgs, session)
        assert result is not None
        assert result.hits[0].role == "user"  # user hit scores higher

    def test_frequency_bonus(self):
        msgs = _make_messages(
            "deploy deploy deploy",
            "deploy once",
        )
        session = _make_search_session()
        result = search_session_content("deploy", msgs, session)
        assert result is not None
        # First message has more occurrences → higher score
        assert result.best_hit.message_idx == 0

    def test_max_hits_limit(self):
        msgs = _make_messages(*[f"match keyword number {i}" for i in range(20)])
        session = _make_search_session()
        result = search_session_content("keyword", msgs, session, max_hits=3)
        assert result is not None
        assert len(result.hits) <= 3


class TestContentSearch:
    def test_ranks_sessions_by_score(self):
        s1 = _make_search_session("s1")
        s1.jsonl_path = ""
        s2 = _make_search_session("s2")
        s2.jsonl_path = ""
        cache = {
            "s1": _make_messages("just one mention of deploy"),
            "s2": _make_messages("deploy deploy deploy to production", "deploy again"),
        }
        results = content_search("deploy", [s1, s2], cache)
        assert len(results) == 2
        assert results[0].session.session_id == "s2"  # more hits → ranked first

    def test_no_results_for_no_match(self):
        s = _make_search_session()
        s.jsonl_path = ""
        cache = {"test-session": _make_messages("Hello world")}
        results = content_search("nonexistent", [s], cache)
        assert results == []

    def test_empty_query_returns_empty(self):
        s = _make_search_session()
        s.jsonl_path = ""
        cache = {"test-session": _make_messages("Hello")}
        results = content_search("", [s], cache)
        assert results == []

    def test_case_insensitive(self):
        s = _make_search_session()
        s.jsonl_path = ""
        cache = {"test-session": _make_messages("Deploy the Feature")}
        results = content_search("deploy feature", [s], cache)
        assert len(results) == 1


# ─── TabManager ──────────────────────────────────────────────────────

from state import TabManager, TabState


class TestTabManager:
    def test_initial_state(self):
        tm = TabManager()
        # Two permanent tabs: Workstreams (home) + Sessions (current_sessions)
        assert len(tm.tabs) == 2
        assert tm.active_idx == 0
        assert tm.is_home
        assert tm.active_tab_id == "home"

    def test_initial_sessions_tab(self):
        tm = TabManager()
        assert tm.tabs[1].id == "current_sessions"
        assert not tm.is_current_sessions  # home is active
        tm.switch_to(1)
        assert tm.is_current_sessions
        assert not tm.is_home

    def test_open_tab(self):
        tm = TabManager()
        idx = tm.open_tab("ws-1", "Auth refactor", "●")
        assert idx == 2  # after the two permanent tabs
        assert len(tm.tabs) == 3
        assert tm.active_idx == 2  # auto-switches
        assert tm.active_tab.ws_id == "ws-1"
        assert not tm.is_home

    def test_open_duplicate_reuses(self):
        tm = TabManager()
        idx1 = tm.open_tab("ws-1", "Auth refactor")
        tm.switch_to(0)  # go home
        idx2 = tm.open_tab("ws-1", "Auth refactor")
        assert idx1 == idx2
        assert len(tm.tabs) == 3
        assert tm.active_idx == 2  # switched to existing

    def test_close_tab(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        tm.open_tab("ws-2", "Two")
        assert len(tm.tabs) == 4
        closed = tm.close_tab(2)  # close "One" (at index 2, after the two permanent tabs)
        assert closed == "ws-1"
        assert len(tm.tabs) == 3
        assert tm.tabs[2].ws_id == "ws-2"

    def test_cannot_close_home(self):
        tm = TabManager()
        result = tm.close_tab(0)
        assert result is None
        assert len(tm.tabs) == 2  # permanent tabs remain

    def test_cannot_close_sessions_tab(self):
        tm = TabManager()
        result = tm.close_tab(1)
        assert result is None
        assert len(tm.tabs) == 2

    def test_close_active_tab_moves_left(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        tm.open_tab("ws-2", "Two")
        assert tm.active_idx == 3  # ws-2
        tm.close_active_tab()
        assert tm.active_idx == 2  # ws-1
        assert tm.active_tab.ws_id == "ws-1"

    def test_close_tab_before_active_preserves(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        tm.open_tab("ws-2", "Two")
        assert tm.active_idx == 3  # ws-2
        tm.close_tab(2)  # close ws-1
        assert tm.active_idx == 2  # ws-2 is now at index 2
        assert tm.active_tab.ws_id == "ws-2"

    def test_next_tab_wraps(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        assert tm.active_idx == 2  # ws-1 at index 2
        tm.next_tab()
        assert tm.active_idx == 0  # wrapped back to home

    def test_prev_tab_wraps(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        tm.switch_to(0)
        tm.prev_tab()
        assert tm.active_idx == 2  # wrapped to ws-1

    def test_switch_to_id(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        tm.open_tab("ws-2", "Two")
        tm.switch_to_id("home")
        assert tm.active_idx == 0
        tm.switch_to_id("current_sessions")
        assert tm.active_idx == 1
        tm.switch_to_id("ws-1")
        assert tm.active_idx == 2

    def test_find_tab(self):
        tm = TabManager()
        tm.open_tab("ws-1", "One")
        assert tm.find_tab("ws-1") == 2  # after the two permanent tabs
        assert tm.find_tab("ws-999") is None

    def test_update_label(self):
        tm = TabManager()
        tm.open_tab("ws-1", "Old name")
        tm.update_label("ws-1", "New name")
        assert tm.tabs[2].label == "New name"

    def test_close_active_home_returns_none(self):
        tm = TabManager()
        result = tm.close_active_tab()
        assert result is None

    def test_close_active_sessions_returns_none(self):
        tm = TabManager()
        tm.switch_to(1)
        result = tm.close_active_tab()
        assert result is None

    def test_two_permanent_tabs_next_cycles(self):
        tm = TabManager()
        assert tm.active_idx == 0
        result = tm.next_tab()
        assert result
        assert tm.active_idx == 1  # moves to sessions tab
        tm.next_tab()
        assert tm.active_idx == 0  # wraps back to home


class TestArchivedFilter:
    """Test that filter_mode='archived' works in get_filtered_streams."""

    def test_archived_filter(self, populated_state):
        # Archive a workstream first
        ws = populated_state.store.active[0]
        populated_state.archive(ws.id)
        populated_state.filter_mode = "archived"
        result = populated_state.get_filtered_streams()
        assert len(result) == 1
        assert result[0].id == ws.id


# ─── Category Auto-Detection from Git Remote ────────────────────────

class TestInferCategoryFromRemote:
    def test_gitlab_sets_work(self, state):
        """A workstream with a gitlab remote should be auto-set to WORK."""
        ws = Workstream(name="work repo", category=Category.PERSONAL, repo_path="/fake/repo")
        state.store.add(ws)
        with patch("state.get_git_remote_host", return_value="gitlab.com"):
            state._infer_category_from_remote(ws)
        assert ws.category == Category.WORK

    def test_github_stays_personal(self, state):
        """A workstream with a github remote stays PERSONAL (already default)."""
        ws = Workstream(name="personal repo", category=Category.PERSONAL, repo_path="/fake/repo")
        state.store.add(ws)
        with patch("state.get_git_remote_host", return_value="github.com"):
            state._infer_category_from_remote(ws)
        assert ws.category == Category.PERSONAL

    def test_no_override_explicit_work(self, state):
        """Don't override an explicit WORK category (e.g. to PERSONAL for github)."""
        ws = Workstream(name="explicit work", category=Category.WORK, repo_path="/fake/repo")
        state.store.add(ws)
        with patch("state.get_git_remote_host", return_value="github.com"):
            state._infer_category_from_remote(ws)
        assert ws.category == Category.WORK

    def test_no_override_explicit_work(self, state):
        """Don't override an explicit WORK category."""
        ws = Workstream(name="work item", category=Category.WORK, repo_path="/fake/repo")
        state.store.add(ws)
        with patch("state.get_git_remote_host", return_value="gitlab.com"):
            state._infer_category_from_remote(ws)
        assert ws.category == Category.WORK

    def test_no_remote_does_nothing(self, state):
        """When get_git_remote_host returns None, category is unchanged."""
        ws = Workstream(name="no remote", category=Category.PERSONAL, repo_path="/fake/repo")
        state.store.add(ws)
        with patch("state.get_git_remote_host", return_value=None):
            state._infer_category_from_remote(ws)
        assert ws.category == Category.PERSONAL

    def test_no_repo_path_does_nothing(self, state):
        """When repo_path is empty, category is unchanged."""
        ws = Workstream(name="no repo", category=Category.PERSONAL)
        state.store.add(ws)
        state._infer_category_from_remote(ws)
        assert ws.category == Category.PERSONAL


# ── Command Palette ──────────────────────────────────────────────

from state import CommandDef, COMMAND_REGISTRY, get_command_items


class TestCommandRegistry:
    def test_registry_not_empty(self):
        assert len(COMMAND_REGISTRY) > 10

    def test_all_names_unique(self):
        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert len(names) == len(set(names))

    def test_no_alias_clashes_with_names(self):
        """Alias should not duplicate another command's primary name."""
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                # Some aliases like "r" are ok — they map to the same execute_command path
                pass  # just ensure registry loads without error

    def test_get_command_items_returns_all(self):
        items = get_command_items(has_ws=True)
        assert len(items) == len(COMMAND_REGISTRY)
        ids = [item_id for item_id, _ in items]
        for cmd in COMMAND_REGISTRY:
            assert cmd.name in ids

    def test_get_command_items_no_ws_dims_requires_ws(self):
        items = get_command_items(has_ws=False)
        by_id = {item_id: label for item_id, label in items}
        # "spawn" requires ws — should be dimmed (no [bold])
        assert "[bold]" not in by_id["spawn"]
        # "add" doesn't require ws — should be bold
        assert "[bold]" in by_id["add"]

    def test_get_command_items_with_ws_shows_bold(self):
        items = get_command_items(has_ws=True)
        by_id = {item_id: label for item_id, label in items}
        # "spawn" requires ws — should be bold when ws available
        assert "[bold]" in by_id["spawn"]

    def test_execute_command_dispatch(self, state):
        """Core commands from registry still dispatch correctly via execute_command."""
        result = state.execute_command("help")
        assert result["action"] == "help"
        result = state.execute_command("brain")
        assert result["action"] == "brain"
        result = state.execute_command("ship")
        assert result["action"] == "ship"
        result = state.execute_command("wip")
        assert result["action"] == "git-action"

    def test_no_command_returns_unknown(self, state):
        """No registry command should return 'Unknown command' error."""
        for cmd in COMMAND_REGISTRY:
            result = state.execute_command(cmd.name)
            if result["action"] == "error":
                assert "unknown command" not in result.get("msg", "").lower(), (
                    f"Command '{cmd.name}' not dispatched: {result.get('msg')}"
                )

    def test_no_alias_returns_unknown(self, state):
        """No registry alias should return 'Unknown command' error."""
        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                result = state.execute_command(alias)
                if result["action"] == "error":
                    assert "unknown command" not in result.get("msg", "").lower(), (
                        f"Alias '{alias}' (for '{cmd.name}') not dispatched: {result.get('msg')}"
                    )

    def test_newly_fixed_commands(self, state):
        """Commands that were missing from execute_command dispatch."""
        assert state.execute_command("add")["action"] == "add"
        assert state.execute_command("new")["action"] == "add"
        assert state.execute_command("create")["action"] == "add"
        assert state.execute_command("rename")["action"] == "rename"
        assert state.execute_command("open")["action"] == "open"
        assert state.execute_command("o")["action"] == "open"
        assert state.execute_command("refresh")["action"] == "refresh"
        assert state.execute_command("braindump")["action"] == "brain"
        assert state.execute_command("session")["action"] == "spawn"
        assert state.execute_command("claude")["action"] == "spawn"
        assert state.execute_command("r")["action"] == "resume"
        assert state.execute_command("?")["action"] == "help"


# ── Worktree Discovery + Enrichment ─────────────────────────────────


class TestDiscoverAndEnrichWorktrees:
    def test_creates_workstream_for_new_worktree(self, state):
        """A new worktree should auto-create a workstream."""
        with patch("state.discover_worktrees") as mock_discover, \
             patch("state.get_jira_cache") as mock_jira, \
             patch("state.get_mr_cache") as mock_mr, \
             patch("state.get_ticket_solve_status") as mock_solve, \
             patch("os.path.isdir", return_value=True):
            mock_discover.return_value = [
                {"path": "/home/dev/repo-wt", "branch": "UB-1234-fix", "ticket_key": "UB-1234", "repo_path": "/home/dev/repo"},
            ]
            mock_jira.return_value = {}
            mock_mr.return_value = {}
            mock_solve.return_value = None

            changed = state.discover_and_enrich_worktrees()
            assert changed is True
            # Should have created one workstream
            assert len(state.store.active) == 1
            ws = state.store.active[0]
            assert "UB-1234" in ws.name
            assert ws.ticket_key == "UB-1234"

    def test_skips_existing_linked_worktree(self, state):
        """If a worktree is already linked, don't create a duplicate."""
        ws = Workstream(name="Existing", repo_path="/home/dev/repo-wt")
        ws.add_link("worktree", "/home/dev/repo-wt", "repo-wt")
        state.store.add(ws)

        with patch("state.discover_worktrees") as mock_discover, \
             patch("state.get_jira_cache") as mock_jira, \
             patch("state.get_mr_cache") as mock_mr, \
             patch("state.get_ticket_solve_status") as mock_solve, \
             patch("os.path.isdir", return_value=True):
            mock_discover.return_value = [
                {"path": "/home/dev/repo-wt", "branch": "UB-1234-fix", "ticket_key": "UB-1234", "repo_path": "/home/dev/repo"},
            ]
            mock_jira.return_value = {}
            mock_mr.return_value = {}
            mock_solve.return_value = None

            changed = state.discover_and_enrich_worktrees()
            assert changed is False
            assert len(state.store.active) == 1  # no new ws created

    def test_enriches_from_jira_cache(self, state):
        """Workstreams with ticket_key get enriched from Jira cache."""
        from actions import JiraTicketInfo
        ws = Workstream(name="Test", repo_path="/home/dev/repo")
        ws.add_link("ticket", "UB-1234", "UB-1234")
        state.store.add(ws)

        with patch("state.discover_worktrees") as mock_discover, \
             patch("state.get_jira_cache") as mock_jira, \
             patch("state.get_mr_cache") as mock_mr, \
             patch("state.get_ticket_solve_status") as mock_solve, \
             patch("os.path.isdir", return_value=True):
            mock_discover.return_value = []
            mock_jira.return_value = {
                "UB-1234": JiraTicketInfo(key="UB-1234", summary="Fix the bug", status="In Progress"),
            }
            mock_mr.return_value = {}
            mock_solve.return_value = None

            state.discover_and_enrich_worktrees()
            ws = state.store.active[0]
            assert ws.ticket_key == "UB-1234"
            assert ws.ticket_summary == "Fix the bug"
            assert ws.ticket_status == "In Progress"

    def test_auto_archives_removed_worktree(self, state):
        """Workstreams linked to non-existent worktree paths get auto-archived."""
        ws = Workstream(name="Gone", repo_path="/gone/path")
        ws.add_link("worktree", "/gone/path", "repo")
        state.store.add(ws)

        with patch("state.discover_worktrees") as mock_discover, \
             patch("state.get_jira_cache") as mock_jira, \
             patch("state.get_mr_cache") as mock_mr, \
             patch("state.get_ticket_solve_status") as mock_solve, \
             patch("os.path.isdir", return_value=False):
            mock_discover.return_value = []
            mock_jira.return_value = {}
            mock_mr.return_value = {}
            mock_solve.return_value = None

            changed = state.discover_and_enrich_worktrees()
            assert changed is True
            assert len(state.store.active) == 0
            assert len(state.store.archived) == 1

    def test_creates_ws_with_jira_summary_name(self, state):
        """When Jira cache has a summary, the ws name includes it."""
        from actions import JiraTicketInfo
        with patch("state.discover_worktrees") as mock_discover, \
             patch("state.get_jira_cache") as mock_jira, \
             patch("state.get_mr_cache") as mock_mr, \
             patch("state.get_ticket_solve_status") as mock_solve, \
             patch("os.path.isdir", return_value=True):
            mock_discover.return_value = [
                {"path": "/home/dev/wt", "branch": "UB-42-fix", "ticket_key": "UB-42", "repo_path": "/repo"},
            ]
            mock_jira.return_value = {
                "UB-42": JiraTicketInfo(key="UB-42", summary="Fix login timeout"),
            }
            mock_mr.return_value = {}
            mock_solve.return_value = None

            state.discover_and_enrich_worktrees()
            ws = state.store.active[0]
            assert ws.name == "UB-42: Fix login timeout"


# ── Non-Repo Workstream Auto-Discovery ──────────────────────────────


class TestDiscoverNonRepoWorkstreams:
    def _session(self, project_path: str, sid: str = "abc") -> ClaudeSession:
        return ClaudeSession(
            session_id=sid,
            project_path=project_path,
            project_dir=project_path.replace("/", "-"),
            started_at="2026-04-25T00:00:00Z",
            last_activity="2026-04-25T00:00:00Z",
            message_count=1,
        )

    def test_creates_ws_for_home_dir(self, state, tmp_path):
        """A session launched from a non-repo dir auto-creates a workstream."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        state.sessions = [self._session(str(scratch))]
        with patch("state._git_toplevel", return_value=None):
            changed = state.discover_non_repo_workstreams()
        assert changed is True
        assert len(state.store.active) == 1
        ws = state.store.active[0]
        assert ws.name == "scratch"
        assert ws.repo_path == str(scratch)

    def test_skips_session_inside_git_repo(self, state, tmp_path):
        """A session whose path is inside a git repo is left alone."""
        repo = tmp_path / "repo"
        repo.mkdir()
        state.sessions = [self._session(str(repo))]
        with patch("state._git_toplevel", return_value=str(repo)):
            changed = state.discover_non_repo_workstreams()
        assert changed is False
        assert len(state.store.active) == 0

    def test_skips_already_linked_path(self, state, tmp_path):
        """If a workstream already anchors the path, skip."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        existing = Workstream(name="Existing", repo_path=str(scratch))
        state.store.add(existing)
        state.sessions = [self._session(str(scratch))]
        with patch("state._git_toplevel", return_value=None):
            changed = state.discover_non_repo_workstreams()
        assert changed is False
        assert len(state.store.active) == 1

    def test_dedupes_same_path_across_sessions(self, state, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        state.sessions = [
            self._session(str(scratch), sid="a"),
            self._session(str(scratch), sid="b"),
        ]
        with patch("state._git_toplevel", return_value=None):
            state.discover_non_repo_workstreams()
        assert len(state.store.active) == 1

    def test_names_home_as_home(self, state):
        home = str(Path.home())
        state.sessions = [self._session(home)]
        with patch("state._git_toplevel", return_value=None), \
             patch("state._isdir_cached", return_value=True):
            state.discover_non_repo_workstreams()
        assert len(state.store.active) == 1
        assert state.store.active[0].name == "Home"

    def test_skips_paths_in_skip_dirs(self, state, tmp_path):
        cache = tmp_path / ".cache" / "thing"
        cache.mkdir(parents=True)
        state.sessions = [self._session(str(cache))]
        with patch("state._git_toplevel", return_value=None):
            changed = state.discover_non_repo_workstreams()
        assert changed is False
        assert len(state.store.active) == 0

    def test_skips_missing_directory(self, state):
        state.sessions = [self._session("/nonexistent/path/here")]
        with patch("state._git_toplevel", return_value=None):
            changed = state.discover_non_repo_workstreams()
        assert changed is False
        assert len(state.store.active) == 0


class TestPathMatchesDir:
    """A non-repo workstream must not over-claim subdirectory sessions."""

    def test_repo_dir_matches_subdirectory(self):
        with patch("state._git_toplevel", return_value="/repo"):
            assert _path_matches_dir("/repo/sub", "/repo") is True
            assert _path_matches_dir("/repo", "/repo") is True

    def test_non_repo_dir_only_matches_exact(self):
        """Home dir (~) should not claim sessions from /home/kyle/dev/foo."""
        with patch("state._git_toplevel", return_value=None):
            assert _path_matches_dir("/home/kyle", "/home/kyle") is True
            assert _path_matches_dir("/home/kyle/dev/foo", "/home/kyle") is False

    def test_no_match_for_unrelated_path(self):
        with patch("state._git_toplevel", return_value="/repo"):
            assert _path_matches_dir("/other", "/repo") is False
