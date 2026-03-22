"""Application state — pure Python business logic, no Textual dependency.

AppState holds all the data and logic that drives the TUI. Every method is
testable with fast, synchronous tests. The app layer is a thin shell that
renders AppState into widgets and routes key events to state mutations.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Sequence


# ── Fuzzy matching ──────────────────────────────────────────────────

def fuzzy_match(query: str, text: str) -> int | None:
    """Score *query* as a fuzzy subsequence of *text*.

    Returns an integer score (higher is better) or ``None`` when *query*
    is not a subsequence of *text*.  Scoring rewards:
    * consecutive matching characters (streak bonus)
    * matches at word boundaries (after ``-_./`` or camelCase transition)
    * match starting at position 0 of *text*

    Both *query* and *text* are compared case-insensitively, but an
    exact-case hit gets a small bonus per character.
    """
    if not query:
        return 0
    if not text:
        return None

    q = query.lower()
    t = text.lower()
    qi = 0  # index into q
    score = 0
    streak = 0
    prev_match_idx = -2  # impossible start so first match isn't "consecutive"

    for ti, ch in enumerate(t):
        if qi < len(q) and ch == q[qi]:
            # Base point for a match
            score += 1

            # Consecutive bonus (grows with streak length)
            if ti == prev_match_idx + 1:
                streak += 1
                score += streak * 2
            else:
                streak = 0

            # Word-boundary bonus
            if ti == 0:
                score += 5
            elif t[ti - 1] in " -_./\\":
                score += 4
            elif t[ti - 1].islower() and ch != t[ti]:
                # camelCase boundary (original char is upper)
                score += 3

            # Exact-case bonus
            if text[ti] == query[qi]:
                score += 1

            prev_match_idx = ti
            qi += 1

    # All query chars consumed?
    if qi < len(q):
        return None

    return score


def fuzzy_filter(query: str, items: Sequence[str]) -> list[tuple[int, int]]:
    """Return ``(index, score)`` pairs for items matching *query*, best first."""
    results = []
    for i, text in enumerate(items):
        s = fuzzy_match(query, text)
        if s is not None:
            results.append((i, s))
    results.sort(key=lambda t: t[1], reverse=True)
    return results

import re
from dataclasses import dataclass, field

from models import (
    Category, Link, Origin, Status, Store, TodoItem, Workstream,
    STATUS_ICONS, _relative_time,
)
from sessions import ClaudeSession, SessionMessage, extract_session_content, get_live_session_ids, refresh_session_tail


# ── Content search ─────────────────────────────────────────────────

@dataclass
class SearchHit:
    """A single match within a session message."""
    message_idx: int
    role: str
    timestamp: str
    snippet: str                          # ~120 chars of context
    match_ranges: list[tuple[int, int]]   # (start, end) in snippet for highlighting
    score: float


@dataclass
class SessionSearchResult:
    """Aggregated search results for one session."""
    session: ClaudeSession
    total_score: float
    hit_count: int
    best_hit: SearchHit
    hits: list[SearchHit] = field(default_factory=list)


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """Parse query into phrase tokens and word tokens.

    Quoted strings become phrase tokens; remaining words become word tokens.
    Returns (phrases, words).
    """
    phrases: list[str] = []
    remainder = query
    for m in re.finditer(r'"([^"]+)"', query):
        phrases.append(m.group(1).lower())
        remainder = remainder.replace(m.group(0), " ")
    words = [w for w in remainder.lower().split() if w]
    return phrases, words


def extract_snippet(
    text: str,
    query_words: list[str],
    max_length: int = 140,
) -> tuple[str, list[tuple[int, int]]]:
    """Extract a snippet around the densest cluster of query words.

    Returns ``(snippet_text, match_ranges)`` where match_ranges are
    ``(start, end)`` offsets into snippet_text for highlighting.
    """
    text_lower = text.lower()

    # Find all match positions
    positions: list[tuple[int, int]] = []  # (start, end)
    for w in query_words:
        start = 0
        while True:
            idx = text_lower.find(w, start)
            if idx == -1:
                break
            positions.append((idx, idx + len(w)))
            start = idx + 1
    if not positions:
        # Fallback: return start of text
        snip = text[:max_length].strip()
        if len(text) > max_length:
            snip += "…"
        return snip, []

    positions.sort()

    # Find the window of max_length chars with the most matches
    best_start = 0
    best_count = 0
    for anchor, _ in positions:
        win_start = max(0, anchor - max_length // 4)
        win_end = win_start + max_length
        count = sum(1 for s, e in positions if s >= win_start and e <= win_end)
        if count > best_count:
            best_count = count
            best_start = win_start

    # Expand to word boundary
    if best_start > 0:
        sp = text.rfind(" ", max(0, best_start - 20), best_start + 10)
        if sp != -1:
            best_start = sp + 1

    snip_raw = text[best_start:best_start + max_length]
    # Collapse whitespace
    snip = " ".join(snip_raw.split())

    prefix = "…" if best_start > 0 else ""
    suffix = "…" if best_start + max_length < len(text) else ""
    snip = prefix + snip + suffix

    # Recalculate match ranges within the snippet
    snip_lower = snip.lower()
    ranges: list[tuple[int, int]] = []
    for w in query_words:
        start = 0
        while True:
            idx = snip_lower.find(w, start)
            if idx == -1:
                break
            ranges.append((idx, idx + len(w)))
            start = idx + 1
    # Merge overlapping ranges
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    return snip, merged


def search_session_content(
    query: str,
    messages: list[SessionMessage],
    session: ClaudeSession,
    max_hits: int = 5,
) -> SessionSearchResult | None:
    """Search through a session's messages and metadata for query terms.

    All query words must appear in a single message (AND semantics)
    for message hits.  Title/metadata matching is scored separately
    so sessions whose name matches always surface.
    Returns None if no match at all.
    """
    if not query.strip():
        return None

    phrases, words = _parse_query(query)
    all_terms = phrases + words
    if not all_terms:
        return None

    hits: list[SearchHit] = []
    title_score = 0.0

    # --- Title / metadata matching ---
    title_text = " ".join(filter(None, [
        session.display_name, session.last_message_text,
    ]))
    if title_text:
        title_lower = title_text.lower()
        matched_terms = [t for t in all_terms if t in title_lower]
        if matched_terms:
            # Strong bonus: title is the most visible field
            title_score = 50.0 * len(matched_terms) / len(all_terms)
            # Extra bonus when ALL terms match in the title
            if len(matched_terms) == len(all_terms):
                title_score += 30.0
            # Exact phrase in title is a very strong signal (multi-word only)
            query_lower = query.strip().lower()
            if ' ' in query_lower and query_lower in title_lower:
                title_score += 100.0
            # Create a synthetic hit from the title so there's a snippet
            snippet, match_ranges = extract_snippet(title_text, all_terms)
            hits.append(SearchHit(
                message_idx=-1,
                role="title",
                timestamp="",
                snippet=snippet,
                match_ranges=match_ranges,
                score=title_score,
            ))

    # --- Message content matching ---
    n_messages = len(messages)
    for i, msg in enumerate(messages):
        msg_lower = msg.text.lower()

        # Check AND: every term must appear
        if not all(t in msg_lower for t in all_terms):
            continue

        # Score
        score = 0.0
        for t in all_terms:
            freq = msg_lower.count(t)
            # Diminishing returns: first occurrence = 10, extras add less and cap at +10
            score += 10 + min((freq - 1) * 2, 10)

        # Exact phrase bonus: the full original query appears verbatim
        # Only meaningful for multi-word queries (single words already get full credit)
        query_lower = query.strip().lower()
        if ' ' in query_lower and query_lower in msg_lower:
            score += 80  # dominant signal — exact match should win

        # Proximity bonus: all terms within 200-char window
        if len(all_terms) > 1:
            first_positions = []
            for t in all_terms:
                idx = msg_lower.find(t)
                if idx != -1:
                    first_positions.append(idx)
            if first_positions:
                spread = max(first_positions) - min(first_positions)
                if spread < 200:
                    score += 20

        # User message bonus (user messages express intent)
        if msg.role == "user":
            score += 5

        # Recency: mild additive bonus for later messages
        if n_messages > 1:
            score += 3.0 * (i / (n_messages - 1))

        snippet, match_ranges = extract_snippet(msg.text, all_terms)

        hits.append(SearchHit(
            message_idx=i,
            role=msg.role,
            timestamp=msg.timestamp,
            snippet=snippet,
            match_ranges=match_ranges,
            score=score,
        ))

    if not hits:
        return None

    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:max_hits]
    # Rank by best hit first, with diminishing credit for additional hits.
    # This prevents chatty sessions from outranking an exact match.
    best = hits[0].score
    tail = sum(h.score for h in hits[1:])
    total_score = best + tail * 0.2

    return SessionSearchResult(
        session=session,
        total_score=total_score,
        hit_count=len(hits),
        best_hit=hits[0],
        hits=hits,
    )


def content_search(
    query: str,
    sessions: list[ClaudeSession],
    content_cache: dict[str, list[SessionMessage]],
) -> list[SessionSearchResult]:
    """Search across all sessions, returning ranked results.

    Populates *content_cache* for any sessions not yet extracted.
    Results are sorted by total_score descending.
    """
    if not query.strip():
        return []

    results: list[SessionSearchResult] = []
    for s in sessions:
        if s.session_id not in content_cache:
            if s.jsonl_path:
                content_cache[s.session_id] = extract_session_content(s.jsonl_path)
            else:
                content_cache[s.session_id] = []

        messages = content_cache[s.session_id]
        result = search_session_content(query, messages, s)
        if result is not None:
            results.append(result)

    # Add a recency bonus so newer sessions rank higher when scores are similar.
    # Up to 35% of the best score is awarded based on relative recency.
    if len(results) > 1:
        best_total = max(r.total_score for r in results)
        max_bonus = best_total * 0.35
        # Parse timestamps into epoch seconds for interpolation
        epochs: list[float | None] = []
        for r in results:
            ts = r.session.last_user_message_at
            if ts:
                try:
                    epochs.append(datetime.fromisoformat(ts).timestamp())
                except (ValueError, TypeError):
                    epochs.append(None)
            else:
                epochs.append(None)
        valid = [e for e in epochs if e is not None]
        if len(valid) >= 2:
            e_min, e_max = min(valid), max(valid)
            span = e_max - e_min
            if span > 0:
                for r, ep in zip(results, epochs):
                    if ep is not None:
                        frac = (ep - e_min) / span
                        r.total_score += max_bonus * frac

    results.sort(key=lambda r: r.total_score, reverse=True)
    return results
from threads import Thread, ThreadActivity, session_activity, load_last_seen, mark_thread_seen
from rendering import ViewMode, _best_activity, _all_sessions_seen


class AppState:
    """Central state container for the orchestrator.

    All business logic lives here. The Textual app calls these methods
    and re-renders based on the results.
    """

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.view_mode: ViewMode = ViewMode.WORKSTREAMS
        self.filter_mode: str = "all"
        self.sort_mode: str = "updated"
        self.search_text: str = ""
        self.sessions: list[ClaudeSession] = []
        self.threads: list[Thread] = []
        self.discovered_ws: list[Workstream] = []
        self.preview_visible: bool = True
        self.tmux_paths: set[str] = set()
        self.tmux_names: set[str] = set()
        self.throbber_frame: int = 0
        self.preview_sessions: list[ClaudeSession] = []
        self.last_seen_cache: dict[str, str] = {}
        self._sessions_for_ws_cache: dict[str, list[ClaudeSession]] = {}
        self._last_seen_valid: bool = False
        self._session_mtimes: dict[str, float] = {}  # session_id -> last known mtime
        self.infer_repo_paths()

    # ── View navigation ──

    def next_view(self) -> ViewMode:
        modes = list(ViewMode)
        idx = modes.index(self.view_mode)
        self.view_mode = modes[(idx + 1) % len(modes)]
        return self.view_mode

    def prev_view(self) -> ViewMode:
        modes = list(ViewMode)
        idx = modes.index(self.view_mode)
        self.view_mode = modes[(idx - 1) % len(modes)]
        return self.view_mode

    # ── Filtering & sorting ──

    def set_filter(self, mode: str):
        self.filter_mode = mode

    def set_sort(self, mode: str):
        self.sort_mode = mode

    def set_search(self, text: str):
        self.search_text = text

    def get_filtered_streams(self) -> list[Workstream]:
        """Apply current filter, search, and sort to manual workstreams."""
        if self.filter_mode == "all":
            streams = list(self.store.active)
        elif self.filter_mode == "work":
            streams = [w for w in self.store.active if w.category == Category.WORK]
        elif self.filter_mode == "personal":
            streams = [w for w in self.store.active if w.category == Category.PERSONAL]
        elif self.filter_mode == "active":
            streams = [w for w in self.store.active if w.is_active]
        elif self.filter_mode == "stale":
            streams = self.store.stale()
        else:
            streams = list(self.store.active)

        if self.search_text:
            scored = []
            for w in streams:
                # Best fuzzy score across name and description
                s1 = fuzzy_match(self.search_text, w.name)
                s2 = fuzzy_match(self.search_text, w.description)
                best = max(s for s in (s1, s2) if s is not None) if any(s is not None for s in (s1, s2)) else None
                if best is not None:
                    scored.append((w, best))
            scored.sort(key=lambda t: t[1], reverse=True)
            streams = [w for w, _ in scored]
            # When searching, fuzzy rank takes priority over sort
            return streams

        return self.store.sorted(streams, self.sort_mode)

    def get_last_seen(self) -> dict[str, str]:
        """Return cached last-seen data, refreshing from disk only when invalidated."""
        if not self._last_seen_valid:
            self.last_seen_cache = load_last_seen()
            self._last_seen_valid = True
        return self.last_seen_cache

    def get_unified_items(self) -> list[Workstream]:
        """Build unified list: manual workstreams + AI-discovered workstreams."""
        manual = self.get_filtered_streams()
        discovered = list(self.discovered_ws)

        # Apply search filter to discovered
        if self.search_text:
            scored = []
            for w in discovered:
                s1 = fuzzy_match(self.search_text, w.name)
                s2 = fuzzy_match(self.search_text, w.description)
                best = max(s for s in (s1, s2) if s is not None) if any(s is not None for s in (s1, s2)) else None
                if best is not None:
                    scored.append((w, best))
            scored.sort(key=lambda t: t[1], reverse=True)
            discovered = [w for w, _ in scored]

        # Apply category filter to discovered
        if self.filter_mode == "work":
            discovered = [w for w in discovered if w.category == Category.WORK]
        elif self.filter_mode == "personal":
            discovered = [w for w in discovered if w.category == Category.PERSONAL]

        # Sort discovered: unread responses float to top, then by last user message time
        last_seen = self.get_last_seen()

        def _has_unread(ws: Workstream) -> bool:
            sessions = self.sessions_for_ws(ws)
            best = _best_activity(sessions, last_seen)
            if best == ThreadActivity.THINKING:
                return True
            if best in (ThreadActivity.RESPONSE_FRESH, ThreadActivity.RESPONSE_READY, ThreadActivity.AWAITING_INPUT):
                return not _all_sessions_seen(sessions, last_seen)
            return False

        discovered.sort(key=lambda w: w.last_user_activity or w.updated_at or "", reverse=True)
        discovered.sort(key=lambda w: 0 if _has_unread(w) else 1)

        return manual + discovered

    # ── Workstream selection ──

    def get_ws(self, ws_id: str) -> Workstream | None:
        """Look up a workstream by ID in both store and discovered."""
        ws = self.store.get(ws_id)
        if ws:
            return ws
        return next((w for w in self.discovered_ws if w.id == ws_id), None)

    def get_session(self, session_id: str) -> ClaudeSession | None:
        return next((s for s in self.sessions if s.session_id == session_id), None)

    def get_archived(self, ws_id: str) -> Workstream | None:
        return next((w for w in self.store.workstreams if w.id == ws_id), None)

    # ── Repo linking ──

    def infer_repo_paths(self) -> int:
        """Backfill repo_path on workstreams from links or matched sessions.

        Returns count of workstreams updated.
        """
        count = 0
        all_ws = list(self.store.workstreams) + list(self.discovered_ws)
        for ws in all_ws:
            if ws.repo_path:
                continue
            # 1. Prefer worktree links, then file links pointing at git repos
            for kind in ("worktree", "file"):
                for link in ws.links:
                    if link.kind != kind:
                        continue
                    expanded = os.path.expanduser(link.value).rstrip("/")
                    if os.path.isdir(expanded) and os.path.isdir(os.path.join(expanded, ".git")):
                        ws.repo_path = expanded
                        count += 1
                        break
                if ws.repo_path:
                    break
            if ws.repo_path:
                continue
            # 2. Infer from matched sessions' project_path (must be a git repo, not home dir)
            home = str(Path.home())
            sessions = self.sessions_for_ws(ws)
            if sessions:
                paths: dict[str, int] = {}
                for s in sessions:
                    p = s.project_path.rstrip("/")
                    if (p and p != home and os.path.isdir(p)
                            and os.path.isdir(os.path.join(p, ".git"))):
                        paths[p] = paths.get(p, 0) + 1
                if paths:
                    best = max(paths, key=paths.get)
                    ws.repo_path = best
                    count += 1
        if count:
            self.store.save()
        return count

    def _ws_dirs(self, ws: Workstream) -> set[str]:
        """Collect all directory paths for a workstream (repo_path + links)."""
        dirs = set()
        if ws.repo_path:
            expanded = os.path.expanduser(ws.repo_path).rstrip("/")
            if os.path.isdir(expanded):
                dirs.add(expanded)
        for link in ws.links:
            if link.kind in ("worktree", "file"):
                expanded = os.path.expanduser(link.value).rstrip("/")
                if os.path.isdir(expanded):
                    dirs.add(expanded)
        return dirs

    def known_repos(self) -> list[str]:
        """Unique repo paths from session history + workstream repo_path values."""
        repos = set()
        for s in self.sessions:
            if s.project_path:
                repos.add(s.project_path.rstrip("/"))
        for ws in self.store.active:
            if ws.repo_path:
                repos.add(os.path.expanduser(ws.repo_path).rstrip("/"))
        return sorted(p for p in repos if os.path.isdir(p))

    # ── Full-system repo discovery ────────────────────────────────────

    _all_repos: list[str] | None = None

    _SKIP_DIRS = frozenset({
        ".cache", ".local", ".npm", ".cargo", ".rustup", ".go", ".gradle",
        ".m2", ".steam", ".platformio", ".nvm", ".pyenv", ".rbenv",
        "node_modules", "__pycache__", "venv", ".venv", "snap",
        ".Trash", ".wine", "Library",
    })

    def discover_all_repos(self, *, force: bool = False) -> list[str]:
        """Scan ~ recursively for git repos, merging with known_repos().

        Results are cached; pass *force=True* to rescan.
        """
        if self._all_repos is not None and not force:
            return self._all_repos

        home = Path.home()
        repos: set[str] = set()
        max_depth = 5

        def _scan(directory: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = list(os.scandir(directory))
            except (PermissionError, OSError):
                return
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name
                if name in self._SKIP_DIRS:
                    continue
                path = Path(entry.path)
                git_dir = path / ".git"
                if git_dir.exists():
                    repos.add(str(path))
                    continue
                # Hidden dirs: check for .git but don't recurse further
                if name.startswith("."):
                    continue
                _scan(path, depth + 1)

        _scan(home, 0)

        # Merge with known_repos (session history + workstream paths)
        for p in self.known_repos():
            repos.add(p)

        self._all_repos = sorted(repos)
        return self._all_repos

    def invalidate_repo_cache(self) -> None:
        """Clear cached repo list so next discover_all_repos() rescans."""
        self._all_repos = None

    def workstreams_for_repo(self, repo_path: str) -> list[Workstream]:
        """Find non-archived workstreams linked to a repo path."""
        normalized = os.path.expanduser(repo_path).rstrip("/")
        results = []
        for ws in self.store.active:
            if ws.repo_path and os.path.expanduser(ws.repo_path).rstrip("/") == normalized:
                results.append(ws)
                continue
            for link in ws.links:
                if link.kind in ("worktree", "file"):
                    expanded = os.path.expanduser(link.value).rstrip("/")
                    if expanded == normalized:
                        results.append(ws)
                        break
        return results

    def create_ws_for_repo(self, repo_path: str) -> Workstream:
        """Auto-create a workstream for a repo. Returns the new workstream."""
        name = Path(repo_path).name
        ws = Workstream(
            name=name,
            repo_path=repo_path,
            status=Status.IN_PROGRESS,
            category=Category.PERSONAL,
        )
        ws.add_link(kind="worktree", value=repo_path, label="repo")
        self.store.add(ws)
        return ws

    # ── Session matching ──

    def sessions_for_ws(self, ws: Workstream, include_archived_sessions: bool = False) -> list[ClaudeSession]:
        """Find sessions for a workstream via thread_ids or directory matching."""
        from actions import find_sessions_for_ws

        cache_key = f"{ws.id}:{include_archived_sessions}"
        if cache_key in self._sessions_for_ws_cache:
            return self._sessions_for_ws_cache[cache_key]

        hidden_sids = set(ws.archived_sessions) if not include_archived_sessions else set()

        effective_tids = ws.thread_ids
        if not effective_tids and self.threads:
            ws_dirs = self._ws_dirs(ws)
            explicit_sids = {link.value for link in ws.links if link.kind == "claude-session"}
            matched = set()
            for t in self.threads:
                if t.project_path.rstrip("/") in ws_dirs:
                    matched.add(t.thread_id)
                elif explicit_sids:
                    for s in t.sessions:
                        if s.session_id in explicit_sids or any(
                            s.session_id.startswith(sid) for sid in explicit_sids
                        ):
                            matched.add(t.thread_id)
                            break
            effective_tids = list(matched)

        if effective_tids:
            thread_map = {t.thread_id: t for t in self.threads}
            sessions = []
            seen = set()
            for tid in effective_tids:
                t = thread_map.get(tid)
                if t:
                    for s in t.sessions:
                        if s.session_id not in seen and s.session_id not in hidden_sids:
                            sessions.append(s)
                            seen.add(s.session_id)
            sessions.sort(key=lambda s: s.last_activity or "", reverse=True)
            self._sessions_for_ws_cache[cache_key] = sessions
            return sessions

        result = find_sessions_for_ws(ws, self.sessions)
        self._sessions_for_ws_cache[cache_key] = result
        return result

    def find_ws_for_session(self, session: ClaudeSession) -> Workstream | None:
        """Reverse-lookup: find a workstream that owns this session."""
        sp = session.project_path.rstrip("/")
        for ws in self.store.active:
            for link in ws.links:
                if link.kind == "claude-session" and (
                    link.value == session.session_id or
                    session.session_id.startswith(link.value)
                ):
                    return ws
            if ws.repo_path and os.path.expanduser(ws.repo_path).rstrip("/") == sp:
                return ws
            for link in ws.links:
                if link.kind in ("worktree", "file"):
                    expanded = os.path.expanduser(link.value).rstrip("/")
                    if os.path.isdir(expanded) and sp == expanded:
                        return ws
        return None

    # ── Mutations ──

    def cycle_status(self, ws_id: str, forward: bool = True) -> Workstream | None:
        """Cycle a workstream's status. Returns the workstream if found."""
        ws = self.get_ws(ws_id)
        if not ws:
            return None
        statuses = list(Status)
        idx = statuses.index(ws.status)
        direction = 1 if forward else -1
        ws.set_status(statuses[(idx + direction) % len(statuses)])
        self.store.update(ws)
        return ws

    # ── Todo operations ───────────────────────────────────────────

    def add_todo(self, ws_id: str, text: str, context: str = "") -> TodoItem | None:
        """Add a todo item. Returns the item or None."""
        ws = self.get_ws(ws_id)
        if not ws or not text.strip():
            return None
        item = TodoItem(text=text.strip(), context=context.strip())
        ws.todos.append(item)
        self.store.update(ws)
        return item

    def toggle_todo(self, ws_id: str, todo_id: str) -> bool:
        """Toggle done flag on a todo item."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        for t in ws.todos:
            if t.id == todo_id:
                t.done = not t.done
                self.store.update(ws)
                return True
        return False

    def archive_todo(self, ws_id: str, todo_id: str) -> bool:
        """Archive a todo item."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        for t in ws.todos:
            if t.id == todo_id:
                t.archived = True
                self.store.update(ws)
                return True
        return False

    def unarchive_todo(self, ws_id: str, todo_id: str) -> bool:
        """Unarchive a todo item."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        for t in ws.todos:
            if t.id == todo_id:
                t.archived = False
                self.store.update(ws)
                return True
        return False

    def delete_todo(self, ws_id: str, todo_id: str) -> bool:
        """Delete a todo item."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        before = len(ws.todos)
        ws.todos = [t for t in ws.todos if t.id != todo_id]
        if len(ws.todos) < before:
            self.store.update(ws)
            return True
        return False

    def edit_todo(self, ws_id: str, todo_id: str, text: str | None = None, context: str | None = None) -> bool:
        """Edit a todo item's text and/or context."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        for t in ws.todos:
            if t.id == todo_id:
                if text is not None:
                    t.text = text.strip()
                if context is not None:
                    t.context = context.strip()
                self.store.update(ws)
                return True
        return False

    def reorder_todo(self, ws_id: str, todo_id: str, direction: int) -> bool:
        """Move a todo item up (-1) or down (+1) within the active list."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        active = [t for t in ws.todos if not t.archived]
        idx = next((i for i, t in enumerate(active) if t.id == todo_id), None)
        if idx is None:
            return False
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(active):
            return False
        # Swap in the active list, then rebuild ws.todos preserving archived positions
        active[idx], active[new_idx] = active[new_idx], active[idx]
        archived = [t for t in ws.todos if t.archived]
        ws.todos = active + archived
        self.store.update(ws)
        return True

    @staticmethod
    def active_todos(ws: Workstream) -> list[TodoItem]:
        """Non-archived todos: crystallized first, then undone, then done."""
        active = [t for t in ws.todos if not t.archived]
        crystallized = [t for t in active if not t.done and getattr(t, "origin", "manual") == "crystallized"]
        undone = [t for t in active if not t.done and getattr(t, "origin", "manual") != "crystallized"]
        done = [t for t in active if t.done]
        return crystallized + undone + done

    @staticmethod
    def archived_todos(ws: Workstream) -> list[TodoItem]:
        """Archived todo items."""
        return [t for t in ws.todos if t.archived]

    def rename(self, ws_id: str, new_name: str) -> bool:
        """Rename a workstream. Returns True if successful."""
        ws = self.get_ws(ws_id)
        if not ws or not new_name.strip():
            return False
        ws.name = new_name.strip()
        self.store.update(ws)
        return True

    def archive(self, ws_id: str) -> str | None:
        """Archive a workstream. Returns the name if successful."""
        ws = self.get_ws(ws_id)
        if not ws:
            return None
        self.store.archive(ws_id)
        return ws.name

    def unarchive(self, ws_id: str) -> str | None:
        """Unarchive a workstream. Returns the name if successful."""
        ws = self.get_archived(ws_id)
        if not ws:
            return None
        self.store.unarchive(ws_id)
        return ws.name

    def delete(self, ws_id: str) -> str | None:
        """Delete a workstream. Returns the name if successful."""
        ws = self.get_ws(ws_id) or self.get_archived(ws_id)
        if not ws:
            return None
        name = ws.name
        self.store.remove(ws_id)
        return name

    def add_link(self, ws_id: str, link: Link) -> bool:
        """Add a link to a workstream."""
        ws = self.get_ws(ws_id)
        if not ws:
            return False
        ws.links.append(link)
        ws.touch()
        self.store.update(ws)
        return True

    # ── Notifications ──

    def notifications_for_ws(self, ws: Workstream) -> list:
        """Get notifications matching this workstream's directories."""
        from notifications import load_notifications, notifications_for_dirs
        dirs = self._ws_dirs(ws)
        if not dirs:
            return []
        return notifications_for_dirs(load_notifications(), dirs)

    # ── Session management ──

    def update_sessions(self, sessions: list[ClaudeSession],
                        threads: list[Thread], discovered: list[Workstream]):
        """Apply new session/thread data from background discovery."""
        self.sessions = sessions
        self.threads = threads
        self.discovered_ws = discovered
        self.invalidate_caches()
        self.infer_repo_paths()

    def invalidate_caches(self):
        """Clear derived-data caches after session/thread updates."""
        self._sessions_for_ws_cache.clear()
        self._last_seen_valid = False

    def refresh_liveness(self) -> bool:
        """Update is_live flags and tail-read active sessions. Returns True if anything changed."""
        old_live = {s.session_id for s in self.sessions if s.is_live}
        live_ids = get_live_session_ids()
        for s in self.sessions:
            s.is_live = s.session_id in live_ids
        for s in self.preview_sessions:
            s.is_live = s.session_id in live_ids
        new_live = {s.session_id for s in self.sessions if s.is_live}

        changed = old_live != new_live
        active_ids = new_live | (old_live - new_live)
        seen = set()
        for s in self.sessions:
            if s.session_id in active_ids and s.session_id not in seen:
                seen.add(s.session_id)
                # Skip tail read if file hasn't changed
                try:
                    mtime = os.path.getmtime(s.jsonl_path)
                except OSError:
                    continue
                if mtime == self._session_mtimes.get(s.session_id):
                    continue
                self._session_mtimes[s.session_id] = mtime
                if refresh_session_tail(s):
                    changed = True
        for s in self.preview_sessions:
            if s.session_id in active_ids and s.session_id not in seen:
                seen.add(s.session_id)
                try:
                    mtime = os.path.getmtime(s.jsonl_path)
                except OSError:
                    continue
                if mtime == self._session_mtimes.get(s.session_id):
                    continue
                self._session_mtimes[s.session_id] = mtime
                refresh_session_tail(s)

        if changed:
            self.invalidate_caches()
        return changed

    # ── Tmux ──

    def update_tmux_status(self, paths: set[str], names: set[str]) -> bool:
        """Update tmux window state. Returns True if changed."""
        if paths != self.tmux_paths or names != self.tmux_names:
            self.tmux_paths = paths
            self.tmux_names = names
            return True
        return False

    def ws_has_tmux(self, ws: Workstream) -> bool:
        if ws.repo_path:
            rp = os.path.expanduser(ws.repo_path).rstrip("/")
            for tmux_path in self.tmux_paths:
                if tmux_path == rp or tmux_path.startswith(rp + "/"):
                    return True
        for link in ws.links:
            if link.kind == "worktree":
                expanded = os.path.expanduser(link.value).rstrip("/")
                for tmux_path in self.tmux_paths:
                    if tmux_path == expanded or tmux_path.startswith(expanded + "/"):
                        return True
        spawn_name = f"\U0001f916{ws.name[:18]}"
        if spawn_name in self.tmux_names:
            return True
        if ws.name[:20] in self.tmux_names:
            return True
        return False

    # ── Command execution ──

    def execute_command(self, cmd_text: str, selected_ws_id: str | None = None) -> dict:
        """Execute a command palette command. Returns action dict for the app to handle.

        Returns: {"action": str, ...} where action is one of:
            "view", "notify", "refresh", "spawn", "resume", "export",
            "brain", "help", "delete", "unarchive", "error"
        """
        from rendering import LINK_KINDS

        parts = cmd_text.strip().split(None, 1)
        if not parts:
            return {"action": "noop"}

        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        ws = self.get_ws(selected_ws_id) if selected_ws_id else None

        # View switching
        if cmd in ("workstreams", "ws"):
            self.view_mode = ViewMode.WORKSTREAMS
            return {"action": "view"}
        elif cmd == "sessions":
            self.view_mode = ViewMode.SESSIONS
            return {"action": "view"}
        elif cmd == "archived":
            self.view_mode = ViewMode.ARCHIVED
            return {"action": "view"}

        # Status
        elif cmd in ("status", "st") and ws:
            if not arg:
                return {"action": "error", "msg": "Usage: status <queued|in-progress|awaiting-review|done|blocked>"}
            try:
                ws.set_status(Status(arg))
                self.store.update(ws)
                return {"action": "refresh", "msg": f"{ws.name} → {STATUS_ICONS[ws.status]} {ws.status.value}"}
            except ValueError:
                return {"action": "error", "msg": f"Invalid status: {arg}"}

        # Link
        elif cmd in ("link", "ln") and ws:
            if ":" not in arg:
                return {"action": "error", "msg": "Usage: link kind:value (e.g. ticket:UB-1234)"}
            kind, value = arg.split(":", 1)
            if kind not in LINK_KINDS:
                return {"action": "error", "msg": f"Unknown kind: {kind}"}
            ws.add_link(kind=kind, value=value, label=kind)
            self.store.update(ws)
            return {"action": "refresh", "msg": f"Added {kind} link to {ws.name}"}

        # Note → Todo
        elif cmd in ("note", "n", "todo", "t") and ws:
            if not arg:
                return {"action": "error", "msg": "Usage: note <text>"}
            self.add_todo(ws.id, arg)
            return {"action": "notify", "msg": f"Todo added to {ws.name}"}

        # Archive
        elif cmd in ("archive", "a") and ws:
            self.archive(ws.id)
            return {"action": "refresh", "msg": f"Archived: {ws.name}"}

        # Unarchive
        elif cmd in ("unarchive", "ua"):
            return {"action": "unarchive"}

        # Delete
        elif cmd in ("delete", "del"):
            return {"action": "delete"}

        # Search
        elif cmd == "search":
            self.search_text = arg
            return {"action": "refresh"}

        # Sort
        elif cmd == "sort":
            valid = ("status", "updated", "created", "category", "name")
            if arg in valid:
                self.sort_mode = arg
                return {"action": "refresh"}
            return {"action": "error", "msg": f"Sort by: {', '.join(valid)}"}

        # Filter
        elif cmd in ("filter", "f"):
            valid = ("all", "work", "personal", "active", "stale")
            if arg in valid:
                self.filter_mode = arg
                return {"action": "refresh"}
            return {"action": "error", "msg": f"Filter: {', '.join(valid)}"}

        # Spawn
        elif cmd == "spawn":
            return {"action": "spawn"}

        # Resume
        elif cmd == "resume":
            return {"action": "resume"}

        # Export
        elif cmd == "export":
            return {"action": "export", "path": arg}

        # Brain
        elif cmd == "brain":
            return {"action": "brain", "text": arg}

        # Help
        elif cmd == "help":
            return {"action": "help"}

        return {"action": "error", "msg": f"Unknown command: {cmd}"}

    def do_export(self, path: str = "") -> tuple[str, int]:
        """Export active workstreams to markdown. Returns (output_path, count)."""
        from rendering import _status_markup

        streams = self.store.active
        output = path or os.path.expanduser("~/workstreams/active.md")

        lines = [
            "# Active Workstreams",
            f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]
        for cat in Category:
            cat_streams = [w for w in streams if w.category == cat]
            if not cat_streams:
                continue
            lines.append(f"## {cat.value.title()}")
            lines.append("")
            cat_streams = self.store.sorted(cat_streams, "status")
            for ws in cat_streams:
                ws_icon = STATUS_ICONS[ws.status]
                lines.append(f"### {ws_icon} {ws.name}")
                lines.append(f"**Status:** {ws.status.value} | **Updated:** {_relative_time(ws.updated_at)}")
                if ws.description:
                    lines.append(f"\n{ws.description}")
                if ws.links:
                    lines.append("\n**Links:**")
                    for lnk in ws.links:
                        if lnk.kind == "url":
                            lines.append(f"- [{lnk.label}]({lnk.value})")
                        else:
                            lines.append(f"- `{lnk.kind}`: {lnk.value}")
                if ws.notes:
                    lines.append(f"\n**Notes:**\n{ws.notes}")
                lines.append("")

        from pathlib import Path
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text("\n".join(lines) + "\n")
        return output, len(streams)
