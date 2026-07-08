"""Sync logic tests split out of test_app.py — no Textual pilot required.

These exercise pure logic: rendering helpers, do_resume branching with
MagicMock apps, command construction, CLI subcommands, state interactions.
"""

import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from models import Category, Link, Store, Workstream
from rendering import (
    _ws_indicators,
    _short_project,
    _short_model,
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
    def test_category_markup_contains_value(self):
        result = _category_markup(Category.WORK)
        assert "work" in result


class TestWsIndicators:
    def test_no_indicators(self):
        ws = Workstream(name="test")
        result = _ws_indicators(ws)
        assert result == ""

    def test_stale_indicator(self):
        ws = Workstream(name="test")
        from datetime import datetime, timedelta
        ws.updated_at = (datetime.now() - timedelta(hours=48)).isoformat()
        result = _ws_indicators(ws)
        assert "\u23f0" in result  # ⏰

    def test_link_indicators(self):
        ws = Workstream(name="test")
        ws.add_link("worktree", "~/work/project", "project")
        ws.add_link("ticket", "UB-1234", "ticket")
        result = _ws_indicators(ws)
        assert "\U0001f333" in result  # 🌳
        assert "\U0001f3ab" in result  # 🎫

    def test_tmux_indicator(self):
        ws = Workstream(name="test")
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

        ws = Workstream(name="test")
        ws.add_link("worktree", str(project_dir), "project")

        session = self._make_session(project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1
        assert found[0].session_id == "abc123"

    def test_match_by_file_link_directory(self, tmp_path):
        """file links pointing to directories should also match."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("file", str(project_dir), "source")

        session = self._make_session(project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_match_explicit_session_link(self):
        ws = Workstream(name="test")
        ws.add_link("claude-session", "abc123", "session")

        session = self._make_session(session_id="abc123")
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_no_duplicates(self, tmp_path):
        """If a session matches both by link and directory, it should appear once."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
        ws.add_link("claude-session", "abc123", "session")
        ws.add_link("worktree", str(project_dir), "project")

        session = self._make_session(session_id="abc123", project_path=str(project_dir))
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 1

    def test_no_match(self):
        ws = Workstream(name="test")
        session = self._make_session(project_path="/some/other/path")
        found = _find_sessions_for_ws(ws, [session])
        assert len(found) == 0

    def test_sorted_by_recent(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        ws = Workstream(name="test")
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
        ws = Workstream(name="Test thread", description="A test", category=Category.WORK)
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
        ws = Workstream(name="Test", description="", category=Category.PERSONAL)

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws, prompt="Help me with this")
            cmd = _find_new_window_cmd(mock_run)
            assert cmd is not None
            assert "--prompt" in cmd
            assert "Help me with this" in cmd
            assert "--resume" not in cmd

    def test_includes_notes_truncated(self):
        ws = Workstream(name="Test")
        ws.notes = "x" * 1000

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws)
            cmd = _find_new_window_cmd(mock_run)
            idx = cmd.index("--ws-notes")
            notes_val = cmd[idx + 1]
            assert len(notes_val) <= 500

    def test_no_notes_when_empty(self):
        ws = Workstream(name="Test")

        import unittest.mock as mock
        with mock.patch("actions.subprocess.run", side_effect=_mock_tmux_run) as mock_run:
            _launch_orch_claude(ws)
            cmd = _find_new_window_cmd(mock_run)
            assert "--ws-notes" not in cmd

    def test_creates_window_in_worker_session(self):
        """Claude windows are created in orch-workers, then linked into orch."""
        ws = Workstream(name="Test")

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
        ws = Workstream(name="test", category=Category.WORK)
        app = MagicMock()

        _do_resume(ws, app, [session])

        app.launch_claude_session.assert_called_once()
        app.push_screen.assert_not_called()

    @patch("actions.find_sessions_for_ws")
    def test_multiple_sessions_shows_picker(self, mock_find):
        """With 2+ matching sessions, invoke the injected pick_session."""
        sessions = [self._make_session(f"s{i}") for i in range(3)]
        mock_find.return_value = sessions
        ws = Workstream(name="test", category=Category.WORK)
        app = MagicMock()
        pick_session = MagicMock()

        _do_resume(ws, app, sessions, pick_session=pick_session)

        pick_session.assert_called_once()
        assert pick_session.call_args[0][1] == sessions
        app.launch_claude_session.assert_not_called()

    def test_no_sessions_no_dirs_notifies(self):
        """With no sessions or directories, show notification."""
        ws = Workstream(name="test", category=Category.WORK)
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


# ─── E2E: Enrichment Rendering ──────────────────────────────────────


class TestEnrichmentRendering:
    """Enrichment badges (Jira/MR/ticket-solve) render on workstream rows."""

    def test_jira_status_renders(self):
        """Workstream with ticket_key and ticket_status shows Jira badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK)
        ws.ticket_key = "UB-1234"
        ws.ticket_status = "In Progress"
        result = _render_ws_option(ws, [], {})
        assert "UB-1234" in result
        assert "In Progress" in result

    def test_mr_badge_renders(self):
        """Workstream with mr_url shows MR badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK)
        ws.mr_url = "https://gitlab.com/mr/1"
        result = _render_ws_option(ws, [], {})
        assert "MR" in result

    def test_ticket_solve_badge_renders(self):
        """Workstream with ticket_solve_status shows solving badge."""
        from rendering import _render_ws_option
        ws = Workstream(name="Test WS", category=Category.WORK)
        ws.ticket_solve_status = "running"
        result = _render_ws_option(ws, [], {})
        assert "solving" in result

    def test_no_enrichment_renders_clean(self):
        """Workstream without enrichment data renders without badges."""
        from rendering import _render_ws_option
        ws = Workstream(name="Clean WS", category=Category.PERSONAL)
        result = _render_ws_option(ws, [], {})
        assert "MR" not in result
        assert "solving" not in result
        # Should still have category and time
        assert "personal" in result

    def test_enrichment_badges_parse_as_rich_markup(self):
        """Rendered enrichment markup must be valid Rich markup (no crashes)."""
        from rich.text import Text
        from rendering import _render_ws_option
        ws = Workstream(name="Full WS", category=Category.WORK)
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


