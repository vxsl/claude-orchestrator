"""Tests for actions.py — tmux, session launch, directory helpers."""

import pytest
from unittest.mock import patch, MagicMock

import actions
from models import Category, Workstream
from sessions import ClaudeSession
from actions import (
    ws_directories, ws_working_dir,
    find_sessions_for_ws, launch_orch_claude,
    has_tmux, do_resume, switch_to_tmux_window,
    get_git_remote_host,
)
from screens import SessionPickerScreen


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

    def test_match_by_subdirectory(self, tmp_path):
        """Session started from a subdirectory matches parent worktree link."""
        d = tmp_path / "repo"
        sub = d / "client" / "web"
        sub.mkdir(parents=True)
        ws = Workstream(name="test")
        ws.add_link("worktree", str(d), "repo")
        sessions = [self._make_session("s1", project_path=str(sub))]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1

    def test_match_by_repo_path(self, tmp_path):
        """Session matches via ws.repo_path (not just links)."""
        d = tmp_path / "repo"
        d.mkdir()
        ws = Workstream(name="test", repo_path=str(d))
        sessions = [self._make_session("s1", project_path=str(d))]
        found = find_sessions_for_ws(ws, sessions)
        assert len(found) == 1

    def test_match_subdirectory_via_repo_path(self, tmp_path):
        """Session from subdirectory matches via ws.repo_path."""
        d = tmp_path / "repo"
        sub = d / "client" / "web"
        sub.mkdir(parents=True)
        ws = Workstream(name="test", repo_path=str(d))
        sessions = [self._make_session("s1", project_path=str(sub))]
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

    def test_single_session_resumes(self):
        session = self._make_session("s1")
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, [session],
                  sessions_for_ws_fn=lambda w: [session])
        app.launch_claude_session.assert_called_once()

    def test_multiple_sessions_shows_picker(self):
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        ws = Workstream(name="test")
        app = MagicMock()
        pick_session = MagicMock()
        do_resume(ws, app, sessions,
                  sessions_for_ws_fn=lambda w: sessions,
                  pick_session=pick_session)
        pick_session.assert_called_once()
        picked_ws, picked_matching, on_pick = pick_session.call_args[0]
        assert picked_ws is ws
        assert picked_matching == sessions
        app.launch_claude_session.assert_not_called()
        # completing the pick resumes the chosen session
        on_pick(sessions[1])
        app.launch_claude_session.assert_called_once()

    def test_multiple_sessions_without_picker_resumes_most_recent(self):
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, sessions,
                  sessions_for_ws_fn=lambda w: sessions)
        app.launch_claude_session.assert_called_once()
        assert app.launch_claude_session.call_args.kwargs["session_id"] == "s0"

    def test_no_sessions_no_dirs_notifies(self):
        ws = Workstream(name="test")
        app = MagicMock()
        do_resume(ws, app, [],
                  sessions_for_ws_fn=lambda w: [])
        app.notify.assert_called_once()
        assert "no sessions" in app.notify.call_args[0][0].lower()


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


# ─── Git Status ──────────────────────────────────────────────────────

from actions import get_worktree_git_status, WorktreeStatus


