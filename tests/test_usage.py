"""Tests for usage.py — parsing and interpreting Claude subscription limits."""

from datetime import datetime, timedelta, timezone

import pytest

import usage
from usage import (
    QuotaLimit,
    QuotaSnapshot,
    describe_limits,
    format_eta,
    parse_usage_payload,
    read_access_token,
    soonest_reset,
)


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _limit(kind="session", percent=100.0, mins=60, group="session", scope=""):
    return QuotaLimit(
        kind=kind, group=group, percent=percent,
        resets_at=None if mins is None else NOW + timedelta(minutes=mins),
        scope_label=scope,
    )


# ─── payload parsing ─────────────────────────────────────────────────

class TestParsePayload:
    def test_parses_real_shape(self):
        snap = parse_usage_payload({"limits": [
            {"kind": "session", "group": "session", "percent": 70,
             "resets_at": "2026-08-25T09:00:00.294755+00:00", "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 46,
             "resets_at": "2026-08-26T11:00:00+00:00", "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 13,
             "resets_at": "2026-08-26T11:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}}},
        ]})
        assert [l.kind for l in snap.limits] == ["session", "weekly_all", "weekly_scoped"]
        assert snap.get("session").percent == 70.0
        assert snap.get("session").resets_at.year == 2026
        assert snap.get("weekly_scoped").label == "weekly (Fable)"
        assert snap.get("session").label == "5h session"
        assert snap.get("weekly_all").label == "weekly"

    def test_missing_limits_key_yields_empty(self):
        assert parse_usage_payload({}).limits == ()
        assert parse_usage_payload(None).limits == ()

    def test_garbage_rows_are_dropped_not_raised(self):
        snap = parse_usage_payload({"limits": [
            "not-a-dict",
            {"kind": "session", "percent": "abc"},
            {"kind": "weekly_all", "group": "weekly", "percent": 12},
        ]})
        assert [l.kind for l in snap.limits] == ["weekly_all"]

    def test_unparseable_reset_becomes_none(self):
        snap = parse_usage_payload({"limits": [
            {"kind": "session", "percent": 100, "resets_at": "tomorrow-ish"},
        ]})
        assert snap.get("session").resets_at is None

    def test_naive_reset_is_treated_as_utc(self):
        snap = parse_usage_payload({"limits": [
            {"kind": "session", "percent": 5, "resets_at": "2026-08-25T09:00:00"},
        ]})
        assert snap.get("session").resets_at.tzinfo is timezone.utc

    def test_z_suffix_reset(self):
        snap = parse_usage_payload({"limits": [
            {"kind": "session", "percent": 5, "resets_at": "2026-08-25T09:00:00Z"},
        ]})
        assert snap.get("session").resets_at.hour == 9


# ─── is_spent / blocking ─────────────────────────────────────────────

class TestBlocking:
    def test_under_threshold_is_clear(self):
        assert _limit(percent=99.0).is_spent(100.0, NOW) is False

    def test_at_threshold_is_spent(self):
        assert _limit(percent=100.0).is_spent(100.0, NOW) is True

    def test_past_reset_is_clear_even_at_100(self):
        # A cached 100% whose window already rolled over must not pin the
        # loop shut — that's what makes serving a stale snapshot safe.
        assert _limit(percent=100.0, mins=-1).is_spent(100.0, NOW) is False

    def test_no_reset_timestamp_still_blocks(self):
        assert _limit(percent=100.0, mins=None).is_spent(100.0, NOW) is True

    def test_blocking_filters_by_kind(self):
        snap = QuotaSnapshot(limits=(
            _limit(kind="session", percent=100.0),
            _limit(kind="weekly_scoped", percent=100.0, group="weekly"),
        ), fetched_at=NOW)
        blocked = snap.blocking(100.0, ("session", "weekly_all"), now=NOW)
        assert [l.kind for l in blocked] == ["session"]

    def test_weekly_scoped_excluded_by_default(self):
        # Exhausting a per-model weekly window downgrades the model rather
        # than blocking the account, so it must not park the loop.
        assert "weekly_scoped" not in usage.DEFAULT_BLOCKING_KINDS

    def test_custom_threshold_parks_early(self):
        snap = QuotaSnapshot(limits=(_limit(percent=92.0),), fetched_at=NOW)
        assert snap.blocking(100.0, ("session",), now=NOW) == []
        assert len(snap.blocking(90.0, ("session",), now=NOW)) == 1


# ─── helpers ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_soonest_reset_picks_earliest(self):
        early, late = _limit(mins=30), _limit(mins=300)
        assert soonest_reset([late, early]) == early.resets_at

    def test_soonest_reset_ignores_missing(self):
        assert soonest_reset([_limit(mins=None)]) is None
        assert soonest_reset([]) is None

    def test_describe_limits(self):
        assert describe_limits([_limit(percent=100.0)]) == "5h session at 100%"
        assert describe_limits([]) == "quota exhausted"

    @pytest.mark.parametrize("mins,expected", [
        (-5, "now"), (0, "now"), (14, "in 14m"), (150, "in 2h"), (60 * 30, "in 1d"),
    ])
    def test_format_eta(self, mins, expected):
        assert format_eta(NOW + timedelta(minutes=mins), now=NOW) == expected

    def test_format_eta_none(self):
        assert format_eta(None) == ""


# ─── credentials ─────────────────────────────────────────────────────

class TestReadAccessToken:
    def test_missing_file(self, tmp_path):
        assert read_access_token(tmp_path / "nope.json") == ""

    def test_malformed_json(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_text("{not json")
        assert read_access_token(f) == ""

    def test_reads_oauth_token(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_text('{"claudeAiOauth": {"accessToken": "sk-tok"}}')
        assert read_access_token(f) == "sk-tok"

    def test_missing_oauth_section(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_text('{"other": 1}')
        assert read_access_token(f) == ""


# ─── cache behaviour ─────────────────────────────────────────────────

class TestGetUsageCache:
    @pytest.fixture(autouse=True)
    def _clean(self):
        usage.clear_cache()
        yield
        usage.clear_cache()

    def test_reuses_snapshot_within_ttl(self, monkeypatch):
        calls = []
        snap = QuotaSnapshot(limits=(), fetched_at=NOW)
        monkeypatch.setattr(usage, "fetch_usage",
                            lambda *a, **k: (calls.append(1), snap)[1])
        assert usage.get_usage() is snap
        assert usage.get_usage() is snap
        assert len(calls) == 1

    def test_force_refetches(self, monkeypatch):
        calls = []
        snap = QuotaSnapshot(limits=(), fetched_at=NOW)
        monkeypatch.setattr(usage, "fetch_usage",
                            lambda *a, **k: (calls.append(1), snap)[1])
        usage.get_usage()
        usage.get_usage(force=True)
        assert len(calls) == 2

    def test_failed_fetch_serves_stale(self, monkeypatch):
        snap = QuotaSnapshot(limits=(), fetched_at=NOW)
        monkeypatch.setattr(usage, "fetch_usage", lambda *a, **k: snap)
        usage.get_usage()
        monkeypatch.setattr(usage, "fetch_usage", lambda *a, **k: None)
        assert usage.get_usage(force=True) is snap

    def test_failed_fetch_with_no_cache_returns_none(self, monkeypatch):
        monkeypatch.setattr(usage, "fetch_usage", lambda *a, **k: None)
        assert usage.get_usage() is None