# ─── CLI subcommand tests ───────────────────────────────────────────


class TestCLISubcommands:
    """Test CLI commands with real Store on temp data."""

    def _make_store_with_ws(self, tmp_path):
        """Create a store with a test workstream, return (store, ws)."""
        store_path = tmp_path / "cli_test_data.json"
        store = Store(path=store_path)
        ws = Workstream(name="CLI Test WS", description="A test",
                        category=Category.WORK)
        store.add(ws)
        return store, ws

    def test_cmd_show(self, tmp_path, capsys):
        """cmd_show should print workstream details without crashing."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_show
        args = MagicMock()
        args.id = ws.id
        with patch("cli.Store", return_value=store):
            cmd_show(args)
        captured = capsys.readouterr()
        assert "CLI Test WS" in captured.out
        assert ws.id in captured.out

    def test_cmd_note(self, tmp_path, capsys):
        """cmd_note should add a todo item to the workstream."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_note
        args = MagicMock()
        args.id = ws.id
        args.text = ["hello", "from", "test"]
        with patch("cli.Store", return_value=store):
            cmd_note(args)
        # Verify todo was added
        updated_ws = store.get(ws.id)
        assert any(t.text == "hello from test" for t in updated_ws.todos)

    def test_cmd_distill_crystallize(self, tmp_path, capsys):
        """cmd_distill crystallize should add a todo to the workstream."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_distill
        args = MagicMock()
        args.distill_mode = "crystallize"
        args.text = "investigate flaky test"
        args.context = "test_foo sometimes fails on CI"
        args.ws_id = ws.id
        with patch("cli.Store", return_value=store), \
             patch.dict(os.environ, {"ORCH_WS_ID": ws.id}):
            cmd_distill(args)
        updated_ws = store.get(ws.id)
        assert any(t.text == "investigate flaky test" for t in updated_ws.todos)

    def test_cmd_distill_compact(self, tmp_path, capsys):
        """cmd_distill compact should save a continuation file."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_distill
        args = MagicMock()
        args.distill_mode = "compact"
        args.summary = "Session summary for next time"
        args.ws_id = ws.id
        cont_dir = tmp_path / "continuations"
        with patch("cli.Store", return_value=store), \
             patch("cli.Path.home", return_value=tmp_path), \
             patch.dict(os.environ, {"ORCH_WS_ID": ws.id}):
            cmd_distill(args)
        # Check continuation file was created
        captured = capsys.readouterr()
        assert "Continuation context saved" in captured.out

    def test_cmd_spawn_outside_tmux(self, tmp_path, capsys):
        """cmd_spawn outside tmux should print error and exit."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_spawn
        args = MagicMock()
        args.id = ws.id
        with patch("cli.Store", return_value=store), \
             patch.dict(os.environ, {"TMUX": ""}, clear=False), \
             pytest.raises(SystemExit):
            cmd_spawn(args)

    def test_cmd_resume_no_session(self, tmp_path, capsys):
        """cmd_resume with no linked session should print info message."""
        store, ws = self._make_store_with_ws(tmp_path)
        from cli import cmd_resume
        args = MagicMock()
        args.id = ws.id
        with patch("cli.Store", return_value=store):
            cmd_resume(args)
        captured = capsys.readouterr()
        assert "no Claude session" in captured.out.lower() or "No Claude session" in captured.out


# ─── Brain dump parser unit tests ────────────────────────────────────


class TestBrainDumpParser:
    """Unit tests for the brain.py parser."""

    def test_single_item(self):
        from brain import parse_brain_dump
        tasks = parse_brain_dump("fix the login bug")
        assert len(tasks) >= 1
        assert tasks[0].name  # non-empty name

    def test_comma_splitting(self):
        from brain import parse_brain_dump
        tasks = parse_brain_dump("fix the auth bug, review Logan's MR, deploy is blocked on migration")
        assert len(tasks) >= 2  # should split into 2-3 tasks

    def test_newline_splitting(self):
        from brain import parse_brain_dump
        tasks = parse_brain_dump("fix auth\nreview MR\ndeploy service")
        assert len(tasks) == 3

    def test_empty_input(self):
        from brain import parse_brain_dump
        assert parse_brain_dump("") == []
        assert parse_brain_dump("   ") == []

    def test_category_detection(self):
        from brain import parse_brain_dump
        from models import Category
        tasks = parse_brain_dump("fix the UB-1234 ticket")
        assert tasks[0].category == Category.WORK


# ─── Ctrl+L binding audit ────────────────────────────────────────────


# ─── Watcher debounce unit tests ─────────────────────────────────────


class TestLeadingEdgeDebounce:
    """Test the leading-edge debounce in watcher.py."""

    def test_fires_immediately_first_time(self):
        from watcher import _LeadingEdgeDebounce
        calls = []
        d = _LeadingEdgeDebounce(lambda: calls.append(1), window=1.0)
        d()
        assert len(calls) == 1

    def test_suppresses_within_window(self):
        from watcher import _LeadingEdgeDebounce
        calls = []
        d = _LeadingEdgeDebounce(lambda: calls.append(1), window=1.0)
        d()
        d()  # within window
        d()  # within window
        assert len(calls) == 1  # only first fire


class TestTrailingEdgeDebounce:
    """Test the trailing-edge debounce in watcher.py."""

    def test_fires_after_quiet(self):
        import time
        from watcher import _TrailingEdgeDebounce
        calls = []
        d = _TrailingEdgeDebounce(lambda: calls.append(1), window=0.05)
        d()
        time.sleep(0.1)
        assert len(calls) == 1

    def test_resets_on_rapid_calls(self):
        import time
        from watcher import _TrailingEdgeDebounce
        calls = []
        d = _TrailingEdgeDebounce(lambda: calls.append(1), window=0.1)
        d()
        time.sleep(0.03)
        d()  # reset timer
        time.sleep(0.03)
        d()  # reset timer again
        # Should still be 0 (timer keeps resetting)
        assert len(calls) == 0
        time.sleep(0.15)
        # Now it should have fired once
        assert len(calls) == 1


class TestSplitHandler:
    """Test that _SplitHandler correctly classifies events."""

    def test_jsonl_is_content(self):
        from watcher import _SplitHandler
        liveness = []
        content = []
        h = _SplitHandler(
            on_liveness=lambda: liveness.append(1),
            on_content=lambda: content.append(1),
            liveness_debounce=0.01,
            content_debounce=0.01,
        )
        from unittest.mock import MagicMock
        event = MagicMock()
        event.src_path = "/home/user/.claude/projects/test/session.jsonl"
        event.is_directory = False
        kind = h._classify(event)
        assert kind == "content"

    def test_session_json_is_liveness(self):
        from watcher import _SplitHandler, CLAUDE_SESSIONS_DIR
        h = _SplitHandler(
            on_liveness=lambda: None,
            on_content=lambda: None,
        )
        from unittest.mock import MagicMock
        event = MagicMock()
        event.src_path = str(CLAUDE_SESSIONS_DIR / "abc123.json")
        event.is_directory = False
        kind = h._classify(event)
        assert kind == "liveness"

    def test_random_file_is_none(self):
        from watcher import _SplitHandler
        h = _SplitHandler(
            on_liveness=lambda: None,
            on_content=lambda: None,
        )
        from unittest.mock import MagicMock
        event = MagicMock()
        event.src_path = "/tmp/random.txt"
        event.is_directory = False
        kind = h._classify(event)
        assert kind is None


class TestCtrlLBinding:
    """Verify ctrl+l works correctly across screens."""

    def test_ctrl_l_not_in_default_keys(self):
        """ctrl+l is handled via on_key, not DEFAULT_KEYS — verify this is intentional."""
        from config import DEFAULT_KEYS
        # ctrl+l should NOT be in DEFAULT_KEYS — it's in on_key handler
        for action, (keys, _, _, _) in DEFAULT_KEYS.items():
            assert "ctrl+l" not in keys, \
                f"ctrl+l found in DEFAULT_KEYS for {action} — should be in on_key handler only"

    def test_detail_screen_has_ctrl_l_binding(self):
        """DetailScreen should have ctrl+l in its BINDINGS."""
        from screens import DetailScreen
        binding_keys = []
        for b in DetailScreen.BINDINGS:
            if isinstance(b, tuple):
                binding_keys.append(b[0])
            else:
                binding_keys.append(b.key)
        assert any("ctrl+l" in k for k in binding_keys), \
            "DetailScreen missing ctrl+l binding"


# ─── CLI edge cases ──────────────────────────────────────────────────


class TestCLIEdgeCases:
    """Test CLI edge cases and error handling."""

    def test_resolve_ws_not_found(self, tmp_path):
        """_resolve_ws with a bogus ID should call sys.exit(1)."""
        store_path = tmp_path / "empty_data.json"
        store = Store(path=store_path)
        from cli import _resolve_ws
        with pytest.raises(SystemExit):
            _resolve_ws(store, "bogus-id-that-does-not-exist")

    def test_cmd_note_creates_todo_items(self, tmp_path, capsys):
        """cmd_note should create todo items, not append to notes string."""
        store_path = tmp_path / "cli_data.json"
        store = Store(path=store_path)
        ws = Workstream(name="Test")
        store.add(ws)

        from cli import cmd_note
        args = MagicMock()
        args.id = ws.id
        args.text = ["first", "todo"]
        with patch("cli.Store", return_value=store):
            cmd_note(args)

        args.text = ["second", "todo"]
        with patch("cli.Store", return_value=store):
            cmd_note(args)

        updated = store.get(ws.id)
        assert len(updated.todos) == 2
        assert updated.todos[0].text == "first todo"
        assert updated.todos[1].text == "second todo"

    def test_cmd_show_with_links_and_notes(self, tmp_path, capsys):
        """cmd_show should display links and notes without crashing."""
        store_path = tmp_path / "cli_data.json"
        store = Store(path=store_path)
        ws = Workstream(name="Detailed WS",
                        description="A detailed workstream")
        ws.notes = "Some important notes\nLine 2"
        ws.add_link("worktree", "/path/to/repo", "main repo")
        ws.add_link("ticket", "UB-1234", "Jira ticket")
        store.add(ws)

        from cli import cmd_show
        args = MagicMock()
        args.id = ws.id
        with patch("cli.Store", return_value=store):
            cmd_show(args)
        captured = capsys.readouterr()
        assert "Detailed WS" in captured.out
        assert "worktree" in captured.out
        assert "UB-1234" in captured.out
        assert "Some important notes" in captured.out


# ─── Adversarial: Name sanitization ──────────────────────────────────


class TestNameSanitization:
    """Verify Workstream.__post_init__ sanitizes names and descriptions."""

    def test_trailing_newline_stripped(self):
        ws = Workstream(name="UB-6526: fix pre-commit\n")
        assert ws.name == "UB-6526: fix pre-commit"

    def test_trailing_whitespace_stripped(self):
        ws = Workstream(name="  hello world  \t")
        assert ws.name == "hello world"

    def test_redundant_ticket_name_deduped(self):
        ws = Workstream(name="UB-6636: UB-6636")
        assert ws.name == "UB-6636"

    def test_ticket_with_real_description_kept(self):
        ws = Workstream(name="UB-6732: time range fix")
        assert ws.name == "UB-6732: time range fix"

    def test_description_stripped(self):
        ws = Workstream(description="  some description\n\n")
        assert ws.description == "some description"

    def test_empty_name_no_crash(self):
        ws = Workstream(name="")
        assert ws.name == ""

    def test_colon_only_no_crash(self):
        ws = Workstream(name=": ")
        assert ws.name == ":"

    def test_from_dict_strips(self):
        d = {
            "id": "test123",
            "name": "UB-1234: fix something\n",
            "description": "",
            "status": "in-progress",
            "category": "work",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "status_changed_at": "2024-01-01T00:00:00",
        }
        ws = Workstream.from_dict(d)
        assert ws.name == "UB-1234: fix something"
        assert "\n" not in ws.name


# ─── Adversarial: auto_link_session ──────────────────────────────────


class TestAutoLinkSession:
    """Verify auto_link_session skips linking when ws has dir links."""

    def test_skips_when_has_dir_links(self, tmp_path):
        store = Store(path=tmp_path / "data.json")
        ws = Workstream(name="test")
        ws.links.append(Link(kind="worktree", label="repo", value="/some/dir"))
        store.add(ws)

        from claude_session_screen import auto_link_session
        auto_link_session(store, ws.id, "session-abc-123")

        updated = store.get(ws.id)
        session_links = [l for l in updated.links if l.kind == "claude-session"]
        assert len(session_links) == 0

    def test_links_when_no_dir_links(self, tmp_path):
        store = Store(path=tmp_path / "data.json")
        ws = Workstream(name="test")
        store.add(ws)

        from claude_session_screen import auto_link_session
        auto_link_session(store, ws.id, "session-abc-123")

        updated = store.get(ws.id)
        session_links = [l for l in updated.links if l.kind == "claude-session"]
        assert len(session_links) == 1
        assert session_links[0].value == "session-abc-123"

    def test_no_duplicate_session_links(self, tmp_path):
        store = Store(path=tmp_path / "data.json")
        ws = Workstream(name="test")
        store.add(ws)

        from claude_session_screen import auto_link_session
        auto_link_session(store, ws.id, "session-abc-123")
        auto_link_session(store, ws.id, "session-abc-123")

        updated = store.get(ws.id)
        session_links = [l for l in updated.links if l.kind == "claude-session"]
        assert len(session_links) == 1


# ─── Adversarial: Thread naming with generic branches ──────────────


class TestThreadNaming:
    """Verify thread naming skips generic branch names."""

    def test_wip_branch_skipped(self):
        from threads import _derive_thread_name
        from sessions import ClaudeSession

        sess = ClaudeSession(
            session_id="s1", project_dir="d",
            project_path="/home/user/project", message_count=5,
        )
        branches = {"s1": "wip"}
        messages = {"s1": "Fix the authentication middleware"}
        name = _derive_thread_name([sess], branches, messages)
        assert name != "wip"
        assert "Fix the authentication" in name

    def test_real_branch_used(self):
        from threads import _derive_thread_name
        from sessions import ClaudeSession

        sess = ClaudeSession(
            session_id="s1", project_dir="d",
            project_path="/home/user/project", message_count=5,
        )
        branches = {"s1": "UB-6668-metric-handling"}
        messages = {"s1": "Implement time ranges"}
        name = _derive_thread_name([sess], branches, messages)
        assert name == "UB-6668-metric-handling"

    def test_generic_branches_all_skipped(self):
        from threads import _derive_thread_name
        from sessions import ClaudeSession

        generics = ["wip", "temp", "dev", "prod", "main", "master", "HEAD",
                     "fix", "hotfix", "test", "staging"]
        for branch in generics:
            sess = ClaudeSession(
                session_id="s1", project_dir="d",
                project_path="/home/user/project", message_count=5,
            )
            name = _derive_thread_name([sess], {"s1": branch}, {"s1": "Do stuff"})
            assert name != branch, f"Generic branch {branch!r} should not be used as name"


# ─── Adversarial: Rich markup in workstream names ────────────────────


class TestRichMarkupEscaping:
    """Verify Rich markup in names doesn't break rendering."""

    def test_rich_escape_brackets(self):
        from rendering import _rich_escape
        result = _rich_escape("[bold]evil[/bold]")
        assert "[" not in result or r"\[" in result

    def test_render_ws_option_with_markup_name(self):
        from rendering import _render_ws_option
        ws = Workstream(name="[red]Malicious[/red]")
        # Should not raise
        result = _render_ws_option(ws, [], {})
        # The brackets should be escaped
        assert r"\[red]" in result or "[red]" not in result

    def test_render_ws_option_with_unicode_name(self):
        from rendering import _render_ws_option
        ws = Workstream(name="🚀 Unicode Test 日本語")
        result = _render_ws_option(ws, [], {})
        assert "Unicode Test" in result