class TestWorktreeGitStatus:
    def test_nonexistent_dir(self):
        status = get_worktree_git_status("/nonexistent/path")
        assert status.error == "not a directory"
        assert status.branch == ""

    def test_real_git_repo(self, tmp_path):
        """Test against a real temporary git repo."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        # Create initial commit
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        status = get_worktree_git_status(str(repo))
        assert status.error == ""
        assert status.branch in ("main", "master")
        assert not status.is_dirty

    def test_dirty_repo(self, tmp_path):
        """Test dirty detection."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Dirty it
        (repo / "file.txt").write_text("changed")

        status = get_worktree_git_status(str(repo))
        assert status.is_dirty
        assert status.has_unstaged

    def test_staged_changes(self, tmp_path):
        """Test staged change detection."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Stage a change
        (repo / "file.txt").write_text("staged")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)

        status = get_worktree_git_status(str(repo))
        assert status.has_staged

    def test_empty_string_path(self):
        status = get_worktree_git_status("")
        assert status.error == "not a directory"


# ─── Jira Cache ──────────────────────────────────────────────────────

from actions import get_jira_cache, get_jira_ticket_info, JiraTicketInfo


class TestJiraCache:
    def test_missing_cache_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("actions._JIRA_CACHE_PATH", tmp_path / "nonexistent.json")
        cache = get_jira_cache()
        assert cache == {}

    def test_valid_cache(self, tmp_path, monkeypatch):
        import json
        cache_path = tmp_path / "tickets.json"
        cache_path.write_text(json.dumps([
            {
                "key": "UB-1234",
                "fields": {
                    "summary": "Fix the bug",
                    "status": {"name": "In Progress"},
                    "assignee": {"displayName": "Kyle"}
                }
            },
            {
                "key": "UB-5678",
                "fields": {
                    "summary": "Add feature",
                    "status": {"name": "Done"},
                    "assignee": None
                }
            }
        ]))
        monkeypatch.setattr("actions._JIRA_CACHE_PATH", cache_path)
        cache = get_jira_cache()
        assert "UB-1234" in cache
        assert cache["UB-1234"].summary == "Fix the bug"
        assert cache["UB-1234"].status == "In Progress"
        assert cache["UB-1234"].assignee == "Kyle"
        assert "UB-5678" in cache
        assert cache["UB-5678"].assignee == ""

    def test_corrupt_cache(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "tickets.json"
        cache_path.write_text("not json")
        monkeypatch.setattr("actions._JIRA_CACHE_PATH", cache_path)
        cache = get_jira_cache()
        assert cache == {}

    def test_get_ticket_info(self, tmp_path, monkeypatch):
        import json
        cache_path = tmp_path / "tickets.json"
        cache_path.write_text(json.dumps([
            {"key": "UB-1", "fields": {"summary": "Test", "status": {"name": "Open"}, "assignee": None}}
        ]))
        monkeypatch.setattr("actions._JIRA_CACHE_PATH", cache_path)
        info = get_jira_ticket_info("UB-1")
        assert info is not None
        assert info.summary == "Test"
        assert get_jira_ticket_info("UB-999") is None


# ─── Dev-Workflow Integration ────────────────────────────────────────

from actions import (
    get_worktree_list, get_recent_branches, run_git_action,
    dev_tools_available, run_dev_tool,
)


class TestWorktreeList:
    def test_real_repo(self, tmp_path):
        """Test worktree listing on a real git repo."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        worktrees = get_worktree_list(str(repo))
        assert len(worktrees) >= 1
        assert worktrees[0]["path"] == str(repo)
        assert "branch" in worktrees[0]

    def test_nonexistent_dir(self):
        worktrees = get_worktree_list("/nonexistent/repo")
        assert worktrees == []


class TestRecentBranches:
    def test_real_repo(self, tmp_path):
        """Test branch listing on a real git repo."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Create a branch and checkout
        subprocess.run(["git", "checkout", "-b", "feature-1"], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-"], cwd=repo, capture_output=True)

        branches = get_recent_branches(str(repo))
        # Should find at least the main branch from the checkout
        branch_names = [b["branch"] for b in branches]
        assert any(b in ("main", "master") for b in branch_names)

    def test_nonexistent_dir(self):
        branches = get_recent_branches("/nonexistent/repo")
        assert branches == []


class TestRunGitAction:
    def test_wip_commit(self, tmp_path):
        """Test WIP commit action."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        # Make a change
        (repo / "file.txt").write_text("changed")
        success, msg = run_git_action("wip", str(repo))
        assert success
        assert "WIP" in msg

    def test_unknown_action(self, tmp_path):
        success, msg = run_git_action("nonexistent", str(tmp_path))
        assert not success


class TestDevToolsAvailable:
    def test_check(self):
        # Just ensure it doesn't crash
        result = dev_tools_available()
        assert isinstance(result, bool)

    def test_run_dev_tool_nonexistent(self):
        cmd = run_dev_tool("nonexistent-tool-xyz")
        # If dev-tools dir doesn't exist, returns empty
        # If it exists but tool doesn't, also returns empty
        assert isinstance(cmd, list)


