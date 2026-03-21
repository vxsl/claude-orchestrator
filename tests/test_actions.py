"""Tests for actions.py — tmux, session launch, directory helpers."""

import pytest
from unittest.mock import patch, MagicMock

from models import Category, Status, Workstream
from sessions import ClaudeSession
from actions import (
    ws_directories, ws_working_dir,
    find_sessions_for_ws, launch_orch_claude,
    has_tmux, do_resume, switch_to_tmux_window,
)
from screens import ThreadPickerScreen


class TestWsDirectories:
    def test_returns_worktree_dirs(self, tmp_path):
        d = tmp_path / "project"
        d.mkdir()
        ws = Workstream(name="test")
        ws.add_link("worktree", str(d), "proj")
        dirs = ws_directories(ws)
        assert str(d) in dirs

    def test_returns_file_dirs(self, tmp_path):
        d = tmp_path / "source"
        d.mkdir()
        ws = Workstream(name="test")
        ws.add_link("file", str(d), "src")
        dirs = ws_directories(ws)
        assert str(d) in dirs

    def test_ignores_non_dirs(self):
        ws = Workstream(name="test")
        ws.add_link("url", "https://example.com", "link")
        ws.add_link("ticket", "UB-123", "ticket")
        dirs = ws_directories(ws)
        assert len(dirs) == 0

    def test_working_dir_falls_back(self):
        ws = Workstream(name="test")
        import os
        assert ws_working_dir(ws) == os.getcwd()


class TestFindSessionsForWs:
    def _make_session(self, sid, project_path="/tmp/test"):
        return ClaudeSession(
            session_id=sid, project_dir="d",
            project_path=project_path, message_count=5,
        )

    def test_match_by_session_link(self):
        ws = Workstream(name="test")
        ws.add_link("claude-session", "s1", "session")
        sessions = [self._make_session("s1"), self._make_session("s2")]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1
        assert found[0].session_id == "s1"

    def test_match_by_prefix(self):
        ws = Workstream(name="test")
        ws.add_link("claude-session", "abc", "session")
        sessions = [self._make_session("abcdef")]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1

    def test_match_by_directory(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        ws = Workstream(name="test")
        ws.add_link("worktree", str(d), "proj")
        sessions = [self._make_session("s1", project_path=str(d))]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1

    def test_no_duplicates(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        ws = Workstream(name="test")
        ws.add_link("claude-session", "s1", "session")
        ws.add_link("worktree", str(d), "proj")
        sessions = [self._make_session("s1", project_path=str(d))]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1

    def test_sorted_by_recent(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        ws = Workstream(name="test")
        ws.add_link("worktree", str(d), "proj")
        s1 = self._make_session("old", project_path=str(d))
        s1.last_activity = "2025-01-01T00:00:00Z"
        s2 = self._make_session("new", project_path=str(d))
        s2.last_activity = "2026-03-20T00:00:00Z"
        found = find_sessions_for_ws(ws, [s1, s2])
        assert found[0].session_id == "new"


class TestDoResume:
    def _make_session(self, sid):
        return ClaudeSession(
            session_id=sid, project_dir="d",
            project_path="/tmp/test", message_count=5,
        )

    @patch("actions.has_tmux", return_value=True)
    @patch("actions.launch_orch_claude", return_value=(True, ""))
    def test_single_session_resumes(self, mock_launch, mock_tmux):
        session = self._make_session("s1")
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, [session],
                  sessions_for_ws_fn=lambda w: [session])
        mock_launch.assert_called_once()
        app.push_screen.assert_not_called()

    @patch("actions.has_tmux", return_value=True)
    def test_multiple_sessions_shows_picker(self, mock_tmux):
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, sessions,
                  sessions_for_ws_fn=lambda w: sessions)
        app.push_screen.assert_called_once()
        screen_arg = app.push_screen.call_args[0][0]
        assert isinstance(screen_arg, ThreadPickerScreen)

    @patch("actions.has_tmux", return_value=False)
    def test_no_tmux_notifies(self, mock_tmux):
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, [])
        app.notify.assert_called_once()
        assert "tmux" in app.notify.call_args[0][0].lower()


class TestSwitchToTmuxWindow:
    """Regression: switch_to_tmux_window must link into the current session
    before selecting, otherwise select-window silently switches the window
    in a different session (e.g. orch-workers) while the user sees nothing."""

    @patch("actions.subprocess.run")
    def test_links_before_selecting(self, mock_run):
        """The window should be linked into the current session before select-window."""
        # display-message returns current session name
        # link-window succeeds
        # select-window succeeds
        mock_run.side_effect = [
            MagicMock(stdout="orch\n", returncode=0),   # display-message
            MagicMock(returncode=0, stderr=""),           # link-window
            MagicMock(returncode=0),                      # select-window
        ]
        assert switch_to_tmux_window("@232") is True

        calls = mock_run.call_args_list
        # First call: get current session
        assert "display-message" in calls[0][0][0]
        # Second call: link-window into current session
        link_args = calls[1][0][0]
        assert "link-window" in link_args
        assert "@232" in link_args
        assert "orch:" in link_args[-1]
        # Third call: select-window
        assert "select-window" in calls[2][0][0]

    @patch("actions.subprocess.run")
    def test_link_failure_still_tries_select(self, mock_run):
        """Even if link-window fails (already linked), select-window should proceed."""
        mock_run.side_effect = [
            MagicMock(stdout="orch\n", returncode=0),    # display-message
            MagicMock(returncode=1, stderr="already linked"),  # link-window fails
            MagicMock(returncode=0),                      # select-window succeeds
        ]
        assert switch_to_tmux_window("@232") is True

    @patch("actions.subprocess.run")
    def test_no_current_session_returns_false(self, mock_run):
        """If we can't determine the current tmux session, bail out."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert switch_to_tmux_window("@232") is False
