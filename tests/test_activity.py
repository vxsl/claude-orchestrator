"""End-to-end tests for session activity states, transitions, and seen-ness.

Covers the full state machine:

  THINKING        — live session, Claude mid-turn
  AWAITING_INPUT  — live session, turn complete (user's turn)
  RESPONSE_READY  — not live, Claude last spoke
  IDLE            — not live, user last spoke or no messages

And the 'seen' logic in _is_session_seen:
  - anchor = last_activity (Claude's last meaningful output)
  - Exception: if user's last action ≥ Claude's last activity, treat as seen
    (covers interrupts — both "interrupted before Claude responded" and
    "interrupted after Claude responded")

'Green' (unseen) means: activity in {AWAITING_INPUT, RESPONSE_READY} AND not seen.
"""

import json
import pytest
from unittest.mock import patch

from sessions import ClaudeSession, parse_session, refresh_session_tail
from threads import ThreadActivity, session_activity
from rendering import _is_session_seen


# ── Helpers ──────────────────────────────────────────────────────────

def _session(**kwargs) -> ClaudeSession:
    """Build a minimal ClaudeSession with overrides."""
    defaults = dict(session_id="test-sid", project_dir="d", project_path="/p")
    defaults.update(kwargs)
    return ClaudeSession(**defaults)


def _jsonl(tmp_path, *lines):
    """Write JSONL lines to a temp file and return the path."""
    f = tmp_path / "session.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


# ── session_activity states ───────────────────────────────────────────