class TestGetGitRemoteHost:
    @patch("actions.subprocess.run")
    def test_ssh_url(self, mock_run):
        """SSH remote git@gitlab.com:org/repo.git → 'gitlab.com'."""
        mock_run.return_value = MagicMock(
            stdout="git@gitlab.com:org/repo.git\n", returncode=0,
        )
        assert get_git_remote_host("/some/path") == "gitlab.com"

    @patch("actions.subprocess.run")
    def test_https_url(self, mock_run):
        """HTTPS remote https://github.com/user/repo → 'github.com'."""
        mock_run.return_value = MagicMock(
            stdout="https://github.com/user/repo\n", returncode=0,
        )
        assert get_git_remote_host("/some/path") == "github.com"

    @patch("actions.subprocess.run")
    def test_failure_returns_none(self, mock_run):
        """Subprocess failure (no remote, not a git repo, etc.) → None."""
        mock_run.return_value = MagicMock(stdout="", returncode=128)
        assert get_git_remote_host("/nonexistent") is None

    @patch("actions.subprocess.run")
    def test_exception_returns_none(self, mock_run):
        """Subprocess exception → None."""
        mock_run.side_effect = OSError("no such command")
        assert get_git_remote_host("/some/path") is None

    @patch("actions.subprocess.run")
    def test_https_with_auth(self, mock_run):
        """HTTPS remote with user:pass@ → strips auth, returns hostname."""
        mock_run.return_value = MagicMock(
            stdout="https://token@github.com/user/repo\n", returncode=0,
        )
        assert get_git_remote_host("/some/path") == "github.com"


# ─── Ticket Key Extraction ───────────────────────────────────────────

from actions import extract_ticket_key


class TestExtractTicketKey:
    def test_standard_branch(self):
        assert extract_ticket_key("UB-1234-fix-thing") == "UB-1234"

    def test_bare_ticket(self):
        assert extract_ticket_key("UB-1234") == "UB-1234"

    def test_multi_letter_project(self):
        assert extract_ticket_key("PROJ-42-add-feature") == "PROJ-42"

    def test_no_ticket(self):
        assert extract_ticket_key("feature/some-feature") == ""

    def test_main_branch(self):
        assert extract_ticket_key("main") == ""

    def test_lowercase_no_match(self):
        assert extract_ticket_key("ub-1234-fix-thing") == ""


# ─── MR Cache ────────────────────────────────────────────────────────

from actions import get_mr_cache


class TestMrCache:
    def test_missing_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("actions._MR_CACHE_PATH", tmp_path / "nonexistent.json")
        assert get_mr_cache() == {}

    def test_dict_format(self, tmp_path, monkeypatch):
        import json
        cache_path = tmp_path / "mr_cache.json"
        cache_path.write_text(json.dumps({
            "UB-1234-fix": {"web_url": "https://gitlab.com/mr/1", "state": "opened"},
        }))
        monkeypatch.setattr("actions._MR_CACHE_PATH", cache_path)
        cache = get_mr_cache()
        assert "UB-1234-fix" in cache
        assert cache["UB-1234-fix"]["web_url"] == "https://gitlab.com/mr/1"

    def test_list_format(self, tmp_path, monkeypatch):
        import json
        cache_path = tmp_path / "mr_cache.json"
        cache_path.write_text(json.dumps([
            {"source_branch": "UB-5678-feat", "web_url": "https://gitlab.com/mr/2"},
        ]))
        monkeypatch.setattr("actions._MR_CACHE_PATH", cache_path)
        cache = get_mr_cache()
        assert "UB-5678-feat" in cache


# ─── Ticket-Solve Cache ─────────────────────────────────────────────

from actions import get_ticket_solve_status


