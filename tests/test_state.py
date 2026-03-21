"""Tests for state.py — the pure Python business logic layer.

These tests are fast, synchronous, and don't touch Textual at all.
This is where the high-value regression protection lives.
"""

import pytest
from datetime import datetime, timedelta

from models import Category, Link, Status, Store, TodoItem, Workstream, Origin
from sessions import ClaudeSession
from threads import Thread, ThreadActivity
from state import AppState
from rendering import ViewMode


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
                     category=Category.WORK, status=Status.IN_PROGRESS)
    ws2 = Workstream(name="Blocked task", description="Stuck on something",
                     category=Category.WORK, status=Status.BLOCKED)
    ws3 = Workstream(name="Personal project", description="Fun stuff",
                     category=Category.PERSONAL, status=Status.QUEUED)
    ws4 = Workstream(name="Done item", description="Completed",
                     category=Category.WORK, status=Status.DONE)
    ws5 = Workstream(name="Meta tooling", description="Orchestrator improvements",
                     category=Category.META, status=Status.IN_PROGRESS)
    ws6 = Workstream(name="Review needed", description="PR open",
                     category=Category.WORK, status=Status.AWAITING_REVIEW)

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


# ─── View Navigation ────────────────────────────────────────────────

class TestViewNavigation:
    def test_initial_view_is_workstreams(self, state):
        assert state.view_mode == ViewMode.WORKSTREAMS

    def test_next_view_cycles(self, state):
        state.next_view()
        assert state.view_mode == ViewMode.SESSIONS
        state.next_view()
        assert state.view_mode == ViewMode.ARCHIVED
        state.next_view()
        assert state.view_mode == ViewMode.WORKSTREAMS

    def test_prev_view_cycles(self, state):
        state.prev_view()
        assert state.view_mode == ViewMode.ARCHIVED
        state.prev_view()
        assert state.view_mode == ViewMode.SESSIONS
        state.prev_view()
        assert state.view_mode == ViewMode.WORKSTREAMS

    def test_next_and_prev_are_inverse(self, state):
        state.next_view()
        state.prev_view()
        assert state.view_mode == ViewMode.WORKSTREAMS


# ─── Filtering ───────────────────────────────────────────────────────

class TestFiltering:
    def test_filter_all(self, populated_state):
        populated_state.set_filter("all")
        items = populated_state.get_filtered_streams()
        assert len(items) == 6

    def test_filter_work(self, populated_state):
        populated_state.set_filter("work")
        items = populated_state.get_filtered_streams()
        assert all(w.category == Category.WORK for w in items)
        assert len(items) == 4  # Active work, Blocked, Done, Review

    def test_filter_personal(self, populated_state):
        populated_state.set_filter("personal")
        items = populated_state.get_filtered_streams()
        assert all(w.category == Category.PERSONAL for w in items)
        assert len(items) == 1

    def test_filter_active(self, populated_state):
        populated_state.set_filter("active")
        items = populated_state.get_filtered_streams()
        assert all(w.is_active for w in items)
        assert len(items) == 3  # IN_PROGRESS (2) + AWAITING_REVIEW (1)

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
        populated_state.set_filter("work")
        populated_state.set_search("blocked")
        items = populated_state.get_filtered_streams()
        assert len(items) == 1
        assert items[0].category == Category.WORK


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
        assert cats[0] in ("meta", "personal", "work")

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

    def test_get_ws_finds_discovered(self, populated_state):
        disc = Workstream(id="disc001", name="Discovered", origin=Origin.DISCOVERED)
        populated_state.discovered_ws = [disc]
        found = populated_state.get_ws("disc001")
        assert found is not None
        assert found.name == "Discovered"

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


# ─── Status Cycling ──────────────────────────────────────────────────

