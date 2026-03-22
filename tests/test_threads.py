"""Tests for threads.py — auto-clustering of Claude sessions into threads."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sessions import ClaudeSession
from threads import (
    Thread,
    ThreadActivity,
    _should_merge,
    _derive_thread_name,
    _extract_git_branch,
    _extract_first_message,
    discover_threads,
    session_activity,
    DEFAULT_BRANCH_GAP,
    FRESH_THRESHOLD,
)


# ─── Thread Properties ───────────────────────────────────────────────

class TestThread:
    def _make_session(self, **kwargs):
        defaults = dict(session_id="s1", project_dir="d", project_path="/p",
                        message_count=5, total_input_tokens=1000,
                        total_output_tokens=500, model="claude-opus-4-6",
                        started_at="2026-03-20T10:00:00Z",
                        last_activity="2026-03-20T10:05:00Z")
        defaults.update(kwargs)
        return ClaudeSession(**defaults)

    def test_is_live(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=True)])
        assert t.is_live is True

    def test_not_live(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=False)])
        assert t.is_live is False

    def test_session_count(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(), self._make_session(session_id="s2")])
        assert t.session_count == 2

    def test_total_tokens(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(), self._make_session(session_id="s2")])
        assert t.total_tokens > 0

    def test_total_messages(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(message_count=3),
                             self._make_session(session_id="s2", message_count=7)])
        assert t.total_messages == 10

    def test_display_name_with_name(self):
        t = Thread(thread_id="t1", name="My Thread", project_path="/home/user/dev/project")
        assert t.display_name == "My Thread"

    def test_display_name_falls_back_to_project(self):
        t = Thread(thread_id="t1", name="", project_path="/home/user/dev/project")
        assert "project" in t.display_name

    def test_age_recent(self):
        ts = datetime.now().astimezone().isoformat()
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(last_activity=ts)])
        assert "ago" in t.age or "just now" in t.age

    def test_models(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(model="claude-opus-4-6"),
                             self._make_session(session_id="s2", model="claude-sonnet-4-6")])
        assert len(t.models) == 2

    def test_last_user_activity(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[
                       self._make_session(last_user_message_at="2026-03-20T10:00:00Z"),
                       self._make_session(session_id="s2", last_user_message_at="2026-03-20T10:05:00Z"),
                   ])
        assert t.last_user_activity == "2026-03-20T10:05:00Z"

    def test_last_user_activity_falls_back_to_last_activity(self):
        """When no user messages tracked, falls back to last_activity."""
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(last_user_message_at="")])
        assert t.last_user_activity == t.last_activity

    def test_last_user_activity_empty_sessions(self):
        t = Thread(thread_id="t1", name="test", project_path="/p", sessions=[])
        assert t.last_user_activity == ""


# ─── Merge Logic ─────────────────────────────────────────────────────

class TestShouldMerge:
    def _session_at(self, ts_str, **kwargs):
        return ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                             started_at=ts_str, last_activity=ts_str, **kwargs)

    def test_same_feature_branch_merges_regardless_of_time(self):
        a = self._session_at("2026-03-10T10:00:00Z")
        b = self._session_at("2026-03-20T10:00:00Z")  # 10 days later
        assert _should_merge(a, b, "feature/my-branch", "feature/my-branch") is True

    def test_different_feature_branches_never_merge(self):
        a = self._session_at("2026-03-20T10:00:00Z")
        b = self._session_at("2026-03-20T10:01:00Z")  # 1 min later
        assert _should_merge(a, b, "feature/a", "feature/b") is False

    def test_default_branches_merge_within_gap(self):
        a = self._session_at("2026-03-20T10:00:00Z")
        b = self._session_at("2026-03-20T10:20:00Z")  # 20 min later
        assert _should_merge(a, b, "main", "main") is True

    def test_default_branches_split_beyond_gap(self):
        a = self._session_at("2026-03-20T10:00:00Z")
        b = self._session_at("2026-03-20T12:00:00Z")  # 2 hours later
        assert _should_merge(a, b, "main", "main") is False

    def test_feature_vs_default_never_merge(self):
        a = self._session_at("2026-03-20T10:00:00Z")
        b = self._session_at("2026-03-20T10:01:00Z")
        assert _should_merge(a, b, "feature/x", "main") is False
        assert _should_merge(a, b, "main", "feature/x") is False

    def test_empty_branches_treated_as_default(self):
        a = self._session_at("2026-03-20T10:00:00Z")
        b = self._session_at("2026-03-20T10:10:00Z")
        assert _should_merge(a, b, "", "") is True  # within gap

    def test_missing_timestamps_no_merge(self):
        a = self._session_at("")
        b = self._session_at("2026-03-20T10:00:00Z")
        assert _should_merge(a, b, "main", "main") is False


# ─── Thread Name Derivation ──────────────────────────────────────────

class TestDeriveThreadName:
    def _make_session(self, **kwargs):
        defaults = dict(session_id="s1", project_dir="d", project_path="/p")
        defaults.update(kwargs)
        return ClaudeSession(**defaults)

    def test_prefers_custom_title(self):
        s = self._make_session(title="My Custom Title")
        name = _derive_thread_name([s], {}, {})
        assert name == "My Custom Title"

    def test_uses_feature_branch(self):
        s = self._make_session()
        name = _derive_thread_name([s], {"s1": "UB-1234-fix-thing"}, {})
        assert "UB-1234" in name

    def test_ignores_default_branch(self):
        s = self._make_session()
        name = _derive_thread_name([s], {"s1": "master"}, {"s1": "hello world"})
        assert name == "hello world"

    def test_uses_first_message_as_fallback(self):
        s = self._make_session()
        name = _derive_thread_name([s], {}, {"s1": "fix the login page bug"})
        assert "fix the login page" in name

    def test_empty_when_no_signals(self):
        s = self._make_session()
        name = _derive_thread_name([s], {}, {})
        assert name == ""


# ─── Git Branch Extraction ───────────────────────────────────────────

class TestExtractGitBranch:
    def test_extracts_branch(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user", "gitBranch": "feature/my-branch",
            "message": {"role": "user", "content": "hello"},
        }) + "\n")
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(jsonl))
        assert _extract_git_branch(s) == "feature/my-branch"

    def test_skips_HEAD(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user", "gitBranch": "HEAD",
            "message": {"role": "user", "content": "hello"},
        }) + "\n")
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(jsonl))
        assert _extract_git_branch(s) == ""


# ─── First Message Extraction ────────────────────────────────────────

class TestExtractFirstMessage:
    def test_extracts_string_content(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "fix the auth bug"},
        }) + "\n")
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(jsonl))
        assert _extract_first_message(s) == "fix the auth bug"

    def test_extracts_list_content(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "hello world"}
            ]},
        }) + "\n")
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(jsonl))
        assert _extract_first_message(s) == "hello world"

    def test_skips_interrupted_messages(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "[Request interrupted by user]"}
                ]},
            }),
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "real message"},
            }),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(jsonl))
        assert _extract_first_message(s) == "real message"


# ─── Full Discovery ──────────────────────────────────────────────────

class TestDiscoverThreads:
    def test_groups_same_branch_sessions(self, tmp_path):
        """Sessions on the same feature branch should cluster together."""
        proj_dir = tmp_path / "projects" / "-test-project"
        proj_dir.mkdir(parents=True)

        # Two sessions on same feature branch, 1 week apart
        for i, (sid, ts) in enumerate([
            ("aaa", "2026-03-10T10:00:00Z"),
            ("bbb", "2026-03-17T10:00:00Z"),
        ]):
            data = [
                json.dumps({"type": "user", "gitBranch": "feature/xyz",
                            "message": {"role": "user", "content": f"msg {i}"},
                            "timestamp": ts, "sessionId": sid}),
                json.dumps({"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "model": "claude-opus-4-6",
                }, "timestamp": ts, "sessionId": sid}),
            ]
            (proj_dir / f"{sid}.jsonl").write_text("\n".join(data) + "\n")

        with patch("sessions.CLAUDE_PROJECTS_DIR", tmp_path / "projects"), \
             patch("sessions.get_live_session_ids", return_value=set()), \
             patch("threads.CLAUDE_PROJECTS_DIR", tmp_path / "projects"):
            threads = discover_threads()
            assert len(threads) == 1  # Should be merged
            assert threads[0].session_count == 2

    def test_splits_different_branches(self, tmp_path):
        """Sessions on different feature branches should be separate threads."""
        proj_dir = tmp_path / "projects" / "-test-project"
        proj_dir.mkdir(parents=True)

        for sid, branch in [("aaa", "feature/a"), ("bbb", "feature/b")]:
            ts = "2026-03-20T10:00:00Z"
            data = [
                json.dumps({"type": "user", "gitBranch": branch,
                            "message": {"role": "user", "content": "hello"},
                            "timestamp": ts, "sessionId": sid}),
                json.dumps({"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "model": "claude-opus-4-6",
                }, "timestamp": ts, "sessionId": sid}),
            ]
            (proj_dir / f"{sid}.jsonl").write_text("\n".join(data) + "\n")

        with patch("sessions.CLAUDE_PROJECTS_DIR", tmp_path / "projects"), \
             patch("sessions.get_live_session_ids", return_value=set()), \
             patch("threads.CLAUDE_PROJECTS_DIR", tmp_path / "projects"):
            threads = discover_threads()
            assert len(threads) == 2


# ─── Thread & Session Activity ──────────────────────────────────────

class TestSessionActivity:
    def _make_session(self, **kwargs):
        defaults = dict(session_id="s1", project_dir="d", project_path="/p",
                        message_count=5, total_input_tokens=1000,
                        total_output_tokens=500, model="claude-opus-4-6",
                        started_at="2026-03-20T10:00:00Z",
                        last_activity="2026-03-20T10:05:00Z")
        defaults.update(kwargs)
        return ClaudeSession(**defaults)

    def test_thinking_when_live_and_last_is_user(self):
        s = self._make_session(is_live=True, last_message_role="user")
        assert session_activity(s) == ThreadActivity.THINKING

    def test_awaiting_input_when_live_and_no_role(self):
        """No messages yet → user hasn't typed, Claude is idle at prompt."""
        s = self._make_session(is_live=True, last_message_role="")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_awaiting_input_when_turn_complete(self):
        """turn_complete flag (from system:turn_duration) is the primary signal."""
        s = self._make_session(is_live=True, last_message_role="user",
                               last_stop_reason="tool_use", turn_complete=True)
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_awaiting_input_when_live_and_last_is_assistant_end_turn(self):
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="end_turn")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_thinking_when_live_and_last_is_assistant_tool_use(self):
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="tool_use")
        assert session_activity(s) == ThreadActivity.THINKING

    def test_thinking_when_live_and_no_stop_reason(self):
        """No definitive signal → conservative THINKING."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="")
        assert session_activity(s) == ThreadActivity.THINKING

    def test_awaiting_input_when_live_and_stop_sequence(self):
        """stop_sequence is a valid turn-complete signal (not just end_turn)."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="stop_sequence")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_awaiting_input_when_live_and_max_tokens(self):
        """max_tokens also means the turn is done."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="max_tokens")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_awaiting_input_when_interactive_tool(self):
        """Interactive tools like AskUserQuestion mean Claude is waiting for user."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="tool_use",
                               last_tool_name="AskUserQuestion")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_awaiting_input_when_exit_plan_mode(self):
        """ExitPlanMode is an interactive tool (plan confirmation poll)."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="tool_use",
                               last_tool_name="ExitPlanMode")
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_thinking_when_regular_tool_use(self):
        """Non-interactive tools (Bash, Read, etc.) mean Claude is still working."""
        s = self._make_session(is_live=True, last_message_role="assistant",
                               last_stop_reason="tool_use",
                               last_tool_name="Bash")
        assert session_activity(s) == ThreadActivity.THINKING

    def test_idle_when_not_live_and_last_is_user(self):
        s = self._make_session(is_live=False, last_message_role="user")
        assert session_activity(s) == ThreadActivity.IDLE

    def test_idle_when_not_live_and_no_role(self):
        s = self._make_session(is_live=False, last_message_role="")
        assert session_activity(s) == ThreadActivity.IDLE

    def test_response_fresh_when_recent_assistant(self):
        recent = datetime.now().astimezone().isoformat()
        s = self._make_session(is_live=False, last_message_role="assistant",
                               last_activity=recent)
        assert session_activity(s) == ThreadActivity.RESPONSE_FRESH

    def test_response_ready_when_old_assistant(self):
        old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        s = self._make_session(is_live=False, last_message_role="assistant",
                               last_activity=old)
        assert session_activity(s) == ThreadActivity.RESPONSE_READY

    def test_idle_when_already_seen(self):
        recent = datetime.now().astimezone().isoformat()
        future = (datetime.now().astimezone() + timedelta(minutes=1)).isoformat()
        s = self._make_session(is_live=False, last_message_role="assistant",
                               last_activity=recent)
        last_seen = {"s1": future}
        assert session_activity(s, last_seen) == ThreadActivity.IDLE

    def test_still_unread_if_seen_before_activity(self):
        activity = datetime.now().astimezone().isoformat()
        seen_before = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
        s = self._make_session(is_live=False, last_message_role="assistant",
                               last_activity=activity)
        last_seen = {"s1": seen_before}
        assert session_activity(s, last_seen) == ThreadActivity.RESPONSE_FRESH


class TestThreadActivity:
    def _make_session(self, **kwargs):
        defaults = dict(session_id="s1", project_dir="d", project_path="/p",
                        message_count=5, total_input_tokens=1000,
                        total_output_tokens=500, model="claude-opus-4-6",
                        started_at="2026-03-20T10:00:00Z",
                        last_activity="2026-03-20T10:05:00Z")
        defaults.update(kwargs)
        return ClaudeSession(**defaults)

    def test_thinking_thread(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=True, last_message_role="user")])
        assert t.activity == ThreadActivity.THINKING

    def test_awaiting_input_thread(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=True, last_message_role="assistant",
                                                last_stop_reason="end_turn")])
        assert t.activity == ThreadActivity.AWAITING_INPUT

    def test_thinking_thread_when_assistant_tool_use(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=True, last_message_role="assistant",
                                                last_stop_reason="tool_use")])
        assert t.activity == ThreadActivity.THINKING

    def test_idle_thread(self):
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=False, last_message_role="user")])
        assert t.activity == ThreadActivity.IDLE

    def test_unread_thread(self):
        old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=False, last_message_role="assistant",
                                                last_activity=old)])
        assert t.activity == ThreadActivity.RESPONSE_READY

    def test_fresh_unread_thread(self):
        recent = datetime.now().astimezone().isoformat()
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=False, last_message_role="assistant",
                                                last_activity=recent)])
        assert t.activity == ThreadActivity.RESPONSE_FRESH

    def test_seen_thread_is_idle(self):
        recent = datetime.now().astimezone().isoformat()
        future = (datetime.now().astimezone() + timedelta(minutes=1)).isoformat()
        t = Thread(thread_id="t1", name="test", project_path="/p",
                   sessions=[self._make_session(is_live=False, last_message_role="assistant",
                                                last_activity=recent)],
                   _last_seen={"t1": future})
        assert t.activity == ThreadActivity.IDLE

    def test_empty_thread_is_idle(self):
        t = Thread(thread_id="t1", name="test", project_path="/p", sessions=[])
        assert t.activity == ThreadActivity.IDLE