class TestSessionActivityLive:
    """Live sessions — Claude in tmux pane."""

    def test_fresh_live_session_is_awaiting_input(self):
        """A newly opened live session with no history → AWAITING_INPUT.
        turn_done = (not last_message_role) = True → user's turn."""
        s = _session(is_live=True, last_message_role="", turn_complete=False)
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_user_message_pending_is_thinking(self):
        """User sent a message, Claude hasn't responded → THINKING."""
        s = _session(
            is_live=True,
            last_message_role="user",
            turn_complete=False,
            last_stop_reason="",
        )
        assert session_activity(s) == ThreadActivity.THINKING

    def test_claude_responded_end_turn_is_awaiting_input(self):
        """Claude finished with end_turn → AWAITING_INPUT."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=False,
            last_stop_reason="end_turn",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_claude_responded_end_turn_via_turn_complete(self):
        """turn_complete=True (from turn_duration) → AWAITING_INPUT."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=True,
            last_stop_reason="",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_claude_tool_use_non_interactive_is_thinking(self):
        """Claude called a regular tool (Bash, Edit, etc.) → THINKING."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=False,
            last_stop_reason="tool_use",
            last_tool_name="Bash",
        )
        assert session_activity(s) == ThreadActivity.THINKING

    def test_claude_interactive_tool_is_awaiting_input(self):
        """Claude asked user a question (AskUserQuestion) → AWAITING_INPUT."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=False,
            last_stop_reason="tool_use",
            last_tool_name="AskUserQuestion",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_exit_plan_mode_is_awaiting_input(self):
        """ExitPlanMode is also an interactive tool → AWAITING_INPUT."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=False,
            last_stop_reason="tool_use",
            last_tool_name="ExitPlanMode",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_interrupted_before_claude_responded_is_awaiting_input(self):
        """User interrupted before Claude said anything → AWAITING_INPUT.
        turn_complete=True (set by interrupt marker), last_message_role="user"
        (interrupt has empty snippet so role isn't updated from user msg)."""
        s = _session(
            is_live=True,
            last_message_role="user",
            turn_complete=True,
            last_stop_reason="",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT

    def test_interrupted_after_claude_responded_is_awaiting_input(self):
        """User interrupted mid-response → AWAITING_INPUT.
        last_message_role stays "assistant" (interrupt has empty snippet)."""
        s = _session(
            is_live=True,
            last_message_role="assistant",
            turn_complete=True,
            last_stop_reason="",
        )
        assert session_activity(s) == ThreadActivity.AWAITING_INPUT


class TestSessionActivityNotLive:
    """Non-live sessions — Claude process has exited."""

    def test_no_messages_is_idle(self):
        """Session with zero messages → IDLE."""
        s = _session(is_live=False, message_count=0)
        assert session_activity(s) == ThreadActivity.IDLE

    def test_user_last_spoke_is_idle(self):
        """Not live, last message from user (aborted/crashed) → IDLE."""
        s = _session(is_live=False, message_count=3, last_message_role="user")
        assert session_activity(s) == ThreadActivity.IDLE

    def test_assistant_last_spoke_is_response_ready(self):
        """Not live, Claude responded, no user follow-up → RESPONSE_READY."""
        s = _session(is_live=False, message_count=4, last_message_role="assistant")
        assert session_activity(s) == ThreadActivity.RESPONSE_READY

    def test_interrupted_session_not_live_is_idle(self):
        """Session closed after interrupt → IDLE (user last acted, non-live)."""
        s = _session(
            is_live=False,
            message_count=2,
            last_message_role="user",
            turn_complete=True,
        )
        assert session_activity(s) == ThreadActivity.IDLE


# ── _is_session_seen ─────────────────────────────────────────────────

class TestIsSessionSeen:
    """'Seen' means user has acknowledged the session's current state."""

    T_OLD  = "2026-03-20T10:00:00Z"
    T_MID  = "2026-03-20T10:05:00Z"
    T_NEW  = "2026-03-20T10:10:00Z"

    def test_no_last_seen_dict_returns_false(self):
        s = _session(last_activity=self.T_NEW)
        assert _is_session_seen(s, None) is False

    def test_session_not_in_dict_returns_false(self):
        s = _session(session_id="abc", last_activity=self.T_NEW)
        assert _is_session_seen(s, {"other": self.T_NEW}) is False

    def test_seen_ts_older_than_activity_returns_false(self):
        """Classic unseen: Claude did something after user last looked."""
        s = _session(
            session_id="sid",
            last_activity=self.T_NEW,
            last_message_role="assistant",
        )
        assert _is_session_seen(s, {"sid": self.T_OLD}) is False

    def test_seen_ts_equal_to_activity_returns_true(self):
        s = _session(
            session_id="sid",
            last_activity=self.T_MID,
            last_message_role="assistant",
        )
        assert _is_session_seen(s, {"sid": self.T_MID}) is True

    def test_seen_ts_newer_than_activity_returns_true(self):
        s = _session(
            session_id="sid",
            last_activity=self.T_OLD,
            last_message_role="assistant",
        )
        assert _is_session_seen(s, {"sid": self.T_NEW}) is True

    def test_no_anchor_returns_false(self):
        """Can't determine seen/unseen without a timestamp."""
        s = _session(
            session_id="sid",
            last_activity="",
            last_user_message_at="",
        )
        assert _is_session_seen(s, {"sid": self.T_NEW}) is False

    def test_falls_back_to_last_user_message_at(self):
        """When last_activity is empty, last_user_message_at is the anchor."""
        s = _session(
            session_id="sid",
            last_activity="",
            last_user_message_at=self.T_MID,
        )
        assert _is_session_seen(s, {"sid": self.T_OLD}) is False
        assert _is_session_seen(s, {"sid": self.T_NEW}) is True

    # ── Interrupt cases (the bug) ─────────────────────────────────────

    def test_interrupted_before_claude_responded_is_seen(self):
        """c3c69fd6 pattern: user msg + interrupt, no Claude response.
        last_message_role="user", last_user_message_at=T_int=last_activity.
        Session must be seen — user caused this state, don't alert them."""
        T_int = self.T_NEW
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="user",      # user msg was last non-empty snippet
            last_activity=T_int,           # interrupt updates last_activity
            last_user_message_at=T_int,    # interrupt bumps this too
            turn_complete=True,
        )
        # seen_ts is older than the interrupt (user viewed before interrupting)
        assert _is_session_seen(s, {"sid": self.T_OLD}) is True

    def test_interrupted_after_claude_responded_is_seen(self):
        """User reads Claude's response, then interrupts the next turn.
        last_message_role="assistant" (interrupt has empty snippet).
        last_user_message_at = last_activity = T_interrupt.
        Should be seen — user was watching and actively stopped it."""
        T_int = self.T_NEW
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="assistant",  # Claude's response was last real snippet
            last_activity=T_int,            # interrupt updates last_activity
            last_user_message_at=T_int,
            turn_complete=True,
        )
        assert _is_session_seen(s, {"sid": self.T_OLD}) is True

    def test_claude_finished_naturally_is_not_seen(self):
        """Claude completed a turn; user hasn't looked since.
        last_user_message_at (T_user) < last_activity (T_done) → NOT seen."""
        T_user = self.T_OLD
        T_done = self.T_NEW
        s = _session(
            session_id="sid",
            last_message_role="assistant",
            last_activity=T_done,
            last_user_message_at=T_user,
            turn_complete=True,
        )
        # User viewed at T_MID (between T_user and T_done) — still older than T_done
        assert _is_session_seen(s, {"sid": self.T_MID}) is False

    def test_claude_finished_and_user_viewed_is_seen(self):
        """Claude finished, then user viewed the session."""
        T_done = self.T_OLD
        T_viewed = self.T_NEW
        s = _session(
            session_id="sid",
            last_message_role="assistant",
            last_activity=T_done,
            last_user_message_at="2026-03-20T09:50:00Z",  # even older
        )
        assert _is_session_seen(s, {"sid": T_viewed}) is True