class TestStatusCycling:
    def test_cycle_forward(self, populated_state):
        ws = populated_state.store.active[0]
        old_status = ws.status
        result = populated_state.cycle_status(ws.id)
        assert result is not None
        assert result.status != old_status

    def test_cycle_backward(self, populated_state):
        ws = populated_state.store.active[0]
        old_status = ws.status
        result = populated_state.cycle_status(ws.id, forward=False)
        assert result is not None
        assert result.status != old_status

    def test_cycle_wraps_around(self, state):
        ws = Workstream(name="test", status=Status.BLOCKED)
        state.store.add(ws)
        statuses_seen = set()
        current = ws
        for _ in range(len(Status)):
            current = state.cycle_status(ws.id)
            statuses_seen.add(current.status)
        assert len(statuses_seen) == len(Status)

    def test_cycle_nonexistent_returns_none(self, state):
        assert state.cycle_status("nonexistent") is None

    def test_cycle_persists(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.cycle_status(ws.id)
        # Reload
        populated_state.store.load()
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.status != Status.IN_PROGRESS or ws.status != Status.IN_PROGRESS


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
        discovered = []
        state.update_sessions(sessions, threads, discovered)
        assert len(state.sessions) == 2
        assert len(state.threads) == 0

    def test_find_ws_for_session_by_link(self, state):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("claude-session", "abc123", "session")
        state.store.add(ws)

        session = _make_session("abc123")
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_by_directory(self, state, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("worktree", str(project_dir), "project")
        state.store.add(ws)

        session = _make_session(project_path=str(project_dir))
        found = state.find_ws_for_session(session)
        assert found is not None
        assert found.id == ws.id

    def test_find_ws_for_session_not_found(self, state):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        state.store.add(ws)
        session = _make_session(project_path="/some/other/path")
        assert state.find_ws_for_session(session) is None


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

    def test_includes_discovered_workstreams(self, populated_state):
        disc = Workstream(id="disc001", name="Discovered", origin=Origin.DISCOVERED,
                          category=Category.WORK)
        populated_state.discovered_ws = [disc]
        items = populated_state.get_unified_items()
        assert len(items) == 7

    def test_search_filters_discovered(self, populated_state):
        disc = Workstream(id="disc001", name="Special Discovery", origin=Origin.DISCOVERED)
        populated_state.discovered_ws = [disc]
        populated_state.set_search("special")
        items = populated_state.get_unified_items()
        # Only the discovered one should match
        assert any(w.name == "Special Discovery" for w in items)

    def test_category_filter_applies_to_discovered(self, populated_state):
        disc = Workstream(id="disc001", name="Personal disc", origin=Origin.DISCOVERED,
                          category=Category.PERSONAL)
        populated_state.discovered_ws = [disc]
        populated_state.set_filter("work")
        items = populated_state.get_unified_items()
        assert not any(w.id == "disc001" for w in items)


# ─── Command Execution ───────────────────────────────────────────────

class TestCommandExecution:
    def test_view_command(self, state):
        result = state.execute_command("sessions")
        assert result["action"] == "view"
        assert state.view_mode == ViewMode.SESSIONS

    def test_sort_command(self, state):
        result = state.execute_command("sort name")
        assert result["action"] == "refresh"
        assert state.sort_mode == "name"

    def test_sort_invalid(self, state):
        result = state.execute_command("sort bogus")
        assert result["action"] == "error"

    def test_filter_command(self, state):
        result = state.execute_command("filter work")
        assert result["action"] == "refresh"
        assert state.filter_mode == "work"

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

    def test_status_command(self, populated_state):
        ws = populated_state.store.active[0]
        result = populated_state.execute_command("status done", ws.id)
        assert result["action"] == "refresh"
        reloaded = populated_state.store.get(ws.id)
        assert reloaded.status == Status.DONE

    def test_status_invalid(self, populated_state):
        ws = populated_state.store.active[0]
        result = populated_state.execute_command("status bogus", ws.id)
        assert result["action"] == "error"

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
        assert ws.status == Status.IN_PROGRESS
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
        ws = Workstream(name="from-sessions")
        ws.add_link(kind="file", value=str(d), label="dir")  # non-git link
        state.store.add(ws)
        # Add a session that matches this directory
        state.sessions = [_make_session("s1", project_path=str(d))]
        count = state.infer_repo_paths()
        assert count == 1
        assert state.store.get(ws.id).repo_path == str(d)
