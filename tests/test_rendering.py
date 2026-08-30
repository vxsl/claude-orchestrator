"""Tests for rendering.py — markup helpers, color functions, display formatting."""

import os

import pytest
from datetime import datetime, timedelta, timezone
from models import Category, TodoItem, Workstream
from sessions import ClaudeSession
from threads import ThreadActivity
from notifications import Notification
from rendering import (
    _token_color, _colored_tokens, _token_color_markup,
    _category_markup,
    _ws_indicators, _short_project, _short_model,
    _activity_icon, _activity_badge, _best_activity,
    _render_notification_option,
    _parse_worktree_display, _worktree_color, _WORKTREE_COLORS,
    C_DIM, C_GREEN, C_ORANGE, C_RED, C_LIGHT, C_BLUE, C_YELLOW,
    _compact_age, auto_status,
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




class TestCommitLineMarkup:
    """A committed session's commit summary can be multi-line (subject +
    diffstat). Each rendered line must be valid, balanced Rich markup —
    a raw newline used to split the [color]…[/color] span across physical
    lines, orphaning the tags and crashing the DetailView painter."""

    def _committed_session(self, summary: str) -> ClaudeSession:
        old = (datetime.now(timezone.utc) - timedelta(days=19)).isoformat()
        return ClaudeSession(
            session_id="0b5eb18d-aaaa", project_dir="d",
            project_path="/home/kyle/work/repos/x/client/web",
            message_count=10, model="opus",
            started_at=old, last_activity=old,
            last_commit_sha="6b9810babc123",
            last_commit_summary=summary,
        )

    @staticmethod
    def _assert_all_lines_valid_markup(markup: str) -> None:
        from rich.console import Console
        console = Console()
        for lineno, line in enumerate(str(markup).split("\n")):
            console.render_str(line, emoji=False, highlight=False)  # raises on bad markup

    def test_multiline_commit_summary_renders_balanced(self):
        from rendering import _render_session_option
        s = self._committed_session(
            "right-align HeatmapTable cell values\n 1 file changed, 12 insertions(+)")
        out = _render_session_option(
            s, ThreadActivity.AWAITING_INPUT, 0,
            ws_repo_path=None, seen=True, line_width=60)
        self._assert_all_lines_valid_markup(out)
        # the summary stays on one physical line (newline flattened to a
        # space), so text after the newline merges in rather than spilling
        # onto a new line with an orphaned closing tag.
        commit_lines = [ln for ln in str(out).split("\n") if "6b9810b" in ln]
        assert len(commit_lines) == 1
        assert "1 file" in commit_lines[0]

    def test_commit_summary_with_brackets_renders_balanced(self):
        from rendering import _render_session_option
        s = self._committed_session("fix [P5] bug\n\nbody with [brackets] and stat")
        out = _render_session_option(
            s, ThreadActivity.AWAITING_INPUT, 0,
            ws_repo_path=None, seen=True, line_width=60)
        self._assert_all_lines_valid_markup(out)


class TestAutoStatus:
    """The loop's coordinator pane is idle by design for as long as an
    implementer runs, so "working" and "dead" looked identical. This
    names the two waits apart."""

    def _running_ws(self, **kw):
        ws = Workstream(name="w")
        ws.auto_running = True
        ws.auto_pid = os.getpid()  # alive
        ws.auto_iteration = 8
        for k, v in kw.items():
            setattr(ws, k, v)
        return ws

    def test_silent_when_not_running(self):
        assert auto_status(Workstream(name="w")) == ("", "")

    def test_awaiting_coordinator_with_no_implementer(self):
        text, color = auto_status(self._running_ws())
        assert text == "auto iter 8 · awaiting coordinator"
        assert color == C_BLUE

    def test_names_the_running_implementer_and_its_age(self):
        ws = self._running_ws()
        started = (datetime.now() - timedelta(minutes=42)).isoformat()
        ws.todos = [TodoItem(text="t", impl_sid="sid", impl_started_at=started)]
        text, _ = auto_status(ws)
        assert text == "auto iter 8 · impl 42m"

    def test_counts_a_concurrent_batch_and_uses_the_oldest(self):
        ws = self._running_ws()
        ws.todos = [
            TodoItem(text="a", impl_sid="s1",
                     impl_started_at=(datetime.now() - timedelta(minutes=5)).isoformat()),
            TodoItem(text="b", impl_sid="s2",
                     impl_started_at=(datetime.now() - timedelta(hours=2)).isoformat()),
        ]
        text, _ = auto_status(ws)
        # The loop is blocked until the slowest lands, so that is the wait.
        assert text == "auto iter 8 · 2 impl 2h"

    def test_finished_todos_are_not_in_flight(self):
        ws = self._running_ws()
        started = (datetime.now() - timedelta(minutes=42)).isoformat()
        ws.todos = [
            TodoItem(text="a", impl_sid="s1", impl_started_at=started, done=True),
            TodoItem(text="b", impl_sid="s2", impl_started_at=started, archived=True),
        ]
        assert auto_status(ws)[0] == "auto iter 8 · awaiting coordinator"

    def test_dead_owner_reads_as_stale(self):
        ws = self._running_ws(auto_pid=999_999_999)
        text, color = auto_status(ws)
        assert "stale" in text
        assert color == C_RED

    def test_quota_park_wins_over_the_wait(self):
        ws = self._running_ws(auto_paused=True)
        ws.auto_resume_at = (datetime.now() + timedelta(hours=2)).isoformat()
        text, color = auto_status(ws)
        assert text.startswith("auto paused")
        assert color == C_YELLOW


class TestCompactAge:
    def test_minutes_hours_days(self):
        now = datetime.now()
        assert _compact_age((now - timedelta(minutes=42)).isoformat()) == "42m"
        assert _compact_age((now - timedelta(hours=3)).isoformat()) == "3h"
        assert _compact_age((now - timedelta(days=2)).isoformat()) == "2d"

    def test_unparseable_is_empty(self):
        assert _compact_age("") == ""
        assert _compact_age("not-a-date") == ""

    def test_future_clamps_to_zero(self):
        future = (datetime.now() + timedelta(minutes=5)).isoformat()
        assert _compact_age(future) == "0m"
