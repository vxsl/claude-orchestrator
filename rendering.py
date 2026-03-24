"""Rendering helpers — color palette, Rich markup, display formatting.

Pure functions with no Textual dependency. Used by screens, app, and state modules.
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text

def _rich_escape(text: str) -> str:
    """Escape ALL [ characters for Rich markup.

    rich.markup.escape() only escapes sequences that look like valid tags,
    but arbitrary text with [ can still break when embedded inside markup
    (e.g. "[#585858][Binding(key=...)[/#585858]" confuses the parser).
    """
    return text.replace("[", r"\[")


from models import (
    Category, TodoItem, Workstream,
    _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, _ACTIVITY_PRIORITY


# ─── Color Palette (high contrast on true black) ─────────────────────
# Matches alacritty GitHub Dark theme — bright text on black background

C_BLUE = "#58a6ff"       # structural, borders
C_PURPLE = "#d2a8ff"     # headings, personal category
C_CYAN = "#56d4dd"       # active states, work category
C_GREEN = "#6ab889"      # success, done — soft green
C_YELLOW = "#e3b341"     # warnings, queued
C_ORANGE = "#d7875f"     # secondary accents
C_RED = "#ffa198"        # errors, blocked
C_GOLD = "#e3b341"       # crystallized todos, distilled knowledge
C_LIGHT = "#e6edf3"      # primary text (terminal foreground)
C_MID = "#b1bac4"        # secondary text (terminal normal white)
C_DIM = "#6e7681"        # subdued (terminal bright-black)
C_FAINT = "#484f58"      # near-invisible — IDs, decorative
C_RESOLVED = "#7a8a9e"  # muted blue-gray — committed/resolved sessions
C_SHELF = "#7a5218"    # dim amber — shelved sessions (set aside)

# ─── Background Palette ──────────────────────────────────────────────
BG_BASE = "#000000"      # true black — matches terminal
BG_SURFACE = "#060606"   # barely lifted — focused panels
BG_RAISED = "#0d1117"    # bars, headers, inputs
BG_CHROME = "#060809"    # tab bar and footer — darker chrome, between black and panels


# ─── Staleness helpers ──────────────────────────────────────────────

def _is_today(ts: str) -> bool:
    """True if the ISO timestamp falls on today (local time)."""
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now().astimezone()
        return dt.astimezone(now.tzinfo).date() == now.date()
    except (ValueError, TypeError):
        return False


def _any_session_today(sessions: list) -> bool:
    """True if any session in the list was active today."""
    return any(_is_today(s.last_activity or s.started_at or "") for s in sessions)


# When stale (not active today), shift colors one notch dimmer.
# Semantic colors (activity icons, token magnitude, category) stay unchanged.
_STALE_COLOR = {
    C_LIGHT: C_MID,
    C_MID: C_DIM,
    C_DIM: C_FAINT,
}


# ─── Theme maps ─────────────────────────────────────────────────────

CATEGORY_THEME = {
    Category.WORK: C_CYAN,
    Category.PERSONAL: C_PURPLE,
    Category.META: C_DIM,
}

LINK_TYPE_ICONS = {
    "worktree": "\U0001f333",
    "ticket": "\U0001f3ab",
    "claude-session": "\U0001f916",
    "slack": "\U0001f4ac",
    "file": "\U0001f4c4",
    "url": "\U0001f517",
}
LINK_ORDER = ["worktree", "ticket", "claude-session", "file", "url", "slack"]
LINK_KINDS = list(LINK_ORDER)


# ─── Token coloring ─────────────────────────────────────────────────

def _token_color(total_tokens: int) -> str:
    """Color-code token counts by magnitude for at-a-glance readability."""
    if total_tokens >= 10_000_000:
        return C_RED
    if total_tokens >= 1_000_000:
        return C_ORANGE
    if total_tokens >= 100_000:
        return C_LIGHT
    return C_DIM


def _token_color_markup(text: str, total_tokens: int) -> str:
    """Wrap text in Rich color markup based on token magnitude."""
    color = _token_color(total_tokens)
    return f"[{color}]{text}[/{color}]"


def _colored_tokens(session_or_thread) -> str:
    """Return a Rich-markup token string colored by magnitude."""
    total = getattr(session_or_thread, 'total_tokens', None)
    if total is None:
        total = session_or_thread.total_input_tokens + session_or_thread.total_output_tokens
    color = _token_color(total)
    return f"[{color}]{session_or_thread.tokens_display}[/{color}]"


def _thinking_markup_from_chars(chars: int) -> str:
    """Return Rich-markup for estimated thinking tokens (~chars/4), or empty string."""
    t = chars // 4
    if t == 0:
        return ""
    if t > 1_000_000:
        label = f"~{t / 1_000_000:.1f}M"
    elif t > 1_000:
        label = f"~{t / 1_000:.1f}k"
    else:
        label = f"~{t}"
    return f"[{C_DIM}]{label}[/{C_DIM}]"


def _thinking_markup(session_or_thread) -> str:
    """Return Rich-markup for estimated thinking tokens, or empty string."""
    chars = getattr(session_or_thread, 'total_thinking_chars', 0)
    return _thinking_markup_from_chars(chars)


# ─── Context window bar ────────────────────────────────────────────

def _context_color(pct: float) -> str:
    """Color for context window fill percentage."""
    if pct >= 90:
        return C_RED
    if pct >= 75:
        return C_ORANGE
    if pct >= 50:
        return C_YELLOW
    return C_GREEN


def _context_bar(context_tokens: int, window_size: int = 200_000, width: int = 12) -> str:
    """Render a context window fill bar: ▬▬▬▬▬▬──── 52%

    Uses same glyphs as the tool bar (▬ filled, ─ empty).
    Color transitions green→yellow→orange→red as the window fills.
    Returns empty string if no context data.
    """
    if context_tokens <= 0:
        return ""
    pct = min(100.0, context_tokens / window_size * 100)
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    color = _context_color(pct)
    bar = f"[{color}]{'▬' * filled}[/{color}][{C_DIM}]{'─' * empty}[/{C_DIM}]"
    label = f"[{color}]{pct:.0f}%[/{color}]"
    return f"{bar} {label}"


def _context_bar_compact(context_tokens: int, window_size: int = 200_000, width: int = 6) -> str:
    """Compact context bar for inline session display (no percentage label)."""
    if context_tokens <= 0:
        return ""
    pct = min(100.0, context_tokens / window_size * 100)
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    color = _context_color(pct)
    return f"[{color}]{'▬' * filled}[/{color}][{C_DIM}]{'─' * empty}[/{C_DIM}]"


# ─── Rich Markup Helpers ────────────────────────────────────────────

def _category_markup(cat: Category) -> str:
    c = CATEGORY_THEME[cat]
    return f"[{c}]{cat.value}[/{c}]"


def _link_icon(kind: str) -> str:
    return LINK_TYPE_ICONS.get(kind, "\u2022")


def _ws_indicators(ws: Workstream, tmux_check=None) -> str:
    """Build indicator string for a workstream row."""
    parts = []
    if tmux_check and tmux_check(ws):
        parts.append("\u26a1")
    if ws.is_stale:
        parts.append("\u23f0")
    link_types = set(lnk.kind for lnk in ws.links)
    if link_types:
        icons = "".join(LINK_TYPE_ICONS.get(t, "") for t in LINK_ORDER if t in link_types)
        if icons:
            parts.append(icons)
    return " ".join(parts) if parts else ""


def _short_project(path: str) -> str:
    """Abbreviate project path to just the directory name."""
    cleaned = path.replace(str(Path.home()), "~")
    return Path(cleaned).name or cleaned


def _repo_label(repo_path: str) -> str:
    """Dim repo basename for display in workstream list. Returns Rich markup or empty."""
    if not repo_path:
        return ""
    name = Path(repo_path).name
    return f"[{C_DIM}]{name}[/{C_DIM}]"


def _worktree_label(ws: Workstream) -> str:
    """Plain-text worktree/repo label for a workstream."""
    import os
    for link in ws.links:
        if link.kind == "worktree":
            expanded = os.path.expanduser(link.value).rstrip("/")
            return Path(expanded).name
    if ws.repo_path:
        return Path(ws.repo_path).name
    for link in ws.links:
        if link.kind == "file":
            expanded = os.path.expanduser(link.value).rstrip("/")
            if os.path.isdir(expanded):
                return Path(expanded).name
    return ""


# ─── Worktree coloring (matches dev-workflow-tools p10k prompt) ────

# Same 10-color palette as p10k-worktree.zsh (ANSI 256 codes → hex)
_WORKTREE_COLORS = [
    "#ffd700",  # 220 Yellow/Orange
    "#5fd7ff",  # 81  Bright Cyan
    "#af87ff",  # 141 Purple
    "#87d787",  # 114 Green
    "#d75f5f",  # 167 Red/Pink
    "#ffaf00",  # 214 Orange
    "#87afaf",  # 109 Blue
    "#d7875f",  # 173 Brown/Orange
    "#87d7ff",  # 117 Light Blue
    "#ff5faf",  # 205 Pink
]


def _worktree_color(name: str) -> str:
    """Hash-based color for a worktree name, matching the zsh prompt."""
    import binascii
    h = binascii.crc32(name.encode()) & 0xFFFFFFFF
    return _WORKTREE_COLORS[h % len(_WORKTREE_COLORS)]


def _parse_worktree_display(dirname: str) -> tuple[str, str]:
    """Parse 'repo.branch-name' → (repo, display).

    Display is the ticket ID (e.g. 'UB-6709') if found, else the full dirname.
    """
    if "." in dirname:
        repo, branch = dirname.split(".", 1)
        m = re.match(r"^([A-Z]+-\d+)", branch)
        if m:
            return repo, m.group(1)
        return repo, branch
    return dirname, dirname


def _worktree_styled(ws: Workstream) -> tuple[str, str]:
    """Return (display_text, hex_color) for the worktree column.

    Uses the p10k-worktree color scheme: hash-based color from the worktree name.
    Returns ("", "") if no worktree info available.
    """
    label = _worktree_label(ws)
    if not label:
        return "", ""
    _repo, display = _parse_worktree_display(label)
    color = _worktree_color(label)
    return display, color


def _short_model(model: str) -> str:
    lower = model.lower()
    if "opus" in lower:
        return "opus"
    if "sonnet" in lower:
        return "sonnet"
    if "haiku" in lower:
        return "haiku"
    return model[:12] if model else "\u2014"


# ─── Thread Activity Display ─────────────────────────────────────────

THROBBER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"



def _activity_icon(activity: ThreadActivity, throbber_frame: int = 0, seen: bool = False) -> str:
    """Return a Rich-markup activity indicator."""
    if activity == ThreadActivity.THINKING:
        char = THROBBER_FRAMES[throbber_frame % len(THROBBER_FRAMES)]
        return f"[bold {C_BLUE}]{char}[/bold {C_BLUE}]"
    if activity in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
        color = C_DIM if seen else C_YELLOW
        return f"[{color}]●[/{color}]"
    return f"[{C_DIM}]·[/{C_DIM}]"


def _activity_badge(activity: ThreadActivity, seen: bool = False) -> str:
    """Return a Rich-markup pill/badge for non-idle activity states."""
    return ""


def _best_activity(sessions: list, last_seen: dict[str, str] | None = None) -> ThreadActivity:
    """Return the most urgent activity state across a list of sessions."""
    if not sessions:
        return ThreadActivity.IDLE
    best = ThreadActivity.IDLE
    for s in sessions:
        act = session_activity(s, last_seen)
        if _ACTIVITY_PRIORITY[act] < _ACTIVITY_PRIORITY[best]:
            best = act
    return best


@functools.lru_cache(maxsize=1024)
def _parse_iso(ts: str):
    """Parse ISO timestamp to aware datetime, or None."""
    from datetime import datetime, timezone
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_session_seen(session, last_seen: dict[str, str] | None = None) -> bool:
    """True if user has viewed this session since its last meaningful change.

    Uses last_user_message_at (stable) instead of last_activity, which
    gets bumped by heartbeat/system messages on every tail refresh.
    """
    if not last_seen:
        return False
    seen_ts = last_seen.get(session.session_id)
    if not seen_ts:
        return False
    # Use last_user_message_at as the stable "something happened" marker.
    # Falls back to last_activity for sessions without user messages.
    anchor = getattr(session, 'last_user_message_at', '') or session.last_activity or ''
    if not anchor:
        return False
    seen_dt = _parse_iso(seen_ts)
    anchor_dt = _parse_iso(anchor)
    if not seen_dt or not anchor_dt:
        return False
    return seen_dt >= anchor_dt


def _all_sessions_seen(sessions: list, last_seen: dict[str, str] | None = None) -> bool:
    """True if every 'your turn' session in the list has been seen."""
    if not sessions or not last_seen:
        return False
    for s in sessions:
        act = session_activity(s, last_seen)
        if act in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
            if not _is_session_seen(s, last_seen):
                return False
    return True


# ─── Workstream Option Rendering ─────────────────────────────────────

def _render_ws_option(
    ws: Workstream,
    ws_sessions: list[ClaudeSession],
    last_seen: dict[str, str],
    tmux_check=None,
    line_width: int = 0,
    git_status=None,
) -> Text:
    """Render a workstream as a formatted 3-line OptionList entry.

    Layout:
      {icon} {name}  {indicators}  {branch}
         {category} · {worktree} · {N sess} · {tokens} · {updated}
         {description}
    """
    IND = "     "

    # ── Staleness: not active today → dim two notches ──
    # Stale if: has sessions but none active today, OR no sessions and ws itself not updated today
    if ws_sessions:
        stale = not _any_session_today(ws_sessions)
    else:
        stale = not _is_today(ws.updated_at)
    name_color = C_DIM if stale else C_LIGHT
    name_bold = "" if stale else "bold "
    desc_color = C_FAINT if stale else C_DIM
    meta_dim = C_FAINT if stale else C_DIM

    # ── Activity icon (auto-derived from session state, not manual status) ──
    best = _best_activity(ws_sessions, last_seen)
    all_seen = _all_sessions_seen(ws_sessions, last_seen)
    if best == ThreadActivity.THINKING:
        icon = f"[bold {C_BLUE}]◉[/bold {C_BLUE}]"
    elif best in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
        color = C_DIM if all_seen else C_GREEN
        icon = f"[{color}]●[/{color}]"
    elif ws_sessions:
        icon = f"[{C_DIM}]○[/{C_DIM}]"  # has sessions but idle
    else:
        icon = f"[{C_FAINT}]·[/{C_FAINT}]"  # no sessions

    # ── Line 1: icon + name + indicators + branch ──
    name_esc = _rich_escape(ws.name)
    indicators = _ws_indicators(ws, tmux_check=tmux_check)
    ind_markup = f"  [{meta_dim}]{indicators}[/{meta_dim}]" if indicators else ""

    branch_markup = ""
    if git_status and git_status.branch and not git_status.error:
        branch_name = _rich_escape(git_status.branch)
        if git_status.is_dirty:
            branch_markup = f"  [{C_YELLOW}]{branch_name}*[/{C_YELLOW}]"
        else:
            branch_markup = f"  [{meta_dim}]{branch_name}[/{meta_dim}]"
        if git_status.ahead:
            branch_markup += f"[{C_GREEN}]+{git_status.ahead}[/{C_GREEN}]"
        if git_status.behind:
            branch_markup += f"[{C_RED}]-{git_status.behind}[/{C_RED}]"

    if best == ThreadActivity.THINKING:
        name_markup = f"[bold {C_BLUE}]{name_esc}[/bold {C_BLUE}]"
    else:
        name_markup = f"[{name_bold}{name_color}]{name_esc}[/{name_bold}{name_color}]"
    line1 = f" {icon} {name_markup}{ind_markup}{branch_markup}"

    # ── Line 2: metadata chain separated by dim dots ──
    sep = f" [{C_FAINT}]·[/{C_FAINT}] "
    parts: list[str] = []

    cc = CATEGORY_THEME.get(ws.category, C_DIM)
    parts.append(f"[{cc}]{ws.category.value}[/{cc}]")

    # Ticket key + Jira status (enriched from cache)
    ticket_key = getattr(ws, "ticket_key", "")
    if ticket_key:
        ticket_status = getattr(ws, "ticket_status", "")
        if ticket_status:
            # Color-code Jira status
            ts_lower = ticket_status.lower()
            if "progress" in ts_lower or "review" in ts_lower:
                ts_color = C_CYAN
            elif "done" in ts_lower or "closed" in ts_lower or "resolved" in ts_lower:
                ts_color = C_GREEN
            else:
                ts_color = C_DIM
            parts.append(f"[bold]{_rich_escape(ticket_key)}[/bold] [{ts_color}]{_rich_escape(ticket_status)}[/{ts_color}]")
        else:
            parts.append(f"[bold]{_rich_escape(ticket_key)}[/bold]")

    # MR indicator
    mr_url = getattr(ws, "mr_url", "")
    if mr_url:
        parts.append(f"[{C_PURPLE}]MR[/{C_PURPLE}]")

    # Ticket-solve status badge
    solve_status = getattr(ws, "ticket_solve_status", "")
    if solve_status:
        if solve_status.lower() in ("running", "active"):
            parts.append(f"[{C_YELLOW}]solving[/{C_YELLOW}]")
        elif solve_status.lower() in ("done", "complete"):
            parts.append(f"[{C_GREEN}]solved[/{C_GREEN}]")
        else:
            parts.append(f"[{C_DIM}]solve:{_rich_escape(solve_status)}[/{C_DIM}]")

    wt_text, wt_color = _worktree_styled(ws)
    if wt_text:
        parts.append(f"[{wt_color}]{_rich_escape(wt_text)}[/{wt_color}]")

    sess_count = len(ws_sessions) if ws_sessions else 0
    if sess_count:
        parts.append(f"[{meta_dim}]{sess_count} sess[/{meta_dim}]")

    if ws_sessions:
        total_tokens = sum(s.total_input_tokens + s.total_output_tokens for s in ws_sessions)
        if total_tokens > 0:
            if total_tokens > 1_000_000:
                tk = f"{total_tokens / 1_000_000:.1f}M"
            elif total_tokens > 1_000:
                tk = f"{total_tokens / 1_000:.0f}k"
            else:
                tk = str(total_tokens)
            parts.append(_token_color_markup(tk, total_tokens))
        think_chars = sum(s.total_thinking_chars for s in ws_sessions)
        think_m = _thinking_markup_from_chars(think_chars)
        if think_m:
            parts.append(think_m)

    # Use best session activity time (matches sort order), fall back to updated_at
    if ws_sessions:
        effective_ts = max((s.last_activity or s.started_at or "" for s in ws_sessions), default="")
        if not effective_ts:
            effective_ts = ws.updated_at
    else:
        effective_ts = ws.last_user_activity or ws.updated_at
    time_str = _relative_time(effective_ts)
    time_markup = f"[{C_FAINT}]{time_str}[/{C_FAINT}]"

    left_markup = f"{IND}{sep.join(parts)}"
    if line_width > 20:
        left_plain = re.sub(r"\[/?[^\]]*\]", "", left_markup)
        gap = max(2, line_width - len(left_plain) - len(time_str))
        line2 = f"{left_markup}{' ' * gap}{time_markup}"
    else:
        line2 = f"{left_markup}{sep}{time_markup}"

    # ── Line 3: description snippet (if any) ──
    lines = [line1, line2]
    if ws.description:
        desc = ws.description.replace("\n", " ").strip()
        max_desc = (line_width - 5) if line_width > 20 else 67
        if len(desc) > max_desc:
            desc = desc[:max_desc - 1] + "…"
        lines.append(f"{IND}[{desc_color}]{_rich_escape(desc)}[/{desc_color}]")

    # Trailing blank line for visual separation
    lines.append("")
    return Text.from_markup("\n".join(lines))


# ─── Session Option Rendering ────────────────────────────────────────

def _session_title(session: ClaudeSession, titles: dict[str, str] | None = None) -> str:
    """Best available title for a session: AI title > cached > first message > project."""
    from thread_namer import get_session_title
    from threads import _extract_first_message

    if titles and session.session_id in titles:
        return titles[session.session_id]
    cached = get_session_title(session)
    if cached:
        return cached
    first_msg = _extract_first_message(session)
    if first_msg:
        line = first_msg.split("\n")[0].strip()
        if line.startswith("#"):
            line = line.lstrip("# ")
        if len(line) > 60:
            line = line[:57] + "..."
        return line
    return _short_project(session.project_path)


# Badge plain-text widths (for right-alignment padding calculations)
_BADGE_WIDTHS = {
    ThreadActivity.THINKING: 0,
    ThreadActivity.AWAITING_INPUT: 0,
    ThreadActivity.RESPONSE_READY: 0,
    ThreadActivity.IDLE: 0,
}


_BAR_CATS = [
    ("mutate", C_ORANGE),
    ("bash",   "#8fa0b0"),
    ("read",   C_DIM),
    ("search", C_DIM),
    ("agent",  C_RESOLVED),
]

_BAR_LEGEND_LABELS = [
    ("code", C_ORANGE),
    ("bash", "#8fa0b0"),
    ("read", C_DIM),
    ("agent", C_RESOLVED),
]


def tool_bar_legend() -> str:
    """Return a Rich-markup legend for the tool usage bar."""
    example = "".join(f"[not bold {c}]▬[/not bold {c}]" for _, c in _BAR_LEGEND_LABELS)
    labels = " ".join(f"[not bold {c}]{label}[/not bold {c}]" for label, c in _BAR_LEGEND_LABELS)
    return f"{example} {labels}"


def _tool_bar(tool_counts: dict[str, int], width: int = 6) -> str:
    """Render a mini stacked bar chart of tool usage by category.

    Colors: coding=orange, bash=mid, research=dim, agent=purple.
    Returns empty string if no tool usage.
    """
    total = sum(tool_counts.values())
    if total == 0:
        return ""
    parts: list[str] = []
    used = 0
    for cat, color in _BAR_CATS:
        n = tool_counts.get(cat, 0)
        if n == 0:
            continue
        chars = max(1, round(n / total * width))
        chars = min(chars, width - used)
        if chars > 0:
            parts.append(f"[{color}]{'▬' * chars}[/{color}]")
            used += chars
    if used < width:
        parts.append(f"[{C_DIM}]{'─' * (width - used)}[/{C_DIM}]")
    return "".join(parts)


def _file_touchpoints(files: list[str]) -> str:
    """Render mutated file basenames, with overflow indicator."""
    if not files:
        return ""
    if len(files) <= 3:
        return f"[{C_DIM}]{' '.join(files)}[/{C_DIM}]"
    shown = " ".join(files[:2])
    extra = len(files) - 2
    return f"[{C_DIM}]{shown} +{extra}[/{C_DIM}]"


def _render_session_option(
    s: ClaudeSession, act: ThreadActivity, throbber_frame: int = 0,
    title_width: int = 48, ws_repo_path: str = "", seen: bool = False,
    line_width: int = 0, shelved: bool = False, archived: bool = False,
) -> str:
    """Render a session as a formatted multi-line OptionList entry.

    Layout — 4 lines:
      {icon} {title}                                     {badge}
         {model}  {msgs}  {tokens}  {duration}  {age}    {sid}
         ▏▏▏▏▏░░░  app.py sessions.py +4                {project}
         {role}: {snippet}
    """
    INDENT = "    "  # 4 spaces — nested under title
    if line_width > 0:
        LINE_WIDTH = line_width
        title_width = max(20, LINE_WIDTH - 20)
    else:
        LINE_WIDTH = title_width + 20  # right-alignment anchor

    # ── Shelved: amber-tinted and de-emphasized — overrides stale/committed styling ──
    if shelved:
        icon = f"[{C_SHELF}]⏸[/{C_SHELF}]"
        badge = f"[{C_SHELF}]shelved[/{C_SHELF}]"
        badge_w = 8
        model = _short_model(s.model)
        title_raw = _session_title(s)[:title_width]
        title_pad = " " * (title_width - len(title_raw))
        title_esc = _rich_escape(title_raw)
        sid = s.session_id[:8]
        tokens_plain = s.tokens_display
        msgs_str = f"{s.message_count}↑{s.assistant_message_count}↓"
        duration = s.duration_display
        age_str = s.age
        title_fmt = f"[{C_SHELF}]{title_esc}[/{C_SHELF}]"
        prefix_w = 3
        age_col = f"{age_str:>4}"
        age_w = 2 + 4
        fill = max(2, LINE_WIDTH - prefix_w - title_width - age_w - badge_w)
        line1 = f" {icon} {title_fmt}{title_pad}  [{C_FAINT}]{age_col}[/{C_FAINT}]{' ' * fill}{badge}"
        model_part = "" if model == "opus" else f"[{C_FAINT}]{model:<8}[/{C_FAINT}]"
        tok_pad = " " * max(1, 8 - len(tokens_plain))
        dur_str = f"{duration:<8}" if duration else ""
        dur_len = 8 if duration else 0
        model_len = 0 if model == "opus" else 8
        meta_left_len = 4 + model_len + 10 + 8 + dur_len
        sid_gap = max(2, LINE_WIDTH - meta_left_len - 8)
        line2 = (
            f"{INDENT}{model_part}[{C_FAINT}]{msgs_str:<10}[/{C_FAINT}]"
            f"[{C_FAINT}]{tokens_plain}{tok_pad}{dur_str}[/{C_FAINT}]"
            f"{' ' * sid_gap}[{C_FAINT}]{sid}[/{C_FAINT}]"
        )
        ctx_bar = _context_bar_compact(s.context_tokens, s.context_window_size)
        bar = _tool_bar(s.tool_counts)
        files = _file_touchpoints(s.files_mutated)
        think = _thinking_markup(s)
        left = "  ".join(p for p in (ctx_bar, bar, files, think) if p)
        if not left:
            left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
        line3 = f"{INDENT}{left}"
        if s.last_message_text:
            max_snippet = title_width + 12
            snippet_raw = s.last_message_text[:max_snippet]
            if len(s.last_message_text) > max_snippet:
                snippet_raw += "…"
            role_label = "a" if s.last_message_role == "assistant" else "u"
            line4 = f"{INDENT}[{C_FAINT}]{role_label}: {_rich_escape(snippet_raw)}[/{C_FAINT}]"
        else:
            line4 = f"{INDENT}[{C_FAINT}]─[/{C_FAINT}]"
        return "\n".join([line1, line2, line3, line4])

    # ── Archived: all text collapsed to faint ──
    if archived:
        s_mid = C_FAINT
        s_dim = C_FAINT
        stale = True
    else:
        # ── Staleness: not active today → dim two notches ──
        stale = not _is_today(s.last_activity or s.started_at or "")
        s_mid = C_FAINT if stale else C_MID     # secondary text
        s_dim = C_FAINT if stale else C_DIM     # tertiary text

    # Resolved state: session's last action was a git commit
    # THINKING suppresses it (Claude mid-turn, may do more after the commit).
    # AWAITING_INPUT does NOT suppress it — turn is done, commit is the last act.
    committed = bool(s.last_commit_sha) and act != ThreadActivity.THINKING

    if archived:
        icon = f"[{C_DIM}]·[/{C_DIM}]"
        badge = ""
        badge_w = 0
    elif committed:
        icon = f"[{C_PURPLE}]✓[/{C_PURPLE}]"
        badge = f"[{C_PURPLE}]committed[/{C_PURPLE}]"
        badge_w = 9
    else:
        icon = _activity_icon(act, throbber_frame, seen=seen)
        badge = _activity_badge(act, seen=seen)
        badge_w = _BADGE_WIDTHS.get(act, 0)
    model = _short_model(s.model)
    title_raw = _session_title(s)[:title_width]
    title_pad = " " * (title_width - len(title_raw))  # pad to fixed column
    title_esc = _rich_escape(title_raw)
    sid = s.session_id[:8]
    tokens_plain = s.tokens_display
    msgs_str = f"{s.message_count}↑{s.assistant_message_count}↓"
    duration = s.duration_display
    age_str = s.age

    # Title styling: committed = dim, idle = dim, thinking = cyan, active = bright
    # Stale/archived sessions shift to faint
    if archived:
        title_fmt = f"[{C_DIM}]{title_esc}[/{C_DIM}]"
    elif committed:
        title_fmt = f"[{s_dim}]{title_esc}[/{s_dim}]"
    elif act == ThreadActivity.IDLE:
        title_fmt = f"[{s_dim}]{title_esc}[/{s_dim}]"
    elif act == ThreadActivity.THINKING:
        title_fmt = f"[bold {C_BLUE}]{title_esc}[/bold {C_BLUE}]"
    else:
        title_color = C_DIM if stale else C_LIGHT
        title_fmt = f"[{title_color}]{title_esc}[/{title_color}]"

    # Line 1: " {icon} {title......}  {age}  {badge|sid}"
    # If no badge, sid fills the right slot; title_pad ensures age column is fixed.
    prefix_w = 3  # visible: space + icon + space
    age_col = f"{age_str:>4}"
    age_w = 2 + 4  # "  " + age_col
    if badge:
        fill = max(2, LINE_WIDTH - prefix_w - title_width - age_w - badge_w)
        line1 = f" {icon} {title_fmt}{title_pad}  [{s_dim}]{age_col}[/{s_dim}]{' ' * fill}{badge}"
    else:
        fill = max(2, LINE_WIDTH - prefix_w - title_width - age_w - 8)
        line1 = f" {icon} {title_fmt}{title_pad}  [{s_dim}]{age_col}[/{s_dim}]{' ' * fill}[{C_FAINT}]{sid}[/{C_FAINT}]"

    # Line 2: only show model if not opus; stats dim, tokens colored
    # sid shown here only when a badge occupies line 1's right slot
    model_color = C_FAINT if archived else C_MID
    model_part = "" if model == "opus" else f"[{model_color}]{model:<8}[/{model_color}]"
    tokens_fmt = f"[{C_FAINT}]{tokens_plain}[/{C_FAINT}]" if archived else _colored_tokens(s)
    tok_pad = " " * max(1, 8 - len(tokens_plain))
    dur_str = f"{duration:<8}" if duration else ""
    dur_len = 8 if duration else 0
    model_len = 0 if model == "opus" else 8
    meta_left_len = 4 + model_len + 10 + 8 + dur_len
    sid_gap = max(2, LINE_WIDTH - meta_left_len - 8)

    line2_base = (
        f"{INDENT}{model_part}[{s_dim}]{msgs_str:<10}[/{s_dim}]"
        f"{tokens_fmt}"
        f"[{s_dim}]{tok_pad}"
        f"{dur_str}[/{s_dim}]"
    )
    line2 = line2_base + (f"{' ' * sid_gap}[{C_FAINT}]{sid}[/{C_FAINT}]" if badge else "")

    lines = [line1, line2]

    if not archived:
        # Line 3: context bar + tool usage bar + file touchpoints + thinking est. + project path
        ctx_bar = _context_bar_compact(s.context_tokens, s.context_window_size)
        bar = _tool_bar(s.tool_counts)
        files = _file_touchpoints(s.files_mutated)
        think = _thinking_markup(s)
        proj_label = ""
        if ws_repo_path and s.project_path and s.project_path.rstrip("/") != ws_repo_path.rstrip("/"):
            proj_label = f"[{C_FAINT}]{Path(s.project_path).name}[/{C_FAINT}]"
        left = "  ".join(p for p in (ctx_bar, bar, files, think) if p)
        if not left:
            left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
        if proj_label:
            left_plain = re.sub(r"\[/?[^\]]*\]", "", left)
            proj_plain = re.sub(r"\[/?[^\]]*\]", "", proj_label)
            gap = max(2, LINE_WIDTH - 4 - len(left_plain) - len(proj_plain))
            line3 = f"{INDENT}{left}{' ' * gap}{proj_label}" if left else f"{INDENT}{' ' * (LINE_WIDTH - 4 - len(proj_plain))}{proj_label}"
        else:
            line3 = f"{INDENT}{left}"
        lines.append(line3)

    # Line 4: commit info (if resolved) or last message snippet
    if committed:
        sha_short = s.last_commit_sha[:7]
        max_msg = title_width + 4
        commit_msg = _rich_escape(s.last_commit_summary[:max_msg])
        if len(s.last_commit_summary) > max_msg:
            commit_msg += "…"
        lines.append(f"{INDENT}[{C_PURPLE}]{sha_short}[/{C_PURPLE}] [{s_dim}]{commit_msg}[/{s_dim}]")
    elif s.last_message_text:
        is_user = s.last_message_role == "user"
        if is_user:
            # Show user prompt prominently: black background + white text, up to two lines
            line_chars = max(20, LINE_WIDTH - 4)
            text = s.last_message_text.replace("\n", " ")
            line_a_raw = text[:line_chars]
            remainder = text[line_chars:]
            line_a = _rich_escape(line_a_raw)
            lines.append(f"{INDENT}[white on black]{line_a}[/white on black]")
            if remainder:
                line_b_raw = remainder[:line_chars]
                line_b = _rich_escape(line_b_raw)
                if len(remainder) > line_chars:
                    line_b += "…"
                lines.append(f"{INDENT}[white on black]{line_b}[/white on black]")
        else:
            max_snippet = title_width + 12
            snippet = _rich_escape(s.last_message_text[:max_snippet])
            if len(s.last_message_text) > max_snippet:
                snippet += "…"
            msg_color = C_FAINT if stale else "#3b4048"
            lines.append(f"{INDENT}[italic {msg_color}]{snippet}[/italic {msg_color}]")

    return "\n".join(lines)


# ─── Content search result rendering ─────────────────────────────

def _highlight_snippet(snippet: str, match_ranges: list[tuple[int, int]]) -> str:
    """Apply Rich markup highlighting to matched ranges within a snippet.

    Escapes Rich markup in the snippet text first, then wraps matched
    regions in bold yellow markup.
    """
    escaped = _rich_escape(snippet)
    if not match_ranges:
        return f"[{C_DIM}]{escaped}[/{C_DIM}]"

    # Build highlighted string by splicing markup around matches
    # NOTE: match_ranges index into the original snippet; since _rich_escape
    # only adds backslashes before '[', we need to map indices through the
    # escaped string. Build from escaped chars instead.
    parts: list[str] = []
    prev = 0
    for start, end in match_ranges:
        if start > prev:
            parts.append(f"[{C_DIM}]{_rich_escape(snippet[prev:start])}[/{C_DIM}]")
        parts.append(f"[bold {C_YELLOW}]{_rich_escape(snippet[start:end])}[/bold {C_YELLOW}]")
        prev = end
    if prev < len(snippet):
        parts.append(f"[{C_DIM}]{_rich_escape(snippet[prev:])}[/{C_DIM}]")
    return "".join(parts)


def _render_content_search_result(
    result,  # SessionSearchResult — avoid circular import
    title_width: int = 48,
    ws_repo_path: str = "",
) -> str:
    """Render a content search result as a formatted OptionList entry.

    Same 4-line structure as _render_session_option:
      {✸} {title}                                    {hit count}
         {model}  {msgs}  {duration}  {age}           {sid}
         ▏▏▏▏▏░░░  app.py sessions.py +4              {project}
         {role}: {highlighted snippet}
    """
    INDENT = "    "
    LINE_WIDTH = title_width + 20
    s = result.session
    hit = result.best_hit
    title_raw = _session_title(s)[:title_width]
    title = _rich_escape(title_raw)
    model = _short_model(s.model)
    hits_str = f"{result.hit_count} hit{'s' if result.hit_count != 1 else ''}"
    msgs_str = f"{s.message_count}↑{s.assistant_message_count}↓"
    duration = s.duration_display
    age_str = s.age
    sid = s.session_id[:8]

    # Line 1: search icon + title, hit count right-aligned (badge position)
    prefix_w = 3  # " ✸ "
    fill = max(2, LINE_WIDTH - prefix_w - len(title_raw) - len(hits_str))
    line1 = f" \u2738 {title}{' ' * fill}[{C_YELLOW}]{hits_str}[/{C_YELLOW}]"

    # Line 2: only show model if not opus; stats dim, age mid, sid faint
    model_part = "" if model == "opus" else f"[{C_MID}]{model:<8}[/{C_MID}]"
    dur_str = f"{duration:<8}" if duration else ""
    dur_len = 8 if duration else 0
    model_len = 0 if model == "opus" else 8
    meta_left_len = 4 + model_len + 10 + dur_len + len(age_str)
    sid_gap = max(2, LINE_WIDTH - meta_left_len - 8)

    line2 = (
        f"{INDENT}{model_part}[{C_DIM}]{msgs_str:<10}"
        f"{dur_str}[/{C_DIM}]"
        f"[{C_MID}]{age_str}[/{C_MID}]"
        f"{' ' * sid_gap}[{C_FAINT}]{sid}[/{C_FAINT}]"
    )

    lines = [line1, line2]

    # Line 3: context bar + tool usage bar + file touchpoints + project path
    ctx_bar = _context_bar_compact(s.context_tokens, s.context_window_size)
    bar = _tool_bar(s.tool_counts)
    files = _file_touchpoints(s.files_mutated)
    proj_label = ""
    if ws_repo_path and s.project_path and s.project_path.rstrip("/") != ws_repo_path.rstrip("/"):
        proj_label = f"[{C_FAINT}]{Path(s.project_path).name}[/{C_FAINT}]"

    left = "  ".join(p for p in (ctx_bar, bar, files) if p)
    if not left:
        left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
    if proj_label:
        left_plain = re.sub(r"\[/?[^\]]*\]", "", left)
        proj_plain = re.sub(r"\[/?[^\]]*\]", "", proj_label)
        gap = max(2, LINE_WIDTH - 4 - len(left_plain) - len(proj_plain))
        line3 = f"{INDENT}{left}{' ' * gap}{proj_label}" if left else f"{INDENT}{' ' * (LINE_WIDTH - 4 - len(proj_plain))}{proj_label}"
    else:
        line3 = f"{INDENT}{left}"
    lines.append(line3)

    # Snippet line — role colored
    is_user = hit.role == "user"
    highlighted = _highlight_snippet(hit.snippet, hit.match_ranges)
    prefix = f"[{C_MID}]you:[/{C_MID}] " if is_user else ""
    lines.append(f"{INDENT}{prefix}{highlighted}")
    return "\n".join(lines)


# ─── Notification Feed rendering ─────────────────────────────────

def _render_notification_option(notif, max_width: int = 40) -> str:
    """Render a notification as a formatted OptionList entry.

    notif is a notifications.Notification (imported lazily to avoid circular deps).
    Color-coded by freshness: fresh=green, recent=orange, old/dismissed=dim.
    """
    freshness = notif.freshness
    if notif.dismissed:
        color = C_DIM
        icon = "·"
    elif freshness == "fresh":
        color = C_GREEN
        icon = "●"
    elif freshness == "recent":
        color = C_ORANGE
        icon = "●"
    else:
        color = C_DIM
        icon = "○"

    msg = _rich_escape(notif.message[:max_width])
    if len(notif.message) > max_width:
        msg += "…"

    age = _relative_time(notif.timestamp)
    title_esc = _rich_escape(notif.title)
    line1 = f" [{color}]{icon}[/{color}]  [{color}]{msg}[/{color}]"
    line2 = f"      [{C_DIM}]{age} · {title_esc}[/{C_DIM}]"
    return f"{line1}\n{line2}"


def _render_notified_session_option(
    s: ClaudeSession, act: ThreadActivity, notif=None,
    throbber_frame: int = 0, title_width: int = 48,
    ws_repo_path: str = "", seen: bool = False,
    line_width: int = 0,
) -> str:
    """Render a session in elevated/notified style.

    When notif is provided, line 4 shows the notification message.
    When notif is None (your-turn session without notification), line 4
    shows the tail snippet text in green.

    Layout — 4 lines (same height as _render_session_option):
      {icon} {title}                                     {badge}
         {model}  {msgs}  {tokens}  {duration}  {age}    {sid}
         ▏▏▏▏▏░░░  app.py sessions.py +4                {project}
         {notification_message or green snippet}          {notif_age}
    """
    INDENT = "    "
    if line_width > 0:
        LINE_WIDTH = line_width
        title_width = max(20, LINE_WIDTH - 20)
    else:
        LINE_WIDTH = title_width + 20

    icon = _activity_icon(act, throbber_frame, seen=seen)
    badge = _activity_badge(act, seen=seen)
    badge_w = _BADGE_WIDTHS.get(act, 0)
    model = _short_model(s.model)
    title_raw = _session_title(s)[:title_width]
    title_esc = _rich_escape(title_raw)
    sid = s.session_id[:8]
    tokens_plain = s.tokens_display
    msgs_str = f"{s.message_count}↑{s.assistant_message_count}↓"
    duration = s.duration_display
    age_str = s.age

    # Title always bright for notified sessions
    title_fmt = f"[{C_LIGHT}]{title_esc}[/{C_LIGHT}]"

    # Line 1: badge if present, else sid fills the right slot
    prefix_w = 3
    if badge:
        fill = max(2, LINE_WIDTH - prefix_w - len(title_raw) - badge_w)
        line1 = f" {icon} {title_fmt}{' ' * fill}{badge}"
    else:
        fill = max(2, LINE_WIDTH - prefix_w - len(title_raw) - 8)
        line1 = f" {icon} {title_fmt}{' ' * fill}[{C_FAINT}]{sid}[/{C_FAINT}]"

    # Line 2: sid shown here only when badge occupies line 1's right slot
    model_part = "" if model == "opus" else f"[{C_MID}]{model:<8}[/{C_MID}]"
    tokens_fmt = _colored_tokens(s)
    tok_pad = " " * max(1, 8 - len(tokens_plain))
    dur_str = f"{duration:<8}" if duration else ""
    dur_len = 8 if duration else 0
    model_len = 0 if model == "opus" else 8
    meta_left_len = 4 + model_len + 10 + 8 + dur_len + len(age_str)
    sid_gap = max(2, LINE_WIDTH - meta_left_len - 8)
    line2_base = (
        f"{INDENT}{model_part}[{C_DIM}]{msgs_str:<10}[/{C_DIM}]"
        f"{tokens_fmt}"
        f"[{C_DIM}]{tok_pad}"
        f"{dur_str}[/{C_DIM}]"
        f"[{C_MID}]{age_str}[/{C_MID}]"
    )
    line2 = line2_base + (f"{' ' * sid_gap}[{C_FAINT}]{sid}[/{C_FAINT}]" if badge else "")

    # Line 3: tool bar + file touchpoints (same as normal)
    bar = _tool_bar(s.tool_counts)
    files = _file_touchpoints(s.files_mutated)
    proj_label = ""
    if ws_repo_path and s.project_path and s.project_path.rstrip("/") != ws_repo_path.rstrip("/"):
        proj_label = f"[{C_FAINT}]{Path(s.project_path).name}[/{C_FAINT}]"

    left = "  ".join(p for p in (bar, files) if p)
    if not left:
        left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
    if proj_label:
        left_plain = re.sub(r"\[/?[^\]]*\]", "", left)
        proj_plain = re.sub(r"\[/?[^\]]*\]", "", proj_label)
        gap = max(2, LINE_WIDTH - 4 - len(left_plain) - len(proj_plain))
        line3 = f"{INDENT}{left}{' ' * gap}{proj_label}" if left else f"{INDENT}{' ' * (LINE_WIDTH - 4 - len(proj_plain))}{proj_label}"
    else:
        line3 = f"{INDENT}{left}"

    # Line 4: notification message, or green snippet for un-notified your-turn sessions
    if notif is not None:
        freshness = notif.freshness
        if notif.dismissed:
            notif_color = C_DIM
        elif freshness == "fresh":
            notif_color = C_GREEN
        elif freshness == "recent":
            notif_color = C_ORANGE
        else:
            notif_color = C_DIM

        notif_age = _relative_time(notif.timestamp)
        msg_raw = notif.message.replace("\n", " ").strip()
        max_msg = LINE_WIDTH - 4 - len(notif_age) - 2
        if len(msg_raw) > max_msg:
            msg_raw = msg_raw[:max_msg - 1] + "…"
        msg_esc = _rich_escape(msg_raw)
        age_gap = max(2, LINE_WIDTH - 4 - len(msg_raw) - len(notif_age))
        line4 = f"{INDENT}[{notif_color}]{msg_esc}[/{notif_color}]{' ' * age_gap}[{C_DIM}]{notif_age}[/{C_DIM}]"
    else:
        # No notification — show tail snippet in green
        snippet_color = C_DIM if seen else C_GREEN
        if s.last_message_text:
            is_user = s.last_message_role == "user"
            prefix = f"[{C_MID}]you:[/{C_MID}] " if is_user else ""
            prefix_len = 5 if is_user else 0  # len("you: ")
            max_snippet = title_width + 12 - prefix_len
            snippet_raw = s.last_message_text[:max_snippet]
            if len(s.last_message_text) > max_snippet:
                snippet_raw += "…"
            snippet_esc = _rich_escape(snippet_raw)
            msg_color = C_MID if is_user else snippet_color
            line4 = f"{INDENT}{prefix}[italic {msg_color}]{snippet_esc}[/italic {msg_color}]"
        else:
            line4 = f"{INDENT}[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"

    return "\n".join([line1, line2, line3, line4])


# ─── Quiet session separator ──────────────────────────────────────

def QUIET_SEPARATOR_LABEL(width: int = 60) -> str:
    return f"[{C_DIM}]{'─' * width}[/{C_DIM}]"

def SHELVED_SEPARATOR_LABEL(width: int = 60) -> str:
    prefix = "⏸ shelved "
    return f"[{C_SHELF}]{prefix}{'─' * max(2, width - len(prefix))}[/{C_SHELF}]"

def THINKING_SEPARATOR_LABEL(width: int = 60) -> str:
    prefix = "◉ thinking "
    dashes = max(2, width - len(prefix))
    return f"[bold {C_BLUE}]◉[/bold {C_BLUE}] [{C_BLUE}]thinking {'─' * dashes}[/{C_BLUE}]"


# ─── Todo rendering ────────────────────────────────────────────────

TODO_UNDONE_ICON = "\u25cb"   # ○
TODO_DONE_ICON = "\u2713"     # ✓
TODO_ARCHIVED_ICON = "\u25cc"  # ◌
TODO_CRYSTAL_ICON = "\u25c8"  # ◈


def _render_todo_option(item: TodoItem, is_archived: bool = False) -> str:
    """Render a todo item as a formatted OptionList entry."""
    is_crystal = getattr(item, "origin", "manual") == "crystallized"
    text_esc = _rich_escape(item.text)
    if is_archived:
        icon = TODO_ARCHIVED_ICON
        text_fmt = f"[{C_DIM}]{text_esc}[/{C_DIM}]"
    elif item.done:
        icon = TODO_DONE_ICON
        text_fmt = f"[{C_DIM}]{text_esc}[/{C_DIM}]"
    elif is_crystal:
        icon = TODO_CRYSTAL_ICON
        text_fmt = f"[bold {C_GOLD}]{text_esc}[/bold {C_GOLD}]"
    else:
        icon = TODO_UNDONE_ICON
        text_fmt = text_esc
    icon_color = C_GOLD if is_crystal and not is_archived else (C_DIM if (is_archived or item.done) else C_LIGHT)
    ctx_hint = f" [{C_GOLD}]\u2726[/{C_GOLD}]" if is_crystal and item.context else (f" [{C_DIM}]+ctx[/{C_DIM}]" if item.context else "")
    age = _relative_time(item.created_at)
    line1 = f" [{icon_color}]{icon}[/{icon_color}]  {text_fmt}{ctx_hint}"
    line2 = f"      [{C_DIM}]{age}[/{C_DIM}]"
    return f"{line1}\n{line2}"