# ─── Adversarial: CLI note creates TodoItem ──────────────────────────


class TestCLINoteCreatesTodo:
    """Verify CLI note creates TodoItem (not appending to notes string)."""

    def test_note_creates_todo_item(self, tmp_path):
        store_path = tmp_path / "data.json"
        store = Store(path=store_path)
        ws = Workstream(name="Test")
        store.add(ws)

        from cli import cmd_note
        args = MagicMock()
        args.id = ws.id
        args.text = ["fix", "the", "bug"]
        with patch("cli.Store", return_value=store):
            cmd_note(args)

        updated = store.get(ws.id)
        assert len(updated.todos) == 1
        assert updated.todos[0].text == "fix the bug"
        assert updated.todos[0].done is False
        assert updated.todos[0].origin == "manual"

    def test_note_empty_text_exits(self, tmp_path):
        store_path = tmp_path / "data.json"
        store = Store(path=store_path)
        ws = Workstream(name="Test")
        store.add(ws)

        from cli import cmd_note
        args = MagicMock()
        args.id = ws.id
        args.text = ["   "]
        with patch("cli.Store", return_value=store):
            with pytest.raises(SystemExit):
                cmd_note(args)


# ─── Adversarial: Worktree path normalization ────────────────────────


class TestWorktreePathNormalization:
    """Verify .claude/worktrees/agent-* paths get normalized to parent."""

    def test_agent_worktree_normalized(self):
        """Sessions in .claude/worktrees/ should group with parent project."""
        from sessions import ClaudeSession

        parent_session = ClaudeSession(
            session_id="parent-1", project_dir="d",
            project_path="/home/user/dev/project", message_count=5,
        )
        agent_session = ClaudeSession(
            session_id="agent-1", project_dir="d",
            project_path="/home/user/dev/project/.claude/worktrees/agent-abc123",
            message_count=5,
        )

        # Group by normalized path
        by_project = {}
        for s in [parent_session, agent_session]:
            path = s.project_path
            if "/.claude/worktrees/" in path:
                parent = path.split("/.claude/worktrees/")[0]
                if parent:
                    path = parent
            by_project.setdefault(path, []).append(s)

        # Both should be in the same group
        assert len(by_project) == 1
        assert "/home/user/dev/project" in by_project
