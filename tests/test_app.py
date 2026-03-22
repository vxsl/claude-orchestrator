"""Tests for app.py — TUI application using Textual's pilot testing."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from models import Category, Link, Status, Store, Workstream
from app import OrchestratorApp
from screens import SessionPickerScreen
from rendering import (
    _ws_indicators,
    _short_project,
    _short_model,
    _status_markup,
    _category_markup,
)
from actions import (
    ws_directories as _ws_directories,
    do_resume as _do_resume,
    find_sessions_for_ws as _find_sessions_for_ws,
    launch_orch_claude as _launch_orch_claude,
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


def _mock_tmux_run(cmd, **kwargs):
    """Mock subprocess.run for tmux commands used by launch_orch_claude."""
    import subprocess
    args = cmd if isinstance(cmd, list) else [cmd]
    if args[:2] == ["tmux", "has-session"]:
        # Worker session doesn't exist yet
        return subprocess.CompletedProcess(args, returncode=1)
    if args[:2] == ["tmux", "new-session"]:
        return subprocess.CompletedProcess(args, returncode=0)
    if args[:2] == ["tmux", "list-windows"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="@99\n", stderr="")
    if args[:2] == ["tmux", "new-window"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="@100\n", stderr="")
    if args[:2] == ["tmux", "kill-window"]:
        return subprocess.CompletedProcess(args, returncode=0)
    if args[:2] == ["tmux", "link-window"]:
        return subprocess.CompletedProcess(args, returncode=0)
    if args[:2] == ["tmux", "select-window"]:
        return subprocess.CompletedProcess(args, returncode=0)
    return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")


def _find_new_window_cmd(mock_run):
    """Extract the tmux new-window call args from a mock."""
    for call in mock_run.call_args_list:
        args = call[0][0]
        if args[:2] == ["tmux", "new-window"]:
            return args
    return None


class TestLaunchOrchClaude:
    """Test that _launch_orch_claude builds the correct command."""

    def test_builds_resume_command(self, tmp_path):
        """Verify the wrapper is called with correct args for resume."""
        ws = Workstream(name="Test thread", description="A test", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.add_link("worktree", str(tmp_path), "project")

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws, session_id="abc-123", cwd=str(tmp_path))
            cmd = _find_new_window_cmd(mock_run)
            assert cmd is not None
            assert any("orch-claude" in str(c) for c in cmd)
            assert "--ws-id" in cmd
            assert "--resume" in cmd
            assert "abc-123" in cmd

    def test_builds_spawn_command(self, tmp_path):
        """Verify the wrapper is called with correct args for new session."""
        ws = Workstream(name="Test", description="", category=Category.PERSONAL, status=Status.QUEUED)

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws, prompt="Help me with this")
            cmd = _find_new_window_cmd(mock_run)
            assert cmd is not None
            assert "--prompt" in cmd
            assert "Help me with this" in cmd
            assert "--resume" not in cmd

    def test_includes_notes_truncated(self):
        ws = Workstream(name="Test", status=Status.IN_PROGRESS)
        ws.notes = "x" * 1000

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws)
            cmd = _find_new_window_cmd(mock_run)
            idx = cmd.index("--ws-notes")
            notes_val = cmd[idx + 1]
            assert len(notes_val) <= 500

    def test_no_notes_when_empty(self):
        ws = Workstream(name="Test", status=Status.IN_PROGRESS)

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws)
            cmd = _find_new_window_cmd(mock_run)
            assert "--ws-notes" not in cmd

    def test_creates_window_in_worker_session(self):
        """Claude windows are created in orch-workers, then linked into orch."""
        ws = Workstream(name="Test", status=Status.IN_PROGRESS)

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            ok, err = _launch_orch_claude(ws)
            assert ok
            cmds = [c[0][0] for c in mock_run.call_args_list]
            # Should create/check worker session
            assert any(c[:3] == ["tmux", "has-session", "-t"] and "orch-workers" in c for c in cmds)
            # new-window targets orch-workers
            nw = _find_new_window_cmd(mock_run)
            assert "orch-workers" in nw
            # link-window is called to make it visible in orch
            assert any(c[:2] == ["tmux", "link-window"] for c in cmds)


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
        app.state.store = Store(path=store_path)
        yield app


@pytest.mark.asyncio
class TestAppStartup:
    async def test_app_runs(self, app_with_store):
        """App should start and display without crashing."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # App should be running
            assert pilot.app.is_running

    async def test_ws_table_exists(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_table = pilot.app.query_one("#ws-table")
            assert ws_table is not None

    async def test_ws_table_has_rows(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_table = pilot.app.query_one("#ws-table")
            assert ws_table.option_count == 3


@pytest.mark.asyncio
class TestNavigation:
    async def test_j_moves_down(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            initial_row = table.highlighted
            await pilot.press("j")
            assert table.highlighted == initial_row + 1

    async def test_k_moves_up(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")  # move down first
            await pilot.press("j")
            await pilot.press("k")  # then up
            assert table.highlighted == 1

    async def test_g_goes_to_top(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")
            await pilot.press("j")
            await pilot.press("g")
            assert table.highlighted == 0

    async def test_G_goes_to_bottom(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("G")
            assert table.highlighted == table.option_count - 1

    async def test_ctrl_n_moves_down(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            initial = table.highlighted
            await pilot.press("ctrl+n")
            assert table.highlighted == initial + 1

    async def test_ctrl_p_moves_up(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            table = pilot.app.query_one("#ws-table")
            await pilot.press("j")
            await pilot.press("ctrl+p")
            assert table.highlighted == 0


@pytest.mark.asyncio
class TestTabSwitching:
    """Tab cycles through workstream tabs."""

    async def test_tab_stays_on_home_when_no_other_tabs(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("tab")
            assert pilot.app.tabs.is_home
            assert pilot.app.tabs.active_idx == 0

    async def test_tab_bar_exists(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            from widgets import TabBar
            tab_bar = pilot.app.query_one("#tab-bar", TabBar)
            assert tab_bar is not None

    async def test_archived_filter_shows_archived(self, app_with_store):
        """Pressing 6 activates archived filter instead of a separate view."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("6")
            assert pilot.app.filter_mode == "archived"


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
            assert table.option_count == 1  # Only Alpha is work

    async def test_filter_personal(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            assert pilot.app.filter_mode == "personal"
            table = pilot.app.query_one("#ws-table")
            assert table.option_count == 1  # Only Beta is personal


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
@pytest.mark.asyncio
class TestQuickNote:
    async def test_n_opens_note_modal(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("n")
            from screens import QuickNoteScreen
            assert isinstance(pilot.app.screen, QuickNoteScreen)

    async def test_note_modal_escape_cancels(self, app_with_store):
        """Escape dismisses text-input screens (backspace goes to Input widget)."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("n")
            await pilot.press("escape")
            from screens import QuickNoteScreen
            assert not isinstance(pilot.app.screen, QuickNoteScreen)

    async def test_note_adds_to_workstream(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            ws_before = pilot.app._selected_ws()
            assert len(ws_before.todos) == 0
            await pilot.press("n")
            # Type a note
            for char in "test note":
                await pilot.press(char)
            await pilot.press("enter")
            ws_after = pilot.app.store.get(ws_before.id)
            assert any(t.text == "test note" for t in ws_after.todos)


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

    async def test_help_closes_with_backspace(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            await pilot.press("backspace")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_help_closes_with_escape(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            await pilot.press("escape")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_help_mentions_ctrl_d(self, app_with_store):
        """Help screen should mention Ctrl+D for exiting Claude sessions."""
        # Verify the help text constant contains Ctrl+D
        from screens import HelpScreen
        screen = HelpScreen()
        # The compose method creates a Static with help text that includes Ctrl+D
        # We test this by checking the HelpScreen renders without error
        # and verify the source text in app.py contains "Ctrl+D"
        import screens as screens_module
        import inspect
        source = inspect.getsource(screens_module.HelpScreen)
        assert "Ctrl+D" in source


@pytest.mark.asyncio
class TestUILanguage:
    async def test_summary_bar_says_workstreams(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            rendered = pilot.app._render_summary_bar()
            assert "workstreams" in rendered


class TestDoResume:
    """Tests for _do_resume branching: 1 session → immediate, 2+ → picker."""

    def _make_session(self, sid, project_path="/tmp/test", age="1m ago", msgs=5):
        return ClaudeSession(
            session_id=sid, project_dir="d",
            project_path=project_path, message_count=msgs,
        )

    @patch("actions.find_sessions_for_ws")
    def test_single_session_resumes_immediately(self, mock_find):
        """With exactly 1 matching session, resume via launch_claude_session."""
        session = self._make_session("s1", project_path="/tmp/test")
        mock_find.return_value = [session]
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, [session])

        app.launch_claude_session.assert_called_once()
        app.push_screen.assert_not_called()

    @patch("actions.find_sessions_for_ws")
    def test_multiple_sessions_shows_picker(self, mock_find):
        """With 2+ matching sessions, show SessionPickerScreen."""
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        mock_find.return_value = sessions
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, sessions)

        app.push_screen.assert_called_once()
        screen_arg = app.push_screen.call_args[0][0]
        assert isinstance(screen_arg, SessionPickerScreen)

    def test_no_sessions_no_dirs_notifies(self):
        """With no sessions or directories, show notification."""
        ws = Workstream(name="test", category=Category.META)
        app = MagicMock()

        _do_resume(ws, app, [], sessions_for_ws_fn=lambda w: [])

        app.notify.assert_called_once()
        assert "no sessions" in app.notify.call_args[0][0].lower()


# ─── _parse_ts Regression Tests ─────────────────────────────────────

class TestParseTs:
    """Regression: comparing UTC-aware (Z suffix) and naive timestamps must not raise."""

    def test_utc_z_vs_naive_no_error(self):
        """The original bug: last_activity has 'Z', archived_at is naive."""
        from screens import DetailScreen
        from datetime import timezone

        aware = DetailScreen._parse_ts("2026-03-21T09:09:52.535Z")
        naive_input = DetailScreen._parse_ts("2026-03-21T10:16:25.734370")

        # Both should be comparable without TypeError
        assert naive_input > aware

    def test_both_aware(self):
        from screens import DetailScreen

        a = DetailScreen._parse_ts("2026-03-21T09:00:00Z")
        b = DetailScreen._parse_ts("2026-03-21T10:00:00+00:00")
        assert b > a

    def test_invalid_returns_min(self):
        from screens import DetailScreen
        from datetime import timezone

        result = DetailScreen._parse_ts("not-a-date")
        assert result.tzinfo is not None  # must be aware so comparisons work


@pytest.mark.asyncio
class TestHierarchyNavigation:
    async def test_ctrl_l_opens_detail_from_main(self, app_with_store):
        """Ctrl+L on main screen should open DetailScreen (drill in)."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            from screens import DetailScreen
            assert isinstance(pilot.app.screen, DetailScreen)

    async def test_backspace_dismisses_detail_screen(self, app_with_store):
        """Ctrl+H should dismiss DetailScreen back to main."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            from screens import DetailScreen
            assert isinstance(pilot.app.screen, DetailScreen)
            await pilot.press("backspace")
            assert not isinstance(pilot.app.screen, DetailScreen)

    async def test_backspace_dismisses_help_screen(self, app_with_store):
        """Ctrl+H should dismiss HelpScreen."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            assert pilot.app.screen.__class__.__name__ == "HelpScreen"
            await pilot.press("backspace")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_escape_dismisses_detail(self, app_with_store):
        """Escape should dismiss DetailScreen (bound to action_dismiss)."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            from screens import DetailScreen
            assert isinstance(pilot.app.screen, DetailScreen)
            await pilot.press("escape")
            assert not isinstance(pilot.app.screen, DetailScreen)

    async def test_q_does_not_dismiss_detail(self, app_with_store):
        """q should not dismiss DetailScreen (binding removed)."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            from screens import DetailScreen
            assert isinstance(pilot.app.screen, DetailScreen)
            await pilot.press("q")
            assert isinstance(pilot.app.screen, DetailScreen)

    async def test_escape_still_dismisses_picker(self, app_with_store):
        """Escape retained on pickers — HelpScreen should dismiss with Escape."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            assert pilot.app.screen.__class__.__name__ == "HelpScreen"
            await pilot.press("escape")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_backspace_at_root_does_nothing(self, app_with_store):
        """Ctrl+H at root screen should do nothing (no action_go_back)."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            screen_before = pilot.app.screen.__class__.__name__
            await pilot.press("backspace")
            assert pilot.app.screen.__class__.__name__ == screen_before

    async def test_backspace_after_search_dismisses_detail(self, app_with_store):
        """Regression: backspace must exit detail after search cancel, not get stuck."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            from screens import DetailScreen
            # Enter detail
            await pilot.press("ctrl+l")
            assert isinstance(pilot.app.screen, DetailScreen)
            # Open search, type, then backspace to empty and cancel
            await pilot.press("/")
            await pilot.press("a")
            await pilot.press("backspace")  # delete 'a'
            await pilot.press("backspace")  # empty → cancel search
            # Now backspace should dismiss detail
            await pilot.press("backspace")
            assert not isinstance(pilot.app.screen, DetailScreen)


@pytest.mark.asyncio
class TestCtrlHNavigation:
    """Ctrl+H (0x08) is distinct from backspace (0x7f) in Textual.

    In alacritty + tmux, Ctrl+H sends 0x08 which Textual maps to 'ctrl+h',
    not 'backspace'. Both must be bound for navigation to work.
    """

    async def test_ctrl_h_dismisses_detail_screen(self, app_with_store):
        """Ctrl+H key event should dismiss DetailScreen back to main."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            from screens import DetailScreen
            assert isinstance(pilot.app.screen, DetailScreen)
            await pilot.press("ctrl+h")
            assert not isinstance(pilot.app.screen, DetailScreen)

    async def test_ctrl_h_dismisses_help_screen(self, app_with_store):
        """Ctrl+H key event should dismiss HelpScreen."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("question_mark")
            assert pilot.app.screen.__class__.__name__ == "HelpScreen"
            await pilot.press("ctrl+h")
            assert pilot.app.screen.__class__.__name__ != "HelpScreen"

    async def test_ctrl_h_at_root_does_nothing(self, app_with_store):
        """Ctrl+H at root screen should not crash or change screen."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            screen_before = pilot.app.screen.__class__.__name__
            await pilot.press("ctrl+h")
            assert pilot.app.screen.__class__.__name__ == screen_before


class TestBackspaceBindingOnScreens:
    """Regression: backspace must be a screen-level BINDING, not just app.on_key().

    ModalScreens don't bubble key events to App in a real terminal,
    so every modal must declare its own backspace binding.
    """

    @pytest.mark.parametrize("screen_cls_name", [
        "HelpScreen", "QuickNoteScreen", "TodoScreen",
        "_TodoEditScreen", "_TodoContextScreen", "LinksScreen",
        "AddScreen", "DetailScreen", "BrainDumpScreen",
        "BrainPreviewScreen", "AddLinkScreen", "LinkSessionScreen",
        "SessionPickerScreen", "RepoPickerScreen",
        "WorkstreamPickerScreen", "ConfirmScreen",
    ])
    def test_screen_has_backspace_binding(self, screen_cls_name):
        """Every modal screen must have backspace in its BINDINGS."""
        import screens as screens_module
        cls = getattr(screens_module, screen_cls_name)
        binding_keys = []
        for b in cls.BINDINGS:
            if isinstance(b, tuple):
                binding_keys.append(b[0])
            else:
                binding_keys.append(b.key)
        assert any("backspace" in k for k in binding_keys), \
            f"{screen_cls_name} missing backspace in BINDINGS"

    @pytest.mark.parametrize("screen_cls_name", [
        "HelpScreen", "QuickNoteScreen", "TodoScreen",
        "_TodoEditScreen", "_TodoContextScreen", "LinksScreen",
        "AddScreen", "DetailScreen", "BrainDumpScreen",
        "BrainPreviewScreen", "AddLinkScreen", "LinkSessionScreen",
        "SessionPickerScreen", "RepoPickerScreen",
        "WorkstreamPickerScreen", "ConfirmScreen",
    ])
    def test_screen_has_ctrl_h_binding(self, screen_cls_name):
        """Every modal screen must also have ctrl+h (0x08) alongside backspace."""
        import screens as screens_module
        cls = getattr(screens_module, screen_cls_name)
        binding_keys = []
        for b in cls.BINDINGS:
            if isinstance(b, tuple):
                binding_keys.append(b[0])
            else:
                binding_keys.append(b.key)
        assert any("ctrl+h" in k for k in binding_keys), \
            f"{screen_cls_name} missing ctrl+h in BINDINGS"


class TestRichMarkupEscaping:
    """Regression: user-generated text with [ must not crash Rich markup rendering.

    rich.markup.escape() does NOT escape all brackets — only ones that look like
    valid tags. Arbitrary text like "[Binding(key='backspace')]" passes through
    unescaped, then crashes when embedded inside Rich color tags. All rendering
    helpers must use _rich_escape() (which escapes ALL brackets) on any
    user-generated text before embedding it in Rich markup.
    """

    BRACKET_TEXT = "[Binding(key='backspace', action='go_back')]"
    MARKUP_CHARS = "[bold]not a tag[/bold]"

    def _make_session(self, last_text="", title=""):
        return ClaudeSession(
            session_id="test-brackets",
            project_dir="d",
            project_path="/tmp/test",
            message_count=5,
            last_message_text=last_text,
            last_message_role="user",
            model="claude-sonnet-4-6",
            title=title,
        )

    def test_render_session_option_with_brackets_in_last_message(self):
        """Session with [ in last_message_text must not crash."""
        from rendering import _render_session_option
        from threads import ThreadActivity
        s = self._make_session(last_text=self.BRACKET_TEXT)
        # Must not raise MarkupError
        result = _render_session_option(s, ThreadActivity.IDLE)
        assert "backspace" in result

    def test_render_session_option_with_brackets_in_title(self):
        """Session with [ in title must not crash."""
        from rendering import _render_session_option, _session_title, _rich_escape
        from threads import ThreadActivity
        # _session_title uses a cache; test _rich_escape on the title directly
        title = _rich_escape(self.BRACKET_TEXT)
        assert r"\[" in title
        # Also verify the full render path doesn't crash
        s = self._make_session(title=self.BRACKET_TEXT)
        result = _render_session_option(s, ThreadActivity.IDLE)
        assert result  # didn't crash

    def test_render_session_option_with_markup_in_last_message(self):
        """Session with Rich markup tags in text must not be interpreted."""
        from rendering import _render_session_option
        from threads import ThreadActivity
        s = self._make_session(last_text=self.MARKUP_CHARS)
        result = _render_session_option(s, ThreadActivity.IDLE)
        # The [bold] should be escaped, not rendered as markup
        assert r"\[bold]" in result

    def test_render_notification_option_with_brackets(self):
        """Notification with [ in message must not crash."""
        from rendering import _render_notification_option
        from notifications import Notification
        notif = Notification(
            id="test", timestamp="2026-03-21T12:00:00Z",
            cwd="/tmp", title=self.BRACKET_TEXT,
            message=self.BRACKET_TEXT, session_id="x",
        )
        result = _render_notification_option(notif)
        assert "backspace" in result

    def test_render_todo_option_with_brackets(self):
        """Todo with [ in text must not crash."""
        from rendering import _render_todo_option
        from models import TodoItem
        todo = TodoItem(text=self.BRACKET_TEXT)
        result = _render_todo_option(todo, is_archived=False)
        assert "backspace" in result

    def test_rich_escape_escapes_all_brackets(self):
        """_rich_escape must escape ALL [ characters, not just tag-like ones."""
        from rendering import _rich_escape
        escaped = _rich_escape(self.BRACKET_TEXT)
        assert "[" not in escaped.replace(r"\[", "")

    def test_session_option_render_does_not_raise(self):
        """End-to-end: rendering a session option through Rich must not raise."""
        from rich.console import Console
        from rich.text import Text
        from rendering import _render_session_option
        from threads import ThreadActivity
        s = self._make_session(last_text=self.BRACKET_TEXT, title="[oops]")
        markup = _render_session_option(s, ThreadActivity.IDLE)
        console = Console()
        # This is the call that actually crashed — Rich parses the markup
        text = Text.from_markup(markup)
        assert text  # didn't raise


# ─── E2E: Command Palette (Step 5) ──────────────────────────────────


@pytest.mark.asyncio
class TestCommandPaletteE2E:
    """Command palette opens with : and dispatches commands correctly."""

    async def test_colon_opens_fuzzy_picker(self, app_with_store):
        """Pressing : should push a FuzzyPickerScreen onto the screen stack."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("colon")
            from widgets import FuzzyPickerScreen
            assert isinstance(pilot.app.screen, FuzzyPickerScreen)

    async def test_palette_has_items(self, app_with_store):
        """The command palette should show all commands from the registry."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("colon")
            from widgets import FuzzyPicker
            picker = pilot.app.screen.query_one("#fpscreen-picker", FuzzyPicker)
            from state import COMMAND_REGISTRY
            assert len(picker._all_items) >= len(COMMAND_REGISTRY)

    async def test_palette_escape_cancels(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("colon")
            from widgets import FuzzyPickerScreen
            assert isinstance(pilot.app.screen, FuzzyPickerScreen)
            await pilot.press("escape")
            assert not isinstance(pilot.app.screen, FuzzyPickerScreen)


# ─── E2E: Tab Bar (CHANGES.md: ctrl+tab, ctrl+shift+tab, x) ────────


@pytest.mark.asyncio
class TestTabBarE2E:
    """Tab bar appears, can be navigated with ctrl+tab, and tabs close with x."""

    async def test_tab_bar_renders(self, app_with_store):
        """Tab bar widget is present and rendering."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            from widgets import TabBar
            bar = pilot.app.query_one("#tab-bar", TabBar)
            assert bar is not None
            assert bar.tab_count >= 1  # At least "Home"

    async def test_open_detail_creates_tab(self, app_with_store):
        """Opening a workstream detail adds a tab."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # Enter opens detail / creates tab
            await pilot.press("enter")
            assert len(pilot.app.tabs.tabs) >= 2

    async def test_x_closes_tab(self, app_with_store):
        """x on the home screen should close a non-Home tab."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            # Open a detail tab then go back to home
            await pilot.press("enter")
            tab_count_after_open = len(pilot.app.tabs.tabs)
            assert tab_count_after_open >= 2
            await pilot.press("escape")  # back to home
            # Switch to the detail tab via tab manager, then close
            pilot.app.tabs.switch_to(1)
            pilot.app.action_close_tab()
            assert len(pilot.app.tabs.tabs) == tab_count_after_open - 1

    async def test_x_cannot_close_home(self, app_with_store):
        """x on Home tab should not close it."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            assert pilot.app.tabs.is_home
            await pilot.press("x")
            assert pilot.app.tabs.is_home
            assert len(pilot.app.tabs.tabs) == 1


# ─── E2E: Filter Keys 1-6 ──────────────────────────────────────────


@pytest.mark.asyncio
class TestFilterKeysE2E:
    """Filter keys 1-6 change the active filter, especially 6=archived."""

    async def test_filter_1_all(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("1")
            assert pilot.app.filter_mode == "all"

    async def test_filter_2_work(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            assert pilot.app.filter_mode == "work"

    async def test_filter_3_personal(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            assert pilot.app.filter_mode == "personal"

    async def test_filter_4_active(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            assert pilot.app.filter_mode == "active"

    async def test_filter_5_stale(self, app_with_store):
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("5")
            assert pilot.app.filter_mode == "stale"

    async def test_filter_6_archived(self, app_with_store):
        """Key 6 shows archived — this replaced the old ViewMode.ARCHIVED view."""
        async with app_with_store.run_test(size=(120, 40)) as pilot:
            await pilot.press("6")
            assert pilot.app.filter_mode == "archived"


# ─── E2E: Enrichment Rendering ──────────────────────────────────────


class TestEnrichmentRendering:
    """Enrichment badges (Jira/MR/ticket-solve) render on workstream rows."""

    def test_jira_status_renders(self):
        """Workstream with ticket_key and ticket_status shows Jira badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.ticket_key = "UB-1234"
        ws.ticket_status = "In Progress"
        result = _render_ws_option(ws, [], {})
        assert "UB-1234" in result
        assert "In Progress" in result

    def test_mr_badge_renders(self):
        """Workstream with mr_url shows MR badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.mr_url = "https://gitlab.com/mr/1"
        result = _render_ws_option(ws, [], {})
        assert "MR" in result

    def test_ticket_solve_badge_renders(self):
        """Workstream with ticket_solve_status shows solving badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.ticket_solve_status = "running"
        result = _render_ws_option(ws, [], {})
        assert "solving" in result

    def test_no_enrichment_renders_clean(self):
        """Workstream without enrichment data renders without badges."""
        from rendering import _render_ws_option
        ws = Workstream(name="Clean WS", category=Category.PERSONAL, status=Status.QUEUED)
        result = _render_ws_option(ws, [], {})
        assert "MR" not in result
        assert "solving" not in result
        # Should still have category and time
        assert "personal" in result

    def test_enrichment_badges_parse_as_rich_markup(self):
        """Rendered enrichment markup must be valid Rich markup (no crashes)."""
        from rich.text import Text
        from rendering import _render_ws_option
        ws = Workstream(name="Full WS", category=Category.WORK, status=Status.IN_PROGRESS)
        ws.ticket_key = "UB-9999"
        ws.ticket_status = "Done"
        ws.mr_url = "https://gitlab.com/mr/42"
        ws.ticket_solve_status = "complete"
        result = _render_ws_option(ws, [], {})
        # Should not raise
        text = Text.from_markup(result)
        assert text


# ─── E2E: Worktree Discovery Integration ────────────────────────────


class TestWorktreeDiscoveryIntegration:
    """Test worktree discovery against real git repos on this machine."""

    def test_discover_real_worktrees(self):
        """discover_worktrees finds worktrees in this very repo."""
        from actions import discover_worktrees
        repo = str(Path(__file__).parent.parent)
        results = discover_worktrees([repo])
        # This repo has .claude/worktrees/* and a .performance worktree
        branches = [wt["branch"] for wt in results]
        assert len(results) >= 1
        # Should not include 'master' or 'main'
        assert "master" not in branches
        assert "main" not in branches

    def test_known_repos_finds_real_repos(self):
        """known_repos() returns real directories that exist on disk."""
        from state import AppState
        from models import Store
        store = Store()
        st = AppState(store)
        repos = st.known_repos()
        for r in repos:
            assert Path(r).is_dir(), f"known_repos returned non-existent: {r}"

    def test_jira_cache_parses(self):
        """Jira cache file parses without error (if it exists)."""
        from actions import get_jira_cache, _JIRA_CACHE_PATH
        if not _JIRA_CACHE_PATH.exists():
            pytest.skip("No Jira cache on this machine")
        cache = get_jira_cache()
        assert len(cache) > 0
        for key, info in cache.items():
            assert key  # non-empty key
            assert hasattr(info, "summary")

    def test_mr_cache_parses(self):
        """MR cache file parses without error (if it exists)."""
        from actions import get_mr_cache, _MR_CACHE_PATH
        if not _MR_CACHE_PATH.exists():
            pytest.skip("No MR cache on this machine")
        cache = get_mr_cache()
        assert len(cache) > 0
        for key, info in cache.items():
            assert key
            # Should have a URL field (either 'url' or 'web_url')
            assert info.get("url") or info.get("web_url"), (
                f"MR entry {key} has no url: {info}"
            )
