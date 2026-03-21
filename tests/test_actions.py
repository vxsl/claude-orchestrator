"""Tests for actions.py — tmux, session launch, directory helpers."""

import pytest
from unittest.mock import patch, MagicMock

from models import Category, Status, Workstream
from sessions import ClaudeSession
from actions import (
    ws_directories, ws_working_dir,
    find_sessions_for_ws, launch_orch_claude,
    has_tmux, do_resume,
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
