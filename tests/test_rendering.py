"""Tests for rendering.py — markup helpers, color functions, display formatting."""

import pytest
from datetime import datetime, timedelta, timezone
from models import Category, Status, Workstream
from sessions import ClaudeSession
from threads import ThreadActivity
from notifications import Notification
from rendering import (
    _token_color, _colored_tokens, _token_color_markup,
    _status_markup, _category_markup,
    _ws_indicators, _short_project, _short_model,
    _activity_icon, _activity_badge, _best_activity,
    _render_notification_option,
    _parse_worktree_display, _worktree_color, _WORKTREE_COLORS,
    C_DIM, C_GREEN, C_ORANGE, C_RED, C_LIGHT,
)


class TestTokenColor:
    def test_small_tokens(self):
        assert _token_color(100) == C_DIM

    def test_medium_tokens(self):
        assert _token_color(500_000) == C_LIGHT

    def test_large_tokens(self):
        assert _token_color(5_000_000) == C_ORANGE

    def test_huge_tokens(self):
        assert _token_color(50_000_000) == C_RED

    def test_token_color_markup(self):
        result = _token_color_markup("1.5M", 1_500_000)
        assert "1.5M" in result
        assert C_ORANGE in result


class TestActivityIcons:
    def test_thinking_icon(self):
        icon = _activity_icon(ThreadActivity.THINKING, 0)
        assert "◉" in icon  # Static thinking indicator

    def test_awaiting_input(self):
        icon = _activity_icon(ThreadActivity.AWAITING_INPUT)
        assert "●" in icon

    def test_idle(self):
        icon = _activity_icon(ThreadActivity.IDLE)
        assert "·" in icon


class TestActivityBadge:
    def test_thinking_badge(self):
        badge = _activity_badge(ThreadActivity.THINKING)
        assert "thinking" in badge

    def test_awaiting_badge(self):
        badge = _activity_badge(ThreadActivity.AWAITING_INPUT)
        assert "your turn" in badge

    def test_idle_badge_empty(self):
        assert _activity_badge(ThreadActivity.IDLE) == ""


class TestBestActivity:
    def test_empty_is_idle(self):
        assert _best_activity([]) == ThreadActivity.IDLE


class TestWorktreeDisplay:
    def test_parse_ticket_branch(self):
        repo, display = _parse_worktree_display("ul.UB-6668-implement-new-metric")
        assert repo == "ul"
        assert display == "UB-6668"

    def test_parse_plain_branch(self):
        repo, display = _parse_worktree_display("ul.feature-branch")
        assert repo == "ul"
        assert display == "feature-branch"

    def test_parse_no_dot(self):
        repo, display = _parse_worktree_display("claude-orchestrator")
        assert repo == "claude-orchestrator"
        assert display == "claude-orchestrator"

    def test_color_consistent(self):
        c1 = _worktree_color("ul.UB-6668-something")
        c2 = _worktree_color("ul.UB-6668-something")
        assert c1 == c2
        assert c1 in _WORKTREE_COLORS

    def test_color_varies(self):
        colors = {_worktree_color(f"repo-{i}") for i in range(20)}
        assert len(colors) > 1


class TestRenderNotificationOption:
    def _notif(self, minutes_ago=5, dismissed=False, message="Fixed the parser"):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return Notification(id="x", timestamp=ts, cwd="/foo", title="project",
                            message=message, dismissed=dismissed)

    def test_fresh_uses_green(self):
        result = _render_notification_option(self._notif(minutes_ago=5))
        assert C_GREEN in result
        assert "●" in result

    def test_recent_uses_orange(self):
        result = _render_notification_option(self._notif(minutes_ago=120))
        assert C_ORANGE in result

    def test_dismissed_uses_dim(self):
        result = _render_notification_option(self._notif(dismissed=True))
        assert "·" in result

    def test_truncates_long_message(self):
        result = _render_notification_option(self._notif(message="x" * 100), max_width=20)
        assert "…" in result

    def test_two_lines(self):
        result = _render_notification_option(self._notif())
        assert "\n" in result
        assert "project" in result  # title in second line


