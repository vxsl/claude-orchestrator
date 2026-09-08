"""Tests for thread_namer.py — which sessions still need a model call.

Claude Code titles its own sessions and writes the result into the JSONL
(``ai-title``), so orch's haiku titler should only ever see the sessions
Claude left untitled.
"""

import pytest

import thread_namer
from sessions import ClaudeSession


def _session(sid: str, **kw) -> ClaudeSession:
    return ClaudeSession(session_id=sid, project_dir="d", project_path="/p", **kw)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """Any model call in these tests is the bug under test."""
    def boom(*a, **k):
        raise AssertionError("title_sessions shelled out to a model")
    monkeypatch.setattr(thread_namer.subprocess, "run", boom)


class TestTitleSessions:
    def test_claude_titled_sessions_need_no_model_call(self, monkeypatch):
        monkeypatch.setattr(thread_namer, "_load_session_cache", lambda: {})
        sessions = [
            _session("a", ai_title="Debug the socket timeout"),
            _session("b", ai_title="Bus stop streams worldgraph query"),
        ]
        assert thread_namer.title_sessions(sessions) == {
            "a": "Debug the socket timeout",
            "b": "Bus stop streams worldgraph query",
        }

    def test_claude_title_beats_our_cached_one(self, monkeypatch):
        """Ours is made from the first 200 characters; Claude's from the lot."""
        monkeypatch.setattr(thread_namer, "_load_session_cache",
                            lambda: {"a": "web client work"})
        s = _session("a", ai_title="Add CI gate for test coverage in web client")
        titles = thread_namer.title_sessions([s])
        assert titles["a"] == "Add CI gate for test coverage in web client"

    def test_cache_still_answers_for_untitled_sessions(self, monkeypatch):
        monkeypatch.setattr(thread_namer, "_load_session_cache",
                            lambda: {"a": "metrics.toml cleanup"})
        titles = thread_namer.title_sessions([_session("a")])
        assert titles == {"a": "metrics.toml cleanup"}


class TestGetSessionTitle:
    def test_prefers_claudes_own_title(self, monkeypatch):
        monkeypatch.setattr(thread_namer, "_load_session_cache",
                            lambda: {"a": "web client work"})
        s = _session("a", ai_title="Determine backend codeowners")
        assert thread_namer.get_session_title(s) == "Determine backend codeowners"

    def test_falls_back_to_the_cache(self, monkeypatch):
        monkeypatch.setattr(thread_namer, "_load_session_cache",
                            lambda: {"a": "metrics.toml cleanup"})
        assert thread_namer.get_session_title(_session("a")) == "metrics.toml cleanup"

    def test_untitled_session_is_empty(self, monkeypatch):
        monkeypatch.setattr(thread_namer, "_load_session_cache", lambda: {})
        assert thread_namer.get_session_title(_session("a")) == ""