class TestTicketSolveStatus:
    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("actions._TICKET_SOLVE_DIR", tmp_path)
        assert get_ticket_solve_status("UB-9999") is None

    def test_valid_file(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr("actions._TICKET_SOLVE_DIR", tmp_path)
        (tmp_path / "UB-1234.json").write_text(json.dumps({"status": "running", "progress": 0.5}))
        result = get_ticket_solve_status("UB-1234")
        assert result is not None
        assert result["status"] == "running"

    def test_corrupt_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("actions._TICKET_SOLVE_DIR", tmp_path)
        (tmp_path / "UB-1234.json").write_text("not json")
        assert get_ticket_solve_status("UB-1234") is None


# ─── Discover Worktrees ─────────────────────────────────────────────

from actions import discover_worktrees


class TestDiscoverWorktrees:
    @patch("actions.get_worktree_list")
    def test_basic_discovery(self, mock_wt):
        mock_wt.return_value = [
            {"path": "/home/user/dev/repo", "branch": "main"},
            {"path": "/home/user/dev/repo-UB-1234", "branch": "UB-1234-fix-thing"},
        ]
        results = discover_worktrees(["/home/user/dev/repo"])
        # main is skipped
        assert len(results) == 1
        assert results[0]["branch"] == "UB-1234-fix-thing"
        assert results[0]["ticket_key"] == "UB-1234"

    @patch("actions.get_worktree_list")
    def test_skips_bare(self, mock_wt):
        mock_wt.return_value = [
            {"path": "/home/user/dev/repo", "branch": "feature-x", "bare": True},
        ]
        results = discover_worktrees(["/home/user/dev/repo"])
        assert len(results) == 0

    @patch("actions.get_worktree_list")
    def test_skips_skip_branches(self, mock_wt):
        mock_wt.return_value = [
            {"path": "/p1", "branch": "main"},
            {"path": "/p2", "branch": "master"},
            {"path": "/p3", "branch": "develop"},
            {"path": "/p4", "branch": "feature-x"},
        ]
        results = discover_worktrees(["/repo"])
        assert len(results) == 1
        assert results[0]["branch"] == "feature-x"

    @patch("actions.get_worktree_list")
    def test_repo_already_listed_is_not_asked_again(self, mock_wt):
        """A repo path that showed up in an earlier listing shares that family.

        Every worktree of a repo reports the same `worktree list`, and orch
        tracks each of them as its own repo path — asking all of them spawns
        one identical git per worktree, every poll cycle.
        """
        mock_wt.return_value = [
            {"path": "/repo", "branch": "main"},
            {"path": "/repo.wt", "branch": "feature-a"},
        ]
        results = discover_worktrees(["/repo", "/repo.wt"])
        assert mock_wt.call_count == 1  # /repo.wt came back in /repo's listing
        assert [r["path"] for r in results] == ["/repo", "/repo.wt"]
        assert {r["repo_path"] for r in results} == {"/repo"}

    @patch("actions.get_worktree_list")
    def test_unrelated_repos_are_each_asked(self, mock_wt):
        mock_wt.side_effect = [
            [{"path": "/a", "branch": "feature-a"}],
            [{"path": "/b", "branch": "feature-b"}],
        ]
        results = discover_worktrees(["/a-repo", "/b-repo"])
        assert mock_wt.call_count == 2
        assert [r["path"] for r in results] == ["/a", "/b"]

    @patch("actions.get_worktree_list")
    def test_deduplicates_paths(self, mock_wt):
        mock_wt.return_value = [
            {"path": "/home/user/dev/wt1", "branch": "feature-a"},
        ]
        # Same repo listed twice
        results = discover_worktrees(["/repo", "/repo"])
        assert len(results) == 1


class TestXWindowMapping:
    """A tmux client on a pts says nothing about whether anyone can see it —
    the terminal emulator's WM_STATE does."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        actions._x_window_cache.clear()
        yield
        actions._x_window_cache.clear()

    def test_ppid_matches_the_real_parent(self):
        import os
        assert actions._ppid(os.getpid()) == os.getppid()

    def test_ppid_survives_spaces_in_comm(self, monkeypatch):
        # "tmux: server" has a space, so naive field-splitting picks the
        # wrong column — parse after the last ')' instead.
        class FakePath:
            def __init__(self, *_):
                pass

            def read_text(self):
                return "2352 (tmux: server) S 7 2352 2352 0 -1 4194560\n"

        monkeypatch.setattr(actions, "Path", FakePath)
        assert actions._ppid(2352) == 7

    def test_ppid_of_missing_process_is_none(self):
        assert actions._ppid(999999999) is None

    def test_wm_state_parses_xprop(self, monkeypatch):
        monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: MagicMock(
            stdout="WM_STATE(WM_STATE):\n\t\twindow state: Iconic\n\t\ticon window: 0x0\n"
        ))
        assert actions._wm_state(1) == "Iconic"

    def test_wm_state_of_unmanaged_window_is_none(self, monkeypatch):
        monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: MagicMock(
            stdout="WM_STATE:  not found.\n"
        ))
        assert actions._wm_state(1) is None

    def test_managed_windows_walks_up_to_the_emulator(self, monkeypatch):
        # The tmux client owns no window; its alacritty parent does.
        monkeypatch.setattr(actions.subprocess, "run", lambda cmd, **k: MagicMock(
            stdout="5\n" if cmd[-1] == "200" else ""
        ))
        monkeypatch.setattr(actions, "_ppid", lambda p: 200 if p == 100 else None)
        monkeypatch.setattr(actions, "_wm_state", lambda w: "Normal")
        assert actions._managed_windows(100) == [5]
        assert actions._x_window_cache[100] == [5]

    def test_unmanaged_children_are_skipped(self, monkeypatch):
        monkeypatch.setattr(actions.subprocess, "run", lambda cmd, **k: MagicMock(
            stdout="5\n6\n"
        ))
        monkeypatch.setattr(actions, "_wm_state", lambda w: "Normal" if w == 6 else None)
        assert actions._managed_windows(100) == [6]

    def test_unresolved_pid_is_not_cached(self, monkeypatch):
        # The window may still be created later, so don't memoise the miss.
        monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: MagicMock(stdout=""))
        monkeypatch.setattr(actions, "_ppid", lambda p: None)
        assert actions._managed_windows(100) == []
        assert 100 not in actions._x_window_cache

    def test_missing_xdotool_is_not_fatal(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("xdotool")

        monkeypatch.setattr(actions.subprocess, "run", boom)
        assert actions._managed_windows(100) == []

    def test_no_display_is_unresolvable(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        assert actions._x_pids_mapped([1]) is None

    def test_pid_without_a_window_is_unresolvable(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(actions, "_managed_windows", lambda p: [])
        assert actions._x_pids_mapped([1]) is None

    def test_iconified_terminal_is_hidden(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(actions, "_managed_windows", lambda p: [5])
        monkeypatch.setattr(actions, "_wm_state", lambda w: "Iconic")
        assert actions._x_pids_mapped(["4242"]) is False

    def test_mapped_terminal_is_visible(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(actions, "_managed_windows", lambda p: [5])
        monkeypatch.setattr(actions, "_wm_state", lambda w: "Normal")
        assert actions._x_pids_mapped(["4242"]) is True

    def test_any_mapped_client_counts_as_visible(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(actions, "_managed_windows", lambda p: [p])
        monkeypatch.setattr(actions, "_wm_state", lambda w: "Normal" if w == 2 else "Iconic")
        assert actions._x_pids_mapped([1, 2]) is True


class TestTmuxPaneVisible:
    def _tmux_replies(self, monkeypatch, *stdouts):
        replies = list(stdouts)

        def fake_run(cmd, **k):
            return MagicMock(stdout=replies.pop(0) if replies else "")

        monkeypatch.setattr(actions.subprocess, "run", fake_run)

    def test_detached_session_is_hidden(self, monkeypatch):
        self._tmux_replies(monkeypatch, "0,1,$0\n")
        assert actions._tmux_pane_visible() is False

    def test_inactive_window_is_hidden(self, monkeypatch):
        self._tmux_replies(monkeypatch, "1,0,$0\n")
        assert actions._tmux_pane_visible() is False

    def test_vt_clients_still_use_the_vt_path(self, monkeypatch):
        self._tmux_replies(monkeypatch, "1,1,$0\n", "/dev/tty2 999\n")
        monkeypatch.setattr(actions, "_vt_is_active", lambda n: n == "tty2")
        assert actions._tmux_pane_visible() is True

    def test_pts_client_defers_to_x(self, monkeypatch):
        self._tmux_replies(monkeypatch, "1,1,$0\n", "/dev/pts/15 4242\n")
        seen = {}

        def fake_mapped(pids):
            seen["pids"] = list(pids)
            return False

        monkeypatch.setattr(actions, "_x_pids_mapped", fake_mapped)
        assert actions._tmux_pane_visible() is False
        assert seen["pids"] == ["4242"]

    def test_pts_client_visible_when_window_mapped(self, monkeypatch):
        self._tmux_replies(monkeypatch, "1,1,$0\n", "/dev/pts/15 4242\n")
        monkeypatch.setattr(actions, "_x_pids_mapped", lambda pids: True)
        assert actions._tmux_pane_visible() is True

    def test_unresolvable_x_counts_as_visible(self, monkeypatch):
        self._tmux_replies(monkeypatch, "1,1,$0\n", "/dev/pts/15 4242\n")
        monkeypatch.setattr(actions, "_x_pids_mapped", lambda pids: None)
        assert actions._tmux_pane_visible() is True


class TestVisibilityWatcher:
    """The 3s poll is fine for noticing we're hidden; coming back needs an
    event, or the terminal sits frozen until the next tick."""

    def _watcher(self):
        import os

        r, w = os.pipe()

        class FakeProc:
            def __init__(self):
                self.stdout = os.fdopen(r, "rb")
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = FakeProc()
        return actions.VisibilityWatcher(proc), w, proc

    def test_normal_means_mapped(self):
        import os
        wat, w, _ = self._watcher()
        os.write(w, b"WM_STATE(WM_STATE):\n\t\twindow state: Normal\n\t\ticon window: 0x0\n")
        assert wat.drain() is True

    def test_iconic_means_hidden(self):
        import os
        wat, w, _ = self._watcher()
        os.write(w, b"WM_STATE(WM_STATE):\n\t\twindow state: Iconic\n\t\ticon window: 0x0\n")
        assert wat.drain() is False

    def test_nothing_pending_is_none(self):
        wat, _, _ = self._watcher()
        assert wat.drain() is None

    def test_partial_line_is_buffered_until_complete(self):
        import os
        wat, w, _ = self._watcher()
        os.write(w, b"WM_STATE(WM_STATE):\n\t\twindow st")
        assert wat.drain() is None
        os.write(w, b"ate: Normal\n")
        assert wat.drain() is True

    def test_burst_collapses_to_the_latest_state(self):
        # Intermediate states are already stale; repainting each is wasted work.
        import os
        wat, w, _ = self._watcher()
        os.write(w, b"\t\twindow state: Normal\n\t\twindow state: Iconic\n")
        assert wat.drain() is False

    def test_eof_yields_none_and_shows_up_as_dead(self):
        import os
        wat, w, proc = self._watcher()
        os.close(w)
        assert wat.drain() is None
        proc.terminated = True
        assert wat.alive() is False

    def test_close_terminates_the_spy(self):
        wat, _, proc = self._watcher()
        assert wat.alive() is True
        wat.close()
        assert proc.terminated is True


class TestVisibilityWatcherFactory:
    def test_no_display_yields_no_watcher(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        assert actions.visibility_watcher() is None

    def test_unresolvable_window_yields_no_watcher(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(actions, "_orch_windows", lambda: [])
        assert actions.visibility_watcher() is None

    def test_window_cache_is_dropped_before_resolving(self, monkeypatch):
        # A restarted terminal emulator has a new window id; the watcher is
        # only ever recreated after one died, so never trust the old entry.
        monkeypatch.delenv("DISPLAY", raising=False)
        actions._x_window_cache[123] = [456]
        actions.visibility_watcher()
        assert actions._x_window_cache == {}

    def test_outside_tmux_it_watches_our_own_window(self, monkeypatch):
        import os
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr(actions.os, "ttyname", lambda fd: "/dev/pts/9")
        seen = []
        monkeypatch.setattr(actions, "_managed_windows", lambda p: seen.append(p) or [7])
        assert actions._orch_windows() == [7]
        assert seen == [os.getpid()]

    def test_under_tmux_it_watches_the_client_windows(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
        monkeypatch.setattr(actions.os, "ttyname", lambda fd: "/dev/pts/9")
        replies = ["$3\n", "4242\n"]
        monkeypatch.setattr(actions.subprocess, "run",
                            lambda *a, **k: MagicMock(stdout=replies.pop(0)))
        monkeypatch.setattr(actions, "_managed_windows", lambda p: [p * 2])
        assert actions._orch_windows() == [8484]

    def test_without_a_tty_there_is_nothing_to_watch(self, monkeypatch):
        # Tests and pipes have no terminal window, and shelling out to tmux
        # from them pollutes suites that patch subprocess globally.
        monkeypatch.setenv("DISPLAY", ":0")

        def no_tty(fd):
            raise OSError("not a tty")

        monkeypatch.setattr(actions.os, "ttyname", no_tty)
        monkeypatch.setattr(actions, "_managed_windows",
                            lambda p: pytest.fail("should not resolve windows"))
        assert actions._orch_windows() == []
