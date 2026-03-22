"""Tests for sessions.py — Claude session discovery and parsing."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sessions import (
    ClaudeSession,
    _decode_project_dir,
    get_live_session_ids,
    parse_session,
    refresh_session_tail,
    discover_sessions,
    find_session,
    sessions_for_project,
)


# ─── ClaudeSession Properties ───────────────────────────────────────

class TestClaudeSessionTokensDisplay:
    def test_zero_tokens_display(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p")
        assert s.tokens_display == "0"

    def test_small_tokens_display(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          total_input_tokens=1000, total_output_tokens=100)
        assert s.tokens_display == "1.1k"

    def test_large_tokens_display(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          total_input_tokens=5_000_000, total_output_tokens=2_000_000)
        assert s.tokens_display == "7.0M"


class TestClaudeSessionTokens:
    def test_small_tokens(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          total_input_tokens=500, total_output_tokens=200)
        assert s.tokens_display == "700"

    def test_k_tokens(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          total_input_tokens=50_000, total_output_tokens=20_000)
        assert "k" in s.tokens_display

    def test_m_tokens(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          total_input_tokens=5_000_000, total_output_tokens=2_000_000)
        assert "M" in s.tokens_display


class TestClaudeSessionDisplay:
    def test_display_name_with_title(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/home/user/project",
                          title="My Session")
        assert s.display_name == "My Session"

    def test_display_name_without_title(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/home/user/project")
        assert "project" in s.display_name or "/" in s.display_name

    def test_age_unknown(self):
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p")
        assert s.age == "unknown"

    def test_age_recent(self):
        ts = datetime.now().astimezone().isoformat()
        s = ClaudeSession(session_id="test", project_dir="d", project_path="/p",
                          last_activity=ts)
        age = s.age
        assert "ago" in age or "just now" in age


# ─── Project Dir Decoding ───────────────────────────────────────────

class TestDecodeProjectDir:
    def test_simple_path(self, tmp_path):
        """Test with an actual path that exists."""
        # Create the directories
        (tmp_path / "project").mkdir()
        dirname = str(tmp_path / "project").replace("/", "-").lstrip("-")
        result = _decode_project_dir(dirname)
        assert result.endswith("project")

    def test_preserves_structure(self):
        """Even without real paths, should produce a path-like string."""
        result = _decode_project_dir("-home-user-dev-my-project")
        assert result.startswith("/")

    def test_dotfile_double_dash(self, tmp_path):
        """Double-dash encodes dotfiles: --xmonad → .xmonad."""
        (tmp_path / ".xmonad").mkdir()
        dirname = str(tmp_path / ".xmonad").replace("/", "-").replace("-.", "--").lstrip("-")
        result = _decode_project_dir(dirname)
        assert result == str(tmp_path / ".xmonad")

    def test_nested_dotfile(self, tmp_path):
        """Nested dotfiles: --dotfiles-xdg--config → .dotfiles/xdg/.config."""
        (tmp_path / ".dotfiles" / "xdg" / ".config").mkdir(parents=True)
        dirname = str(tmp_path / ".dotfiles" / "xdg" / ".config").replace("/", "-").replace("-.", "--").lstrip("-")
        result = _decode_project_dir(dirname)
        assert result == str(tmp_path / ".dotfiles" / "xdg" / ".config")


# ─── Session Parsing ────────────────────────────────────────────────

class TestParseSession:
    def test_parse_valid_session(self, sample_session_jsonl):
        projects_dir, session_file = sample_session_jsonl
        session = parse_session(session_file)
        assert session is not None
        assert session.title == "Test Session"
        assert session.message_count == 2
        assert session.total_input_tokens > 0
        assert session.total_output_tokens > 0
        assert session.model == "claude-opus-4-6"

    def test_parse_token_counts(self, sample_session_jsonl):
        _, session_file = sample_session_jsonl
        session = parse_session(session_file)
        # First message: 1000 + 200 + 300 = 1500 input, 500 output
        # Second message: 2000 input, 1000 output
        # Total: 3500 input, 1500 output
        assert session.total_input_tokens == 3500
        assert session.total_output_tokens == 1500

    def test_parse_timestamps(self, sample_session_jsonl):
        _, session_file = sample_session_jsonl
        session = parse_session(session_file)
        assert "2026-03-20T10:00:00" in session.started_at
        assert "2026-03-20T10:05:00" in session.last_activity

    def test_skip_wakatime_files(self, tmp_path):
        """Wakatime files are filtered in discover_sessions via glob, not parse_session.
        parse_session checks stem.endswith('.wakatime') but Path.stem strips
        only the last extension, so .jsonl.wakatime stem is 'some-session.jsonl'.
        The real filter happens in discover_sessions' 'if name.endswith(".wakatime")' check.
        """
        waka_file = tmp_path / "projects" / "test" / "some-session.jsonl.wakatime"
        waka_file.parent.mkdir(parents=True)
        waka_file.write_text("")
        # parse_session will actually process it (stem='some-session.jsonl')
        # but discover_sessions skips it via the name endswith check
        result = parse_session(waka_file)
        assert result is not None  # parse_session doesn't catch this case
        assert result.message_count == 0

    def test_parse_empty_file(self, tmp_path):
        empty = tmp_path / "projects" / "test" / "empty.jsonl"
        empty.parent.mkdir(parents=True)
        empty.write_text("")
        session = parse_session(empty)
        assert session is not None
        assert session.message_count == 0

    def test_interrupt_marker_sets_turn_complete(self, tmp_path):
        """Full parse: '[Request interrupted]' user message marks turn complete."""
        f = tmp_path / "projects" / "test" / "session.jsonl"
        f.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "message": {"content": "do the thing"},
                        "timestamp": "2026-03-20T10:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "content": [{"type": "tool_use", "name": "bash"}],
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "stop_reason": "tool_use"},
                        "timestamp": "2026-03-20T10:01:00Z"}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user]"}
            ]}, "timestamp": "2026-03-20T10:01:05Z"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        session = parse_session(f)
        assert session.turn_complete is True

    def test_user_message_resets_stale_stop_reason(self, tmp_path):
        """User message must clear last_stop_reason so session_activity sees THINKING, not stale 'end_turn'."""
        f = tmp_path / "projects" / "test" / "session.jsonl"
        f.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "message": {"content": "first prompt"},
                        "timestamp": "2026-03-20T10:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "stop_reason": "end_turn"},
                        "timestamp": "2026-03-20T10:01:00Z"}),
            json.dumps({"type": "user", "message": {"content": "second prompt"},
                        "timestamp": "2026-03-20T10:02:00Z"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        session = parse_session(f)
        assert session.last_stop_reason == ""
        assert session.last_tool_name == ""

    def test_parse_malformed_json(self, tmp_path):
        bad = tmp_path / "projects" / "test" / "bad.jsonl"
        bad.parent.mkdir(parents=True)
        bad.write_text("not json\nalso not json\n")
        session = parse_session(bad)
        assert session is not None  # Should handle gracefully
        assert session.message_count == 0

    def test_parse_tracks_last_user_message_at(self, tmp_path):
        f = tmp_path / "projects" / "test" / "session.jsonl"
        f.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-03-20T10:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 50}},
                        "timestamp": "2026-03-20T10:01:00Z"}),
            json.dumps({"type": "user", "timestamp": "2026-03-20T10:02:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 50}},
                        "timestamp": "2026-03-20T10:03:00Z"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        session = parse_session(f)
        assert session.last_user_message_at == "2026-03-20T10:02:00Z"
        assert session.last_activity == "2026-03-20T10:03:00Z"


# ─── Last Message Text ─────────────────────────────────────────────

class TestExtractMessageText:
    def test_user_string_content(self):
        from sessions import _extract_message_text
        data = {"type": "user", "message": {"content": "Hello world"}}
        assert _extract_message_text(data) == "Hello world"

    def test_user_list_content(self):
        from sessions import _extract_message_text
        data = {"type": "user", "message": {"content": [
            {"type": "text", "text": "Fix the bug in main.py"}
        ]}}
        assert _extract_message_text(data) == "Fix the bug in main.py"

    def test_assistant_list_content(self):
        from sessions import _extract_message_text
        data = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I'll fix that for you."},
            {"type": "tool_use", "name": "edit"}
        ]}}
        assert _extract_message_text(data) == "I'll fix that for you."

    def test_multiline_collapsed(self):
        from sessions import _extract_message_text
        data = {"type": "user", "message": {"content": "line1\n  line2\nline3"}}
        assert _extract_message_text(data) == "line1 line2 line3"

    def test_no_message_returns_empty(self):
        from sessions import _extract_message_text
        assert _extract_message_text({"type": "user"}) == ""

    def test_skips_interrupted_messages(self):
        from sessions import _extract_message_text
        data = {"type": "user", "message": {"content": [
            {"type": "text", "text": "[Request interrupted by user]"}
        ]}}
        assert _extract_message_text(data) == ""

    def test_truncates_long_content(self):
        from sessions import _extract_message_text
        data = {"type": "user", "message": {"content": "x" * 300}}
        assert len(_extract_message_text(data)) == 200


class TestParseSessionLastMessage:
    def test_tracks_last_message_text(self, tmp_path):
        f = tmp_path / "projects" / "test" / "session.jsonl"
        f.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "message": {"content": "first question"},
                        "timestamp": "2026-03-20T10:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "content": [{"type": "text", "text": "here is my answer"}],
                        "usage": {"input_tokens": 100, "output_tokens": 50}},
                        "timestamp": "2026-03-20T10:01:00Z"}),
            json.dumps({"type": "user", "message": {"content": "follow up"},
                        "timestamp": "2026-03-20T10:02:00Z"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        session = parse_session(f)
        assert session.last_message_text == "follow up"
        assert session.last_message_role == "user"

    def test_assistant_last_message(self, tmp_path):
        f = tmp_path / "projects" / "test" / "session.jsonl"
        f.parent.mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "message": {"content": "hello"},
                        "timestamp": "2026-03-20T10:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6",
                        "content": [{"type": "text", "text": "Done! I've fixed the issue."}],
                        "usage": {"input_tokens": 100, "output_tokens": 50}},
                        "timestamp": "2026-03-20T10:01:00Z"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        session = parse_session(f)
        assert session.last_message_text == "Done! I've fixed the issue."
        assert session.last_message_role == "assistant"


class TestRefreshSessionTailLastMessage:
    def _write_jsonl(self, path, lines):
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def test_updates_last_message_text(self, tmp_path):
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "user", "message": {"content": "do the thing"},
             "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}],
             "usage": {}}, "timestamp": "2026-03-20T10:01:00Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="",
                          last_activity="")
        refresh_session_tail(s)
        assert s.last_message_text == "Done."
        assert s.last_message_role == "assistant"


# ─── Tail Refresh ──────────────────────────────────────────────────

class TestRefreshSessionTail:
    def _write_jsonl(self, path, lines):
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def test_detects_role_change(self, tmp_path):
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "user", "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}], "usage": {}}, "timestamp": "2026-03-20T10:01:00Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="user",
                          last_activity="2026-03-20T10:00:00Z")
        changed = refresh_session_tail(s)
        assert changed is True
        assert s.last_message_role == "assistant"
        assert s.last_activity == "2026-03-20T10:01:00Z"

    def test_tracks_last_user_message_at(self, tmp_path):
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "user", "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "assistant", "message": {"usage": {}}, "timestamp": "2026-03-20T10:01:00Z"},
            {"type": "user", "timestamp": "2026-03-20T10:02:00Z"},
            {"type": "assistant", "message": {"usage": {}}, "timestamp": "2026-03-20T10:03:00Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="",
                          last_activity="")
        refresh_session_tail(s)
        assert s.last_user_message_at == "2026-03-20T10:02:00Z"
        assert s.last_activity == "2026-03-20T10:03:00Z"

    def test_no_change_returns_false(self, tmp_path):
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "user", "timestamp": "2026-03-20T10:00:00Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="user",
                          last_activity="2026-03-20T10:00:00Z")
        changed = refresh_session_tail(s)
        assert changed is False

    def test_missing_file_returns_false(self):
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path="/nonexistent/path.jsonl",
                          last_message_role="user", last_activity="")
        assert refresh_session_tail(s) is False

    def test_stop_hook_summary_sets_turn_complete(self, tmp_path):
        """system:stop_hook_summary is a definitive turn-complete signal."""
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "assistant", "message": {"usage": {}, "stop_reason": "tool_use"},
             "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "user", "timestamp": "2026-03-20T10:00:01Z"},
            {"type": "system", "subtype": "stop_hook_summary",
             "timestamp": "2026-03-20T10:00:02Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="",
                          last_activity="")
        refresh_session_tail(s)
        assert s.turn_complete is True

    def test_interrupt_marker_sets_turn_complete(self, tmp_path):
        """'[Request interrupted by user]' user message marks turn as complete."""
        f = tmp_path / "s.jsonl"
        self._write_jsonl(f, [
            {"type": "assistant", "message": {"usage": {}, "stop_reason": "tool_use"},
             "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user]"}
            ]}, "timestamp": "2026-03-20T10:00:01Z"},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="",
                          last_activity="")
        refresh_session_tail(s)
        assert s.turn_complete is True
        # Interrupt marker has no extractable text, so role stays unchanged
        assert s.last_message_role == ""

    def test_reads_only_tail(self, tmp_path):
        """With a large file, still correctly reads the last entries."""
        f = tmp_path / "s.jsonl"
        # Write a bunch of padding lines followed by the real data
        lines = [{"type": "user", "timestamp": f"2026-03-20T09:{i:02d}:00Z"}
                 for i in range(50)]
        lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}], "usage": {}},
                       "timestamp": "2026-03-20T10:05:00Z"})
        self._write_jsonl(f, lines)
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="user",
                          last_activity="2026-03-20T09:00:00Z")
        changed = refresh_session_tail(s)
        assert changed is True
        assert s.last_message_role == "assistant"
        assert s.last_activity == "2026-03-20T10:05:00Z"


# ─── Session Discovery ──────────────────────────────────────────────

class TestDiscoverSessions:
    @pytest.fixture(autouse=True)
    def _mock_live(self):
        with patch("sessions.get_live_session_ids", return_value=set()):
            yield

    def test_discover_with_mock(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=10)
            assert len(sessions) >= 1
            assert sessions[0].title == "Test Session"

    def test_discover_with_filter(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=10, project_filter="test-project")
            assert len(sessions) >= 1

    def test_discover_no_match(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=10, project_filter="nonexistent")
            assert len(sessions) == 0

    def test_discover_min_messages(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=10, min_messages=100)
            assert len(sessions) == 0

    def test_discover_nonexistent_dir(self, tmp_path):
        fake = tmp_path / "nonexistent"
        with patch("sessions.CLAUDE_PROJECTS_DIR", fake):
            sessions = discover_sessions()
            assert sessions == []

    def test_discover_unlimited(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=0)
            assert len(sessions) >= 1  # limit=0 means unlimited

    def test_discover_limit_applied(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            sessions = discover_sessions(limit=1)
            assert len(sessions) == 1

    def test_discover_marks_live_sessions(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        live_id = "abc12345-6789-0000-1111-222233334444"
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir), \
             patch("sessions.get_live_session_ids", return_value={live_id}):
            sessions = discover_sessions()
            live = [s for s in sessions if s.is_live]
            assert len(live) == 1
            assert live[0].session_id == live_id


class TestGetLiveSessionIds:
    def test_reads_session_files(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # Write a session file with current PID (guaranteed alive)
        import os
        pid = os.getpid()
        (sessions_dir / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": "live-session-id", "cwd": "/tmp"})
        )
        with patch("sessions.CLAUDE_SESSIONS_DIR", sessions_dir):
            live = get_live_session_ids()
            assert "live-session-id" in live

    def test_skips_dead_pid(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # Use a PID that almost certainly doesn't exist
        (sessions_dir / "999999999.json").write_text(
            json.dumps({"pid": 999999999, "sessionId": "dead-session", "cwd": "/tmp"})
        )
        with patch("sessions.CLAUDE_SESSIONS_DIR", sessions_dir):
            live = get_live_session_ids()
            assert "dead-session" not in live

    def test_nonexistent_dir(self, tmp_path):
        fake = tmp_path / "nonexistent"
        with patch("sessions.CLAUDE_SESSIONS_DIR", fake):
            assert get_live_session_ids() == set()


class TestFindSession:
    def test_find_by_full_id(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            session = find_session("abc12345-6789-0000-1111-222233334444")
            assert session is not None
            assert session.title == "Test Session"

    def test_find_by_prefix(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            session = find_session("abc12345")
            assert session is not None

    def test_find_nonexistent(self, sample_session_jsonl):
        projects_dir, _ = sample_session_jsonl
        with patch("sessions.CLAUDE_PROJECTS_DIR", projects_dir):
            session = find_session("zzz-nonexistent")
            assert session is None