# ── Parse-level: field values after parse_session ────────────────────

class TestParseInterruptBehavior:
    """Verify that parse_session correctly tracks all fields for interrupted sessions."""

    def test_interrupt_before_claude_responded(self, tmp_path):
        """c3c69fd6 pattern: user msg then immediate interrupt.
        - turn_complete=True (from interrupt marker)
        - last_message_role="user" (interrupt has empty snippet, doesn't update role)
        - last_user_message_at=T_int (interrupt bumps it)
        - last_activity=T_int (interrupt is non-CLI-local)"""
        T_user = "2026-03-20T10:00:00Z"
        T_int  = "2026-03-20T10:00:05Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "do the thing"},
             "timestamp": T_user},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        )
        s = parse_session(f)
        assert s.turn_complete is True
        assert s.last_message_role == "user"       # real user msg set it; interrupt didn't override
        assert s.last_user_message_at == T_int     # interrupt bumps this
        assert s.last_activity == T_int            # interrupt is activity

    def test_interrupt_after_claude_responded(self, tmp_path):
        """User msg → Claude responds → user interrupts next turn.
        - last_message_role stays "assistant" (interrupt has empty snippet)
        - last_user_message_at = T_int
        - last_activity = T_int"""
        T_user = "2026-03-20T10:00:00Z"
        T_resp = "2026-03-20T10:01:00Z"
        T_user2 = "2026-03-20T10:02:00Z"
        T_int  = "2026-03-20T10:02:05Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "question"},
             "timestamp": T_user},
            {"type": "assistant",
             "message": {
                 "model": "claude-opus-4-6",
                 "content": [{"type": "text", "text": "here is my answer"}],
                 "usage": {"input_tokens": 100, "output_tokens": 50},
                 "stop_reason": "end_turn",
             },
             "timestamp": T_resp},
            {"type": "user",
             "message": {"content": "follow up"},
             "timestamp": T_user2},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        )
        s = parse_session(f)
        assert s.turn_complete is True
        assert s.last_message_role == "user"       # "follow up" user msg set it; interrupt kept it
        assert s.last_user_message_at == T_int     # interrupt is most recent user action
        assert s.last_activity == T_int

    def test_interrupt_does_not_override_last_message_role_from_assistant(self, tmp_path):
        """After Claude responds with text, an interrupt must NOT change last_message_role."""
        T_user = "2026-03-20T10:00:00Z"
        T_resp = "2026-03-20T10:01:00Z"
        T_int  = "2026-03-20T10:01:05Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "question"},
             "timestamp": T_user},
            {"type": "assistant",
             "message": {
                 "model": "claude-opus-4-6",
                 "content": [{"type": "text", "text": "answer"}],
                 "usage": {"input_tokens": 100, "output_tokens": 50},
                 "stop_reason": "tool_use",
             },
             "timestamp": T_resp},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        )
        s = parse_session(f)
        # Interrupt has empty snippet → last_message_role stays "assistant"
        assert s.last_message_role == "assistant"
        assert s.turn_complete is True
        assert s.last_user_message_at == T_int

    def test_interrupt_does_not_set_last_message_text(self, tmp_path):
        """Interrupt text is excluded from last_message_text."""
        T_user = "2026-03-20T10:00:00Z"
        T_int  = "2026-03-20T10:00:05Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "real question"},
             "timestamp": T_user},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        )
        s = parse_session(f)
        assert s.last_message_text == "real question"   # not the interrupt text

    def test_cli_local_messages_dont_affect_activity(self, tmp_path):
        """CLI-local slash commands must not bump last_activity or last_user_message_at."""
        T_user = "2026-03-20T10:00:00Z"
        T_resp = "2026-03-20T10:01:00Z"
        T_cmd  = "2026-03-20T10:02:00Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "question"},
             "timestamp": T_user},
            {"type": "assistant",
             "message": {
                 "model": "claude-opus-4-6",
                 "content": [{"type": "text", "text": "done"}],
                 "usage": {"input_tokens": 100, "output_tokens": 50},
                 "stop_reason": "end_turn",
             },
             "timestamp": T_resp},
            {"type": "user",
             "message": {"content": "<command-name>/model</command-name>"},
             "timestamp": T_cmd,
             "isMeta": False},
        )
        s = parse_session(f)
        # CLI-local slash command at T_cmd must not change last_activity
        assert s.last_activity == T_resp
        # Must not affect last_user_message_at
        assert s.last_user_message_at == T_user
        # Must not affect turn_complete (turn was done at T_resp)
        assert s.turn_complete is False  # user msg reset it, then CLI-local preserved it

    def test_turn_complete_via_turn_duration(self, tmp_path):
        """system:turn_duration is the canonical end-of-turn signal."""
        T_user = "2026-03-20T10:00:00Z"
        T_resp = "2026-03-20T10:01:00Z"
        T_dur  = "2026-03-20T10:01:01Z"
        f = _jsonl(tmp_path,
            {"type": "user",
             "message": {"content": "question"},
             "timestamp": T_user},
            {"type": "assistant",
             "message": {
                 "model": "claude-opus-4-6",
                 "content": [{"type": "text", "text": "answer"}],
                 "usage": {"input_tokens": 100, "output_tokens": 50},
                 "stop_reason": "tool_use",
             },
             "timestamp": T_resp},
            {"type": "system", "subtype": "turn_duration", "durationMs": 1000,
             "timestamp": T_dur},
        )
        s = parse_session(f)
        assert s.turn_complete is True
        assert s.total_work_ms == 1000
        # turn_duration has a timestamp that updates last_activity
        assert s.last_activity == T_dur


