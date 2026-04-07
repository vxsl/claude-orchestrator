"""Tests for brain.py — stream-of-consciousness parser."""

import pytest

from brain import (
    parse_brain_dump,
    brain_dump_to_workstreams,
    _split_text,
    _clean_fragment,
    _extract_name,
    _detect_category,
)
from models import Category


# ─── Text Splitting ─────────────────────────────────────────────────

class TestSplitText:
    def test_comma_separation(self):
        parts = _split_text("fix the auth bug, review the PR, deploy staging")
        assert len(parts) >= 2

    def test_semicolon_separation(self):
        parts = _split_text("fix auth; review PR; deploy")
        assert len(parts) == 3

    def test_also_splitting(self):
        parts = _split_text("fix the bug also review the PR")
        assert len(parts) >= 2

    def test_and_then_splitting(self):
        parts = _split_text("fix the bug and then deploy it")
        assert len(parts) >= 2

    def test_numbered_list(self):
        parts = _split_text("1. fix auth 2. review PR 3. deploy")
        assert len(parts) == 3

    def test_bullet_points(self):
        parts = _split_text("- fix auth\n- review PR\n- deploy")
        assert len(parts) == 3

    def test_newline_separation(self):
        parts = _split_text("fix the auth bug\nreview the PR\ndeploy to staging")
        assert len(parts) == 3

    def test_single_item(self):
        parts = _split_text("fix the auth bug")
        assert len(parts) == 1

    def test_empty_string(self):
        parts = _split_text("")
        assert len(parts) == 1  # returns [""]

    def test_dont_forget(self):
        parts = _split_text("fix the bug, don't forget to update docs")
        assert len(parts) >= 2


# ─── Category Detection ─────────────────────────────────────────────

class TestDetectCategory:
    def test_work_ticket(self):
        # \bUB-\d+\b needs the ticket format embedded naturally
        assert _detect_category("fix ticket UB-1234 for the deploy") == Category.WORK

    def test_work_deploy(self):
        assert _detect_category("deploy to staging") == Category.WORK

    def test_work_pipeline(self):
        assert _detect_category("fix the CI pipeline") == Category.WORK

    def test_work_bug(self):
        assert _detect_category("fix the auth bug") == Category.WORK

    def test_default_personal(self):
        assert _detect_category("organize my notes") == Category.PERSONAL


# ─── Name Extraction ────────────────────────────────────────────────

class TestExtractName:
    def test_short_text_preserved(self):
        assert _extract_name("fix the auth bug") == "fix the auth bug"

    def test_long_text_truncated(self):
        long_text = "fix the authentication system that has been broken for weeks because of the migration"
        name = _extract_name(long_text)
        assert len(name) <= 50

    def test_cleans_filler(self):
        name = _extract_name("I need to fix the auth bug")
        assert "I need to" not in name

    def test_removes_trailing_punctuation(self):
        cleaned = _clean_fragment("fix the bug,")
        assert not cleaned.endswith(",")


# ─── Full Parse ─────────────────────────────────────────────────────

class TestParseBrainDump:
    def test_simple_comma_list(self):
        tasks = parse_brain_dump("fix auth bug, also review Logan's MR, deploy staging")
        assert len(tasks) >= 2

    def test_category_inference(self):
        tasks = parse_brain_dump("deploy the staging pipeline, also update my dotfiles config")
        categories = {t.category for t in tasks}
        assert Category.WORK in categories

    def test_empty_input(self):
        assert parse_brain_dump("") == []

    def test_whitespace_only(self):
        assert parse_brain_dump("   ") == []

    def test_single_task(self):
        tasks = parse_brain_dump("fix the authentication bug")
        assert len(tasks) == 1
        assert tasks[0].name

    def test_names_are_reasonable(self):
        tasks = parse_brain_dump("fix auth bug, review Logan's MR, deploy v2")
        for task in tasks:
            assert len(task.name) > 3
            assert len(task.name) <= 60

    def test_raw_text_preserved(self):
        tasks = parse_brain_dump("fix the authentication bug in the login flow")
        assert len(tasks) >= 1
        assert "auth" in tasks[0].raw_text.lower()


class TestBrainDumpToWorkstreams:
    def test_creates_workstreams(self):
        workstreams = brain_dump_to_workstreams("fix auth, review PR")
        assert len(workstreams) >= 1
        for ws in workstreams:
            assert ws.name
            assert ws.id
            assert ws.created_at
