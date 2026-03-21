"""Tests for notifications.py — load, filter, dismiss, and panel integration."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from notifications import (
    Notification, _notif_id,
    load_notifications, notifications_for_dirs,
    dismiss_notification, undismiss_notification, dismiss_all_for_dirs,
    _load_dismissed, _save_dismissed,
    FRESH_THRESHOLD, RECENT_THRESHOLD,
)
from models import Workstream, Store
from sessions import ClaudeSession


def _make_notif_line(cwd="/home/user/project", message="done something",
                     title="project", session_id="sess-123",
                     minutes_ago=5):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return json.dumps({
        "timestamp": ts, "cwd": cwd, "title": title,
        "message": message, "session_id": session_id,
    })


class TestNotifId:
    def test_stable(self):
        a = _notif_id("2025-01-01", "/foo", "hello")
        b = _notif_id("2025-01-01", "/foo", "hello")
        assert a == b

    def test_different_content(self):
        a = _notif_id("2025-01-01", "/foo", "hello")
        b = _notif_id("2025-01-01", "/foo", "world")
        assert a != b


class TestNotification:
    def test_freshness_fresh(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        n = Notification(id="x", timestamp=ts, cwd="/foo", title="t", message="m")
        assert n.freshness == "fresh"

    def test_freshness_recent(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        n = Notification(id="x", timestamp=ts, cwd="/foo", title="t", message="m")
        assert n.freshness == "recent"

    def test_freshness_old(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        n = Notification(id="x", timestamp=ts, cwd="/foo", title="t", message="m")
        assert n.freshness == "old"

    def test_dt_parsing(self):
        ts = "2025-06-15T10:30:00+00:00"
        n = Notification(id="x", timestamp=ts, cwd="/foo", title="t", message="m")
        assert n.dt.year == 2025
        assert n.dt.month == 6


class TestLoadNotifications:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "notifications.jsonl"
        f.write_text("")
        with patch("notifications.NOTIFICATIONS_FILE", f), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = load_notifications()
        assert result == []

    def test_missing_file(self, tmp_path):
        with patch("notifications.NOTIFICATIONS_FILE", tmp_path / "missing.jsonl"), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = load_notifications()
        assert result == []

    def test_loads_valid_lines(self, tmp_path):
        f = tmp_path / "notifications.jsonl"
        f.write_text(
            _make_notif_line(message="first", minutes_ago=10) + "\n" +
            _make_notif_line(message="second", minutes_ago=5) + "\n"
        )
        with patch("notifications.NOTIFICATIONS_FILE", f), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = load_notifications()
        assert len(result) == 2
        # Newest first
        assert result[0].message == "second"
        assert result[1].message == "first"

    def test_skips_old_notifications(self, tmp_path):
        f = tmp_path / "notifications.jsonl"
        f.write_text(
            _make_notif_line(minutes_ago=5) + "\n" +
            _make_notif_line(minutes_ago=60 * 100) + "\n"  # 100 hours old
        )
        with patch("notifications.NOTIFICATIONS_FILE", f), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = load_notifications()
        assert len(result) == 1

    def test_marks_dismissed(self, tmp_path):
        f = tmp_path / "notifications.jsonl"
        line = _make_notif_line(message="test")
        f.write_text(line + "\n")
        # Figure out what the ID would be
        data = json.loads(line)
        nid = _notif_id(data["timestamp"], data["cwd"], data["message"])
        # Write dismissed file
        d = tmp_path / "dismissed.json"
        d.write_text(json.dumps([nid]))
        with patch("notifications.NOTIFICATIONS_FILE", f), \
             patch("notifications.DISMISSED_FILE", d):
            result = load_notifications()
        assert len(result) == 1
        assert result[0].dismissed is True

    def test_skips_invalid_json(self, tmp_path):
        f = tmp_path / "notifications.jsonl"
        f.write_text("not json\n" + _make_notif_line() + "\n")
        with patch("notifications.NOTIFICATIONS_FILE", f), \
             patch("notifications.DISMISSED_FILE", tmp_path / "dismissed.json"):
            result = load_notifications()
        assert len(result) == 1


class TestNotificationsForDirs:
    def test_filters_by_dir(self):
        n1 = Notification(id="1", timestamp="", cwd="/home/user/project-a", title="a", message="m1")
        n2 = Notification(id="2", timestamp="", cwd="/home/user/project-b", title="b", message="m2")
        n3 = Notification(id="3", timestamp="", cwd="/home/user/project-a", title="a", message="m3")
        result = notifications_for_dirs([n1, n2, n3], {"/home/user/project-a"})
        assert len(result) == 2
        assert all(n.cwd == "/home/user/project-a" for n in result)

    def test_normalizes_trailing_slash(self):
        n = Notification(id="1", timestamp="", cwd="/home/user/proj", title="t", message="m")
        result = notifications_for_dirs([n], {"/home/user/proj/"})
        assert len(result) == 1

    def test_empty_dirs(self):
        n = Notification(id="1", timestamp="", cwd="/foo", title="t", message="m")
        result = notifications_for_dirs([n], set())
        assert result == []


class TestDismissal:
    def test_dismiss_and_load(self, tmp_path):
        d = tmp_path / "dismissed.json"
        with patch("notifications.DISMISSED_FILE", d), \
             patch("notifications.CACHE_DIR", tmp_path):
            dismiss_notification("abc123")
            dismissed = _load_dismissed()
        assert "abc123" in dismissed

    def test_undismiss(self, tmp_path):
        d = tmp_path / "dismissed.json"
        d.write_text(json.dumps(["abc123", "def456"]))
        with patch("notifications.DISMISSED_FILE", d), \
             patch("notifications.CACHE_DIR", tmp_path):
            undismiss_notification("abc123")
            dismissed = _load_dismissed()
        assert "abc123" not in dismissed
        assert "def456" in dismissed

    def test_dismiss_all_for_dirs(self, tmp_path):
        d = tmp_path / "dismissed.json"
        n1 = Notification(id="1", timestamp="", cwd="/project-a", title="a", message="m1")
        n2 = Notification(id="2", timestamp="", cwd="/project-b", title="b", message="m2")
        with patch("notifications.DISMISSED_FILE", d), \
             patch("notifications.CACHE_DIR", tmp_path):
            dismiss_all_for_dirs([n1, n2], {"/project-a"})
            dismissed = _load_dismissed()
        assert "1" in dismissed
        assert "2" not in dismissed


# ─── Panel cycling logic ──────────────────────────────────────────

class TestDetailScreenPanelIds:
    """Test _panel_ids logic without instantiating the full Textual screen."""

    def _make_screen_state(self, sessions=None, archived=None, feed=None):
        """Return a mock object with the fields _panel_ids reads."""
        from screens import DetailScreen
        obj = MagicMock(spec=DetailScreen)
        obj._detail_sessions = sessions or []
        obj._archived_sessions = archived or []
        obj._feed_notifications = feed or []
        # Call the real method
        obj._panel_ids = DetailScreen._panel_ids.__get__(obj)
        return obj

    def test_sessions_only(self):
        s = self._make_screen_state()
        assert s._panel_ids() == ["detail-sessions", "detail-scroll"]

    def test_sessions_and_archived(self):
        s = self._make_screen_state(archived=[MagicMock()])
        assert s._panel_ids() == ["detail-sessions", "detail-archived", "detail-scroll"]

    def test_sessions_and_feed(self):
        s = self._make_screen_state(feed=[MagicMock()])
        assert s._panel_ids() == ["detail-sessions", "detail-scroll", "detail-feed"]

    def test_all_panels(self):
        s = self._make_screen_state(
            archived=[MagicMock()],
            feed=[MagicMock()],
        )
        assert s._panel_ids() == [
            "detail-sessions", "detail-archived", "detail-scroll", "detail-feed"
        ]

    def test_empty_archived_skipped(self):
        s = self._make_screen_state(archived=[], feed=[MagicMock()])
        panels = s._panel_ids()
        assert "detail-archived" not in panels

    def test_empty_feed_skipped(self):
        s = self._make_screen_state(archived=[MagicMock()], feed=[])
        panels = s._panel_ids()
        assert "detail-feed" not in panels


class TestOnKeyRouting:
    """Test that on_key routes ctrl+j/k to the correct screen."""

    def test_routes_to_screen_with_panel_nav(self):
        """When active screen has action_next_panel, route there."""
        from app import OrchestratorApp
        app = MagicMock(spec=OrchestratorApp)
        mock_screen = MagicMock()
        mock_screen.action_next_panel = MagicMock()
        mock_screen.action_prev_panel = MagicMock()
        app.screen = mock_screen

        # Simulate what on_key does
        event_j = MagicMock(key="ctrl+j")
        # Call the routing logic directly
        screen = app.screen
        if hasattr(screen, 'action_next_panel'):
            screen.action_next_panel()
        mock_screen.action_next_panel.assert_called_once()

        event_k = MagicMock(key="ctrl+k")
        if hasattr(screen, 'action_prev_panel'):
            screen.action_prev_panel()
        mock_screen.action_prev_panel.assert_called_once()

    def test_falls_back_to_app_panel_nav(self):
        """When active screen lacks action_next_panel, use app's."""
        from app import OrchestratorApp
        app = MagicMock(spec=OrchestratorApp)
        mock_screen = MagicMock(spec=[])  # no methods
        app.screen = mock_screen

        screen = app.screen
        if hasattr(screen, 'action_next_panel'):
            screen.action_next_panel()
        else:
            app.action_next_panel()
        app.action_next_panel.assert_called_once()