# ── refresh_session_tail: same guarantees ────────────────────────────

class TestRefreshTailInterrupt:
    """Tail-read path must have the same interrupt behavior as full parse."""

    def _write(self, path, lines):
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def test_interrupt_sets_turn_complete(self, tmp_path):
        T_int = "2026-03-20T10:01:05Z"
        f = tmp_path / "s.jsonl"
        self._write(f, [
            {"type": "user", "message": {"content": "go"},
             "timestamp": "2026-03-20T10:00:00Z"},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="", last_activity="")
        refresh_session_tail(s)
        assert s.turn_complete is True
        assert s.last_user_message_at == T_int

    def test_interrupt_does_not_override_assistant_role(self, tmp_path):
        """After assistant message, interrupt must not change last_message_role."""
        T_resp = "2026-03-20T10:01:00Z"
        T_int  = "2026-03-20T10:01:05Z"
        f = tmp_path / "s.jsonl"
        self._write(f, [
            {"type": "assistant",
             "message": {
                 "content": [{"type": "text", "text": "done"}],
                 "usage": {}, "stop_reason": "tool_use",
             },
             "timestamp": T_resp},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="", last_activity="")
        refresh_session_tail(s)
        assert s.last_message_role == "assistant"
        assert s.turn_complete is True

    def test_interrupt_bumps_last_user_message_at(self, tmp_path):
        T_user = "2026-03-20T10:00:00Z"
        T_int  = "2026-03-20T10:00:05Z"
        f = tmp_path / "s.jsonl"
        self._write(f, [
            {"type": "user", "message": {"content": "go"}, "timestamp": T_user},
            {"type": "user",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
             "timestamp": T_int},
        ])
        s = ClaudeSession(session_id="s", project_dir="d", project_path="/p",
                          jsonl_path=str(f), last_message_role="", last_activity="")
        refresh_session_tail(s)
        assert s.last_user_message_at == T_int


# ── Integration: activity + seen = correct color ──────────────────────

class TestGreenLogic:
    """Green = AWAITING_INPUT or RESPONSE_READY AND not seen.
    These tests verify the combined activity + seen decision."""

    T_OLD = "2026-03-20T09:00:00Z"
    T_INT = "2026-03-20T10:00:00Z"

    def _is_green(self, session, last_seen):
        """Returns True if the session would render green."""
        from rendering import _is_session_seen
        act = session_activity(session)
        pending = {ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY}
        if act not in pending:
            return False
        return not _is_session_seen(session, last_seen)

    def test_awaiting_input_not_seen_is_green(self):
        """Claude responded, user hasn't looked → green."""
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="assistant",
            turn_complete=True,
            last_activity=self.T_INT,
            last_user_message_at=self.T_OLD,
        )
        assert self._is_green(s, {"sid": self.T_OLD}) is True

    def test_awaiting_input_seen_is_not_green(self):
        """Claude responded, user has seen it → dim."""
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="assistant",
            turn_complete=True,
            last_activity=self.T_OLD,
            last_user_message_at="2026-03-20T08:00:00Z",
        )
        assert self._is_green(s, {"sid": self.T_INT}) is False

    def test_interrupted_before_claude_responded_is_not_green(self):
        """c3c69fd6: user msg + interrupt, no Claude response → not green."""
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="user",
            turn_complete=True,
            last_activity=self.T_INT,          # interrupt updated this
            last_user_message_at=self.T_INT,   # same
        )
        # seen_ts is before the interrupt — should still not be green
        assert self._is_green(s, {"sid": self.T_OLD}) is False

    def test_interrupted_after_claude_responded_is_not_green(self):
        """Claude responded, user interrupted next request → not green."""
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="assistant",     # Claude's response was last real snippet
            turn_complete=True,
            last_activity=self.T_INT,          # interrupt updated this
            last_user_message_at=self.T_INT,   # same (interrupt)
        )
        assert self._is_green(s, {"sid": self.T_OLD}) is False

    def test_response_ready_not_seen_is_green(self):
        """Non-live session, Claude's last word → green."""
        s = _session(
            session_id="sid",
            is_live=False,
            message_count=2,
            last_message_role="assistant",
            last_activity=self.T_INT,
            last_user_message_at=self.T_OLD,
        )
        assert self._is_green(s, {"sid": self.T_OLD}) is True

    def test_thinking_is_never_green(self):
        """Claude is running → blue, not green."""
        s = _session(
            session_id="sid",
            is_live=True,
            last_message_role="user",
            turn_complete=False,
            last_activity=self.T_INT,
            last_user_message_at=self.T_INT,
        )
        assert self._is_green(s, {"sid": self.T_OLD}) is False

    def test_idle_is_never_green(self):
        """Non-live, user last spoke → dim."""
        s = _session(
            session_id="sid",
            is_live=False,
            message_count=1,
            last_message_role="user",
            last_activity=self.T_INT,
            last_user_message_at=self.T_INT,
        )
        assert self._is_green(s, {"sid": self.T_OLD}) is False

    def test_no_messages_idle_is_never_green(self):
        s = _session(session_id="sid", is_live=False, message_count=0)
        assert self._is_green(s, {"sid": self.T_INT}) is False
