"""Claude subscription quota — the same numbers the `/usage` screen shows.

Reads the OAuth usage endpoint that Claude Code's own `/usage` view hits,
authenticated with the access token Claude Code keeps in
`~/.claude/.credentials.json`. Stdlib only — no Textual, no third-party
HTTP client (see tests/test_purity.py).

The payload's `limits[]` is the part worth caring about::

    {"kind": "session",       "group": "session", "percent": 70,
     "resets_at": "2026-08-25T09:00:00+00:00", "is_active": true}
    {"kind": "weekly_all",    "group": "weekly",  "percent": 46, ...}
    {"kind": "weekly_scoped", "group": "weekly",  "percent": 13,
     "scope": {"model": {"display_name": "Fable"}}, ...}

Auto-mode uses this to hold the loop when a limit is exhausted and to
learn when it will reset (see `auto_mode.AutoMode._await_quota`).
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

DEFAULT_TIMEOUT_S = 8.0

# How long a successful fetch is reused before hitting the network again.
DEFAULT_CACHE_TTL_S = 60.0

# How long a cached snapshot may keep standing in for a *failed* fetch.
# Serving stale here is safe because `QuotaSnapshot.blocking()` ignores any
# limit whose own reset time has already passed — a stale "100%" expires on
# its own rather than pinning the loop shut forever.
STALE_FALLBACK_S = 900.0

# Limit kinds that actually stop all work when exhausted. `weekly_scoped`
# is deliberately excluded: exhausting the per-model weekly window (e.g.
# Opus) downgrades the model rather than blocking the account, so pausing
# on it would idle a loop that could still make progress.
DEFAULT_BLOCKING_KINDS: tuple[str, ...] = ("session", "weekly_all")


def _parse_reset(value) -> Optional[datetime]:
    """Parse an API `resets_at` string into an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuotaLimit:
    """One row of the usage endpoint's `limits[]`."""

    kind: str                          # session | weekly_all | weekly_scoped
    group: str                         # session | weekly
    percent: float                     # 0-100, percent of the window consumed
    resets_at: Optional[datetime]      # aware UTC, or None when unknown
    scope_label: str = ""              # e.g. "Opus" for weekly_scoped rows
    is_active: bool = False

    @property
    def label(self) -> str:
        """Short human name, e.g. '5h session' or 'weekly (Opus)'."""
        if self.kind == "session":
            return "5h session"
        base = "weekly"
        if self.scope_label:
            return f"{base} ({self.scope_label})"
        return base

    def is_spent(self, threshold: float, now: Optional[datetime] = None) -> bool:
        """True if this limit is at/over `threshold` and hasn't reset yet.

        A limit whose `resets_at` is already in the past carries a stale
        percentage — the window rolled over and the API just hasn't been
        re-read. Treat it as clear so a cached snapshot can't pin the
        loop shut past its own expiry.
        """
        if self.percent < threshold:
            return False
        if self.resets_at is None:
            return True
        return self.resets_at > (now or datetime.now(timezone.utc))


@dataclass(frozen=True)
class QuotaSnapshot:
    """A point-in-time read of every usage limit."""

    limits: tuple[QuotaLimit, ...]
    fetched_at: datetime

    def blocking(
        self,
        threshold: float = 100.0,
        kinds: Iterable[str] = DEFAULT_BLOCKING_KINDS,
        now: Optional[datetime] = None,
    ) -> list[QuotaLimit]:
        """Watched limits that are exhausted right now."""
        wanted = set(kinds)
        return [
            lim for lim in self.limits
            if lim.kind in wanted and lim.is_spent(threshold, now)
        ]

    def get(self, kind: str) -> Optional[QuotaLimit]:
        return next((lim for lim in self.limits if lim.kind == kind), None)


def parse_usage_payload(payload: dict) -> QuotaSnapshot:
    """Build a snapshot from the endpoint's JSON body.

    Unknown/garbage rows are dropped rather than raising — the loop must
    never die because the API grew a field.
    """
    limits: list[QuotaLimit] = []
    for raw in (payload or {}).get("limits") or []:
        if not isinstance(raw, dict):
            continue
        try:
            percent = float(raw.get("percent") or 0)
        except (TypeError, ValueError):
            continue
        scope = raw.get("scope") or {}
        model = (scope.get("model") or {}) if isinstance(scope, dict) else {}
        limits.append(QuotaLimit(
            kind=str(raw.get("kind") or ""),
            group=str(raw.get("group") or ""),
            percent=percent,
            resets_at=_parse_reset(raw.get("resets_at")),
            scope_label=str(model.get("display_name") or ""),
            is_active=bool(raw.get("is_active")),
        ))
    return QuotaSnapshot(
        limits=tuple(limits),
        fetched_at=datetime.now(timezone.utc),
    )


def read_access_token(path: Path = CREDENTIALS_PATH) -> str:
    """OAuth access token from Claude Code's credentials file, or ''."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return ""
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return ""
    return str(oauth.get("accessToken") or "")


def fetch_usage(timeout: float = DEFAULT_TIMEOUT_S) -> Optional[QuotaSnapshot]:
    """Hit the usage endpoint. Returns None on any failure.

    Blocking — call from a thread (`asyncio.to_thread`) inside the loop.
    A 401 here just means Claude Code is mid-token-refresh; the caller
    falls back to the cached snapshot.
    """
    token = read_access_token()
    if not token:
        return None
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or "limits" not in payload:
        return None
    return parse_usage_payload(payload)


# Process-local cache: (monotonic stamp, snapshot).
_cache: Optional[tuple[float, QuotaSnapshot]] = None


def clear_cache() -> None:
    """Drop the cached snapshot (tests; and after a manual resume)."""
    global _cache
    _cache = None


def get_usage(
    ttl: float = DEFAULT_CACHE_TTL_S,
    force: bool = False,
) -> Optional[QuotaSnapshot]:
    """Cached `fetch_usage`. None when the quota can't be determined.

    On a failed fetch, a snapshot younger than STALE_FALLBACK_S stands in
    so a transient network blip doesn't read as "quota is fine" — stale
    100%s still expire on their own via `QuotaLimit.is_spent`.
    """
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache[0]) < ttl:
        return _cache[1]
    snap = fetch_usage()
    if snap is not None:
        _cache = (now, snap)
        return snap
    if _cache is not None and (now - _cache[0]) < STALE_FALLBACK_S:
        return _cache[1]
    return None


def describe_limits(limits: Iterable[QuotaLimit]) -> str:
    """'5h session at 100%, weekly at 100%' — for notifications."""
    parts = [f"{lim.label} at {lim.percent:.0f}%" for lim in limits]
    return ", ".join(parts) if parts else "quota exhausted"


def soonest_reset(limits: Iterable[QuotaLimit]) -> Optional[datetime]:
    """Earliest reset among `limits`, or None if none carry one.

    Earliest rather than latest on purpose: the loop re-polls after each
    wait, so it should wake at the first moment anything could change.
    """
    stamps = [lim.resets_at for lim in limits if lim.resets_at is not None]
    return min(stamps) if stamps else None


def format_eta(when: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Compact 'in 2h' / 'in 14m' / 'now' for a reset timestamp."""
    if when is None:
        return ""
    if when.tzinfo is None:  # naive round-trip through a persisted string
        when = when.replace(tzinfo=timezone.utc)
    secs = (when - (now or datetime.now(timezone.utc))).total_seconds()
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"in {int(secs // 60)}m"
    if secs < 86400:
        return f"in {int(secs // 3600)}h"
    return f"in {int(secs // 86400)}d"
