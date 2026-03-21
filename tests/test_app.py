"""Tests for app.py — TUI application using Textual's pilot testing."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from models import Category, Link, Status, Store, Workstream
from app import (
    OrchestratorApp,
    ThreadPickerScreen,
    ViewMode,
    _ws_indicators,
    _ws_directories,
    _do_resume,
    _find_sessions_for_ws,
    _launch_orch_claude,
    _short_project,
    _short_model,
    _status_markup,
    _category_markup,
)
from sessions import ClaudeSession


# ─── Helper Function Tests ──────────────────────────────────────────

class TestMarkupHelpers:
    def test_status_markup_contains_icon(self):
        result = _status_markup(Status.IN_PROGRESS)
        assert "\u25cf" in result  # ● icon

    def test_status_markup_contains_value(self):
        result = _status_markup(Status.BLOCKED)
        assert "blocked" in result

    def test_category_markup_contains_value(self):
        result = _category_markup(Category.WORK)
        assert "work" in result


class TestWsIndicators:
    def test_no_indicators(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        result = _ws_indicators(ws)
        assert result == ""

    def test_stale_indicator(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        from datetime import datetime, timedelta
        ws.updated_at = (datetime.now() - timedelta(hours=48)).isoformat()
        result = _ws_indicators(ws)
        assert "\u23f0" in result  # ⏰

    def test_done_not_stale(self):
        """Done workstreams don't show stale indicator even if old."""
        ws = Workstream(name="test", status=Status.DONE)
        from datetime import datetime, timedelta
        ws.updated_at = (datetime.now() - timedelta(hours=48)).isoformat()
        result = _ws_indicators(ws)
        assert "\u23f0" not in result

    def test_link_indicators(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("worktree", "~/work/project", "project")
        ws.add_link("ticket", "UB-1234", "ticket")
        result = _ws_indicators(ws)
        assert "\U0001f333" in result  # 🌳
        assert "\U0001f3ab" in result  # 🎫

    def test_tmux_indicator(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        result = _ws_indicators(ws, tmux_check=lambda _: True)
        assert "\u26a1" in result  # ⚡


class TestShortProject:
    def test_simple_path(self):
        assert _short_project("/home/user/dev/my-project") == "my-project"

    def test_home_path(self):
        result = _short_project(str(Path.home() / "dev" / "project"))
        assert result == "project"


class TestSessionAutoDiscovery:
    def _make_session(self, session_id="abc123", project_path="/home/kyle/dev/project", **kwargs):
        return ClaudeSession(
            session_id=session_id, project_dir="d", project_path=project_path,
            message_count=10, **kwargs,
        )

    def test_match_by_directory(self, tmp_path):
        # Create a real directory for the link
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("worktree", str(project_dir), "project")

        session = self._make_session(project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1
        assert found[0].session_id == "abc123"

    def test_match_by_file_link_directory(self, tmp_path):
        """file links pointing to directories should also match."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("file", str(project_dir), "source")

        session = self._make_session(project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_match_explicit_session_link(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("claude-session", "abc123", "session")

        session = self._make_session(session_id="abc123")
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_no_duplicates(self, tmp_path):
        """If a session matches both by link and directory, it should appear once."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("claude-session", "abc123", "session")
        ws.add_link("worktree", str(project_dir), "project")

        session = self._make_session(session_id="abc123", project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_no_match(self):
        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        session = self._make_session(project_path="/some/other/path")
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 0

    def test_sorted_by_recent(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test", status=Status.IN_PROGRESS)
        ws.add_link("worktree", str(project_dir), "project")

        s1 = self._make_session(session_id="old", project_path=str(project_dir),
                                last_activity="2026-03-20T08:00:00Z")
        s2 = self._make_session(session_id="new", project_path=str(project_dir),
                                last_activity="2026-03-20T10:00:00Z")
        found = _find_sessions_for_ws(ws, [s1, s2])
        assert found[0].session_id == "new"

    def test_ws_directories(self, tmp_path):
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()

        ws = Workstream(name="test")
        ws.add_link("worktree", str(d1), "a")
        ws.add_link("file", str(d2), "b")
        ws.add_link("url", "https://example.com", "c")  # not a directory

        dirs = _ws_directories(ws)
        assert str(d1) in dirs
        assert str(d2) in dirs
        assert len(dirs) == 2


class TestShortModel:
    def test_opus(self):
        assert _short_model("claude-opus-4-6") == "opus"

    def test_sonnet(self):
        assert _short_model("claude-sonnet-4-6") == "sonnet"

    def test_haiku(self):
        assert _short_model("claude-haiku-4-5-20251001") == "haiku"

    def test_unknown(self):
        result = _short_model("some-other-model-name")
        assert len(result) <= 12

    def test_empty(self):
        assert _short_model("") == "\u2014"


class TestLaunchOrchClaude:
    """Test that _launch_orch_claude builds the correct command."""

    def test_builds_resume_command(self, tmp_path):
        """Verify the wrapper is called with correct args for resume."""
        ws = Workstream(name="Test thread", description="A test", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.add_link("worktree", str(tmp_path), "project")

        import unittest.mock as mock
        with mock.patch("app.subprocess.Popen") as mock_popen:
            _launch_orch_claude(ws, session_id="abc-123", cwd=str(tmp_path))
            assert mock_popen.called
            cmd = mock_popen.call_args[0][0]
            assert any("orch-claude" in str(c) for c in cmd)
            assert "--ws-id" in cmd
            assert "--resume" in cmd
            assert "abc-123" in cmd

    def test_builds_spawn_command(self, tmp_path):
        """Verify the wrapper is called with correct args for new session."""
        ws = Workstream(name="Test", description="", category=Category.PERSONAL, status=Status.QUEUED)

        import unittest.mock as mock
        with mock.patch("app.subprocess.Popen") as mock_popen:
            _launch_orch_claude(ws, prompt="Help me with this")
            assert mock_popen.called
            cmd = mock_popen.call_args[0][0]
            assert "--prompt" in cmd
            assert "Help me with this" in cmd
            assert "--resume" not in cmd

    def test_includes_notes_truncated(self):
        ws = Workstream(name="Test", status=Status.IN_PROGRESS)
        ws.notes = "x" * 1000

        import unittest.mock as mock
        with mock.patch("app.subprocess.Popen") as mock_popen:
            _launch_orch_claude(ws)
            cmd = mock_popen.call_args[0][0]
            idx = cmd.index("--ws-notes")
            notes_val = cmd[idx + 1]
            assert len(notes_val) <= 500

    def test_no_notes_when_empty(self):
        ws = Workstream(name="Test", status=Status.IN_PROGRESS)

        import unittest.mock as mock
        with mock.patch("app.subprocess.Popen") as mock_popen:
            _launch_orch_claude(ws)
            cmd = mock_popen.call_args[0][0]
            assert "--ws-notes" not in cmd


# ─── App Smoke Tests (async) ────────────────────────────────────────

@pytest.fixture
def app_with_store(tmp_path):
    """Create an OrchestratorApp with a temp store.

    Patches thread/session discovery so tests don't load real Claude data.
    """
    store_path = tmp_path / "test_data.json"

    # Pre-populate with test data
    store = Store(path=store_path)
    ws1 = Workstream(name="Alpha", category=Category.WORK, status=Status.IN_PROGRESS)
    ws2 = Workstream(name="Beta", category=Category.PERSONAL, status=Status.QUEUED)
    ws3 = Workstream(name="Gamma", category=Category.META, status=Status.DONE)
    for ws in [ws1, ws2, ws3]:
        store.add(ws)

    with patch("app.discover_threads", return_value=[]), \
         patch("app.get_discovered_workstreams", return_value=[]), \
         patch("app.name_uncached_threads", return_value=0), \
         patch("app.synthesize_workstreams", return_value=0):
        app = OrchestratorApp()
        app.store = Store(path=store_path)
        yield app


@pytest.mark.asyncio
class TestAppStartup:
    async def test_app_runs(self, app_with_store):
        """App should start and display without crashing."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # App should be running
            assert pilot.app.is_running

    async def test_initial_view_is_workstreams(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            assert pilot.app.view_mode == ViewMode.WORKSTREAMS

    async def test_tables_exist(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_table = pilot.app.query_one("#ws-table")
            sessions_table = pilot.app.query_one("#sessions-table")
            archived_table = pilot.app.query_one("#archived-table")
            assert ws_table is not None
            assert sessions_table is not None
            assert archived_table is not None

    async def test_ws_table_has_rows(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_table = pilot.app.query_one("#ws-table")
            assert ws_table.row_count == 3


@pytest.mark.asyncio
class TestNavigation:
    async def test_j_moves_down(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            initial_row = table.cursor_coordinate.row
            await pilot.press("j")
            assert table.cursor_coordinate.row == initial_row + 1

    async def test_k_moves_up(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")  # move down first
            await pilot.press("j")
            await pilot.press("k")  # then up
            assert table.cursor_coordinate.row == 1

    async def test_g_goes_to_top(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")
            await pilot.press("j")
            await pilot.press("g")
            assert table.cursor_coordinate.row == 0

    async def test_G_goes_to_bottom(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("G")
            assert table.cursor_coordinate.row == table.row_count - 1

    async def test_ctrl_n_moves_down(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            initial = table.cursor_coordinate.row
            await pilot.press("ctrl+n")
            assert table.cursor_coordinate.row == initial + 1

    async def test_ctrl_p_moves_up(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")
            await pilot.press("ctrl+p")
            assert table.cursor_coordinate.row == 0


@pytest.mark.asyncio
class TestViewSwitching:
    async def test_tab_cycles_to_sessions(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("tab")
            assert pilot.app.view_mode == ViewMode.SESSIONS

    async def test_tab_cycles_to_archived(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("tab")
            await pilot.press("tab")
            assert pilot.app.view_mode == ViewMode.ARCHIVED

    async def test_tab_cycles_back_to_workstreams(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("tab")
            await pilot.press("tab")
            await pilot.press("tab")
            assert pilot.app.view_mode == ViewMode.WORKSTREAMS

    async def test_sessions_table_visible_after_tab(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("tab")
            sessions_table = pilot.app.query_one("#sessions-table")
            ws_table = pilot.app.query_one("#ws-table")
            assert sessions_table.display is True
            assert ws_table.display is False


@pytest.mark.asyncio
class TestFilters:
    async def test_filter_all(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("1")
            assert pilot.app.filter_mode == "all"

    async def test_filter_work(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            assert pilot.app.filter_mode == "work"
            table = pilot.app.query_one("#ws-table")
            assert table.row_count == 1  # Only Alpha is work

    async def test_filter_personal(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            assert pilot.app.filter_mode == "personal"
            table = pilot.app.query_one("#ws-table")
            assert table.row_count == 1  # Only Beta is personal


@pytest.mark.asyncio
class TestPreviewPane:
    async def test_preview_pane_exists(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            pane = pilot.app.query_one("#preview-pane")
            assert pane is not None

    async def test_preview_toggle(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            pane = pilot.app.query_one("#preview-pane")
            assert pane.display is True
            await pilot.press("p")
            assert pane.display is False
            await pilot.press("p")
            assert pane.display is True


@pytest.mark.asyncio
class TestStatusCycling:
    async def test_s_cycles_status(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws = pilot.app._selected_ws()
            old_status = ws.status
            await pilot.press("s")
            ws = pilot.app._selected_ws()
            assert ws.status != old_status


@pytest.mark.asyncio
class TestQuickNote:
    async def test_n_opens_note_modal(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("n")
            from app import QuickNoteScreen
            assert isinstance(pilot.app.screen, QuickNoteScreen)

    async def test_note_modal_escape_cancels(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("n")
            await pilot.press("escape")
            from app import QuickNoteScreen
            assert not isinstance(pilot.app.screen, QuickNoteScreen)

    async def test_note_adds_to_workstream(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_before = pilot.app._selected_ws()
            assert ws_before.notes == ""
            await pilot.press("n")
            # Type a note
            for char in "test note":
                await pilot.press(char)
            await pilot.press("enter")
            ws_after = pilot.app.store.get(ws_before.id)
            assert "test note" in ws_after.notes


@pytest.mark.asyncio
class TestRename:
    async def test_E_opens_rename_input(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("E")
            rename_input = pilot.app.query_one("#rename-input")
            assert rename_input.display is True

    async def test_rename_prefills_current_name(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws = pilot.app._selected_ws()
            await pilot.press("E")
            rename_input = pilot.app.query_one("#rename-input")
            assert rename_input.value == ws.name


@pytest.mark.asyncio
class TestFindWsForSession:
    async def test_finds_by_directory(self, app_with_store, tmp_path):
        """_find_ws_for_session matches by directory link."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # Add a worktree link to a workstream
            ws = pilot.app._selected_ws()
            project_dir = tmp_path / "project"
            project_dir.mkdir()
            ws.add_link("worktree", str(project_dir), "project")
            pilot.app.store.update(ws)

            session = ClaudeSession(
                session_id="test123", project_dir="d",
                project_path=str(project_dir), message_count=5,
            )
            found = pilot.app._find_ws_for_session(session)
            assert found is not None
            assert found.id == ws.id

    async def test_returns_none_for_unlinked(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            session = ClaudeSession(
                session_id="test123", project_dir="d",
                project_path="/some/random/path", message_count=5,
            )
            found = pilot.app._find_ws_for_session(session)
            assert found is None


@pytest.mark.asyncio
class TestHelpScreen:
    async def test_help_opens(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            assert pilot.app.screen.__class__.__name__ == "HelpScreen"

    async def test_help_closes_with_q(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            await pilot.press("q")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_help_mentions_ctrl_d(self, app_with_store):
        """Help screen should mention Ctrl+D for exiting Claude sessions."""
        # Verify the help text constant contains Ctrl+D
        from app import HelpScreen
        screen = HelpScreen()
        # The compose method creates a Static with help text that includes Ctrl+D
        # We test this by checking the HelpScreen renders without error
        # and verify the source text in app.py contains "Ctrl+D"
        import app as app_module
        import inspect
        source = inspect.getsource(app_module.HelpScreen)
        assert "Ctrl+D" in source


@pytest.mark.asyncio
class TestUILanguage:
    async def test_view_bar_says_workstreams(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # Check the render method output contains "Threads"
            rendered = pilot.app._render_view_bar()
            assert "Workstreams" in rendered

    async def test_summary_bar_says_threads(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            rendered = pilot.app._render_summary_bar()
            assert "threads" in rendered


class TestDoResume:
    """Tests for _do_resume branching: 1 session → immediate, 2+ → picker."""

    def _make_session(self, sid, project_path="/tmp/test", age="1m ago", msgs=5):
        return ClaudeSession(
            session_id=sid, project_dir="d",
            project_path=project_path, message_count=msgs,
        )

    @patch("app._has_tmux", return_value=True)
    @patch("app._launch_orch_claude", return_value=(True, ""))
    @patch("app._find_sessions_for_ws")
    def test_single_session_resumes_immediately(self, mock_find, mock_launch, mock_tmux):
        """With exactly 1 matching session, resume without showing picker."""
        session = self._make_session("s1", project_path="/tmp/test")
        mock_find.return_value = [session]
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, [session])

        mock_launch.assert_called_once()
        app.push_screen.assert_not_called()

    @patch("app._has_tmux", return_value=True)
    @patch("app._find_sessions_for_ws")
    def test_multiple_sessions_shows_picker(self, mock_find, mock_tmux):
        """With 2+ matching sessions, show ThreadPickerScreen."""
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        mock_find.return_value = sessions
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, sessions)

        app.push_screen.assert_called_once()
        screen_arg = app.push_screen.call_args[0][0]
        assert isinstance(screen_arg, ThreadPickerScreen)
        assert len(screen_arg.thread_sessions) == 3

    @patch("app._has_tmux", return_value=False)
    def test_no_tmux_notifies_error(self, mock_tmux):
        """Without tmux, show error notification."""
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, [])

        app.notify.assert_called_once()
        assert "tmux" in app.notify.call_args[0][0].lower()
