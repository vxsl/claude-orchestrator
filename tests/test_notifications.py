"""Tests for notifications.py — load, filter, dismiss notifications."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from notifications import (
    Notification, _notif_id,
    load_notifications, notifications_for_dirs,
    dismiss_notification, undismiss_notification, dismiss_all_for_dirs,
    _load_dismissed, _save_dismissed,
    FRESH_THRESHOLD, RECENT_THRESHOLD,
)


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
