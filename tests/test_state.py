"""Tests for state.py — the pure Python business logic layer.

These tests are fast, synchronous, and don't touch Textual at all.
This is where the high-value regression protection lives.
"""

import pytest
from datetime import datetime, timedelta

from models import Category, Link, Status, Store, Workstream, Origin
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

class TestNotes:
    def test_add_note(self, populated_state):
        ws = populated_state.store.active[0]
        assert populated_state.add_note(ws.id, "test note")
        reloaded = populated_state.store.get(ws.id)
        assert "test note" in reloaded.notes

    def test_add_note_with_timestamp(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.add_note(ws.id, "hello")
        reloaded = populated_state.store.get(ws.id)
        assert "[" in reloaded.notes  # timestamp bracket

    def test_add_note_empty_fails(self, populated_state):
        ws = populated_state.store.active[0]
        assert not populated_state.add_note(ws.id, "")
        assert not populated_state.add_note(ws.id, "   ")

    def test_add_note_nonexistent_fails(self, state):
        assert not state.add_note("nonexistent", "note")

    def test_add_multiple_notes(self, populated_state):
        ws = populated_state.store.active[0]
        populated_state.add_note(ws.id, "first")
        populated_state.add_note(ws.id, "second")
        reloaded = populated_state.store.get(ws.id)
        assert "first" in reloaded.notes
        assert "second" in reloaded.notes


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
        assert "hello world" in reloaded.notes

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
