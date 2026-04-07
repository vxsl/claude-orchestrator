"""Tests for models.py — Workstream, Store, and helpers."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from models import (
    Category, Link, Store, Workstream,
    _relative_time,
)


# ─── Workstream ─────────────────────────────────────────────────────

class TestWorkstreamDefaults:
    def test_has_id(self):
        ws = Workstream(name="test")
        assert len(ws.id) == 8

    def test_default_category(self):
        ws = Workstream(name="test")
        assert ws.category == Category.PERSONAL

    def test_timestamps_set(self):
        ws = Workstream(name="test")
        assert ws.created_at
        assert ws.updated_at

    def test_empty_links_and_notes(self):
        ws = Workstream(name="test")
        assert ws.links == []
        assert ws.notes == ""
        assert ws.archived is False


class TestWorkstreamProperties:
    def test_is_active_not_archived(self):
        ws = Workstream(name="test")
        assert ws.is_active is True

    def test_is_active_archived(self):
        ws = Workstream(name="test", archived=True)
        assert ws.is_active is False

    def test_is_stale_fresh(self):
        ws = Workstream(name="test")
        assert ws.is_stale is False

    def test_is_stale_old(self):
        ws = Workstream(name="test")
        ws.updated_at = (datetime.now() - timedelta(hours=48)).isoformat()
        assert ws.is_stale is True

    def test_age_returns_string(self):
        ws = Workstream(name="test")
        assert isinstance(ws.age, str)

    def test_staleness_returns_string(self):
        ws = Workstream(name="test")
        assert isinstance(ws.staleness, str)


class TestWorkstreamTouch:
    def test_touch_updates_timestamp(self, sample_ws):
        old = sample_ws.updated_at
        sample_ws.touch()
        assert sample_ws.updated_at >= old


class TestWorkstreamLinks:
    def test_add_link(self, sample_ws):
        link = sample_ws.add_link("ticket", "UB-1234", "Ticket")
        assert len(sample_ws.links) == 1
        assert link.kind == "ticket"
        assert link.value == "UB-1234"

    def test_add_link_default_label(self, sample_ws):
        link = sample_ws.add_link("url", "https://example.com")
        assert link.label == "url"

    def test_link_display(self):
        link = Link(kind="ticket", label="UB-1234", value="UB-1234")
        assert "UB-1234" in link.display

    def test_link_is_openable(self):
        assert Link(kind="url", label="l", value="v").is_openable is True
        assert Link(kind="worktree", label="l", value="v").is_openable is True
        assert Link(kind="slack", label="l", value="v").is_openable is False


class TestWorkstreamSerialization:
    def test_round_trip(self, ws_with_links):
        d = ws_with_links.to_dict()
        restored = Workstream.from_dict(d)
        assert restored.name == ws_with_links.name
        assert restored.category == ws_with_links.category
        assert len(restored.links) == len(ws_with_links.links)

    def test_to_dict_serializes_enums(self, sample_ws):
        d = sample_ws.to_dict()
        assert d["category"] == "work"

    def test_from_dict_migration(self):
        """Old data with status field should still load (status is ignored)."""
        d = {
            "id": "test1234",
            "name": "Old workstream",
            "description": "",
            "status": "queued",
            "category": "personal",
            "links": [],
            "notes": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        ws = Workstream.from_dict(d)
        assert ws.archived is False

    def test_from_dict_migration_no_repo_path(self):
        """Old data without repo_path should still load with empty default."""
        d = {
            "id": "test5678",
            "name": "Legacy ws",
            "description": "",
            "status": "queued",
            "category": "personal",
            "links": [],
            "notes": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        ws = Workstream.from_dict(d)
        assert ws.repo_path == ""

    def test_repo_path_roundtrip(self):
        """repo_path survives to_dict/from_dict."""
        ws = Workstream(name="test", repo_path="/home/user/dev/myrepo")
        d = ws.to_dict()
        assert d["repo_path"] == "/home/user/dev/myrepo"
        restored = Workstream.from_dict(d)
        assert restored.repo_path == "/home/user/dev/myrepo"


# ─── Store ──────────────────────────────────────────────────────────

class TestStoreBasicOps:
    def test_add_and_get(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        retrieved = tmp_store.get(sample_ws.id)
        assert retrieved is not None
        assert retrieved.name == sample_ws.name

    def test_get_by_prefix(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        retrieved = tmp_store.get(sample_ws.id[:4])
        assert retrieved is not None
        assert retrieved.id == sample_ws.id

    def test_get_nonexistent(self, tmp_store):
        assert tmp_store.get("nonexistent") is None

    def test_remove(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        tmp_store.remove(sample_ws.id)
        assert tmp_store.get(sample_ws.id) is None

    def test_update(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        sample_ws.name = "Updated name"
        tmp_store.update(sample_ws)
        retrieved = tmp_store.get(sample_ws.id)
        assert retrieved.name == "Updated name"

    def test_persistence(self, tmp_path):
        """Data survives store reload."""
        path = tmp_path / "data.json"
        store1 = Store(path=path)
        ws = Workstream(name="Persistent")
        store1.add(ws)

        store2 = Store(path=path)
        assert len(store2.workstreams) == 1
        assert store2.workstreams[0].name == "Persistent"


class TestStoreFiltering:
    def test_active_excludes_archived(self, populated_store):
        all_count = len(populated_store.workstreams)
        populated_store.workstreams[0].archived = True
        populated_store.save()
        assert len(populated_store.active) == all_count - 1

    def test_by_category(self, populated_store):
        work = populated_store.by_category(Category.WORK)
        assert all(w.category == Category.WORK for w in work)

    def test_search(self, populated_store):
        results = populated_store.search("blocked")
        assert len(results) == 1
        assert results[0].name == "Blocked task"

    def test_search_case_insensitive(self, populated_store):
        results = populated_store.search("PERSONAL")
        assert len(results) == 1

    def test_stale(self, populated_store):
        stale = populated_store.stale(hours=24)
        assert len(stale) >= 1
        assert any(w.name == "Personal project" for w in stale)

    def test_filtered_combined(self, populated_store):
        results = populated_store.filtered(
            category=Category.WORK,
        )
        assert all(w.category == Category.WORK for w in results)


class TestStoreSorting:
    def test_sort_by_name(self, populated_store):
        streams = populated_store.active
        sorted_streams = populated_store.sorted(streams, "name")
        names = [w.name.lower() for w in sorted_streams]
        assert names == sorted(names)

    def test_sort_by_updated(self, populated_store):
        streams = populated_store.active
        sorted_streams = populated_store.sorted(streams, "updated")
        times = [w.updated_at for w in sorted_streams]
        assert times == sorted(times, reverse=True)


class TestStoreArchival:
    def test_archive(self, populated_store):
        ws = populated_store.active[0]
        populated_store.archive(ws.id)
        assert ws.id not in [w.id for w in populated_store.active]

    def test_unarchive(self, populated_store):
        ws = populated_store.active[0]
        populated_store.archive(ws.id)
        populated_store.unarchive(ws.id)
        assert ws.id in [w.id for w in populated_store.active]

    def test_archived_property(self, populated_store):
        ws = populated_store.active[0]
        populated_store.archive(ws.id)
        assert ws.id in [w.id for w in populated_store.archived]


class TestStoreBackup:
    def test_backup_creates_file(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        backup_path = tmp_store.backup()
        assert backup_path.exists()

    def test_backup_limits(self, tmp_store, sample_ws):
        tmp_store.add(sample_ws)
        for _ in range(25):
            tmp_store.backup()
        backup_dir = tmp_store.path.parent / "backups"
        backups = list(backup_dir.glob("data_*.json"))
        assert len(backups) <= 20


# ─── Relative Time ──────────────────────────────────────────────────

class TestRelativeTime:
    def test_just_now(self):
        now = datetime.now().isoformat()
        assert "s ago" in _relative_time(now) or "just now" in _relative_time(now)

    def test_minutes(self):
        t = (datetime.now() - timedelta(minutes=5)).isoformat()
        assert "5m ago" == _relative_time(t)

    def test_hours(self):
        t = (datetime.now() - timedelta(hours=3)).isoformat()
        assert "3h ago" == _relative_time(t)

    def test_days(self):
        t = (datetime.now() - timedelta(days=7)).isoformat()
        assert "7d ago" == _relative_time(t)

    def test_old_date(self):
        result = _relative_time("2024-01-01T00:00:00")
        assert "2024-01-01" in result

    def test_invalid(self):
        assert _relative_time("not-a-date") == "unknown"

    def test_empty(self):
        assert _relative_time("") == "unknown"


# ─── Category Constants ──────────────────────────────────────────────

class TestConstants:
    def test_category_enum_values(self):
        assert Category.WORK.value == "work"
        assert Category.PERSONAL.value == "personal"
