"""Tests for cleanup.py — idle orch-session detection & kill."""

import time
from pathlib import Path

import pytest

from cleanup import (
    DEFAULT_IDLE_HOURS,
    _build_jsonl_index,
    _idle_hours_from_env,
    cleanup_idle_orch_sessions,
    find_idle_orch_sessions,
)


# ─── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def projects(tmp_path: Path):
    """Build a fake ~/.claude/projects/ tree.  Returns (root, helper)."""
    root = tmp_path / "projects"
    root.mkdir()

    def add(project: str, session_id: str, age_hours: float) -> Path:
        p = root / project
        p.mkdir(exist_ok=True)
        f = p / f"{session_id}.jsonl"
        f.write_text("{}\n")
        ts = time.time() - age_hours * 3600
        import os
        os.utime(f, (ts, ts))
        return f

    return root, add


# ─── _build_jsonl_index ───────────────────────────────────────────────

def test_jsonl_index_empty_dir(tmp_path):
    assert _build_jsonl_index(tmp_path / "missing") == {}


def test_jsonl_index_picks_latest_mtime_across_projects(projects):
    root, add = projects
    add("proj-a", "abc", age_hours=10)
    add("proj-b", "abc", age_hours=2)  # newer copy
    add("proj-a", "xyz", age_hours=5)

    idx = _build_jsonl_index(root)
    assert set(idx) == {"abc", "xyz"}
    # The 2h-old copy of "abc" wins (most recent)
    now = time.time()
    assert (now - idx["abc"]) / 3600 == pytest.approx(2.0, abs=0.1)


def test_jsonl_index_skips_non_jsonl(projects):
    root, _add = projects
    (root / "p1").mkdir()
    (root / "p1" / "stuff.txt").write_text("nope")
    assert _build_jsonl_index(root) == {}


# ─── find_idle_orch_sessions ──────────────────────────────────────────

def _stub_attached(names=()):
    s = set(names)
    return lambda: s


def _stub_list(names):
    return lambda: list(names)


def test_disabled_when_idle_hours_zero(projects):
    root, add = projects
    add("p", "old", age_hours=100)
    out = find_idle_orch_sessions(
        0,
        projects_dir=root,
        list_sessions=_stub_list(["old"]),
        attached_fn=_stub_attached(),
    )
    assert out == []


def test_finds_only_sessions_older_than_threshold(projects):
    root, add = projects
    add("p", "fresh", age_hours=1)
    add("p", "stale", age_hours=8)
    add("p", "ancient", age_hours=72)

    out = find_idle_orch_sessions(
        6,
        projects_dir=root,
        list_sessions=_stub_list(["fresh", "stale", "ancient"]),
        attached_fn=_stub_attached(),
    )
    names = [n for n, _ in out]
    # Sorted oldest-first
    assert names == ["ancient", "stale"]


def test_skips_attached_sessions(projects):
    root, add = projects
    add("p", "stale", age_hours=24)

    out = find_idle_orch_sessions(
        6,
        projects_dir=root,
        list_sessions=_stub_list(["stale"]),
        attached_fn=_stub_attached(["stale"]),
    )
    assert out == []


def test_skips_sessions_without_jsonl(projects):
    root, _add = projects
    out = find_idle_orch_sessions(
        6,
        projects_dir=root,
        list_sessions=_stub_list(["unknown-uuid"]),
        attached_fn=_stub_attached(),
    )
    assert out == []


# ─── cleanup_idle_orch_sessions ───────────────────────────────────────

def test_dry_run_does_not_call_kill(projects):
    root, add = projects
    add("p", "stale", age_hours=24)

    killed_names: list[str] = []

    def kill(name):
        killed_names.append(name)
        return True

    result = cleanup_idle_orch_sessions(
        6,
        dry_run=True,
        projects_dir=root,
        list_sessions=_stub_list(["stale"]),
        attached_fn=_stub_attached(),
        kill_fn=kill,
    )
    assert killed_names == []
    assert [n for n, _ in result] == ["stale"]


def test_kill_failure_excluded_from_result(projects):
    root, add = projects
    add("p", "stale-a", age_hours=24)
    add("p", "stale-b", age_hours=12)

    def kill(name):
        return name == "stale-a"  # b fails

    result = cleanup_idle_orch_sessions(
        6,
        projects_dir=root,
        list_sessions=_stub_list(["stale-a", "stale-b"]),
        attached_fn=_stub_attached(),
        kill_fn=kill,
    )
    assert [n for n, _ in result] == ["stale-a"]


# ─── _idle_hours_from_env ─────────────────────────────────────────────

def test_idle_hours_default(monkeypatch):
    monkeypatch.delenv("ORCH_AUTO_CLEANUP_HOURS", raising=False)
    assert _idle_hours_from_env() == DEFAULT_IDLE_HOURS


def test_idle_hours_from_env_value(monkeypatch):
    monkeypatch.setenv("ORCH_AUTO_CLEANUP_HOURS", "3.5")
    assert _idle_hours_from_env() == 3.5


def test_idle_hours_zero_disables(monkeypatch):
    monkeypatch.setenv("ORCH_AUTO_CLEANUP_HOURS", "0")
    assert _idle_hours_from_env() == 0


def test_idle_hours_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ORCH_AUTO_CLEANUP_HOURS", "garbage")
    assert _idle_hours_from_env() == DEFAULT_IDLE_HOURS
