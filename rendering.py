"""Rendering helpers — color palette, Rich markup, display formatting.

Pure functions with no Textual dependency. Used by screens, app, and state.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

def _rich_escape(text: str) -> str:
    """Escape ALL [ characters for Rich markup.

    rich.markup.escape() only escapes sequences that look like valid tags,
    but arbitrary text with [ can still break when embedded inside markup
    (e.g. "[#585858][Binding(key=...)[/#585858]" confuses the parser).
    """
    return text.replace("[", r"\[")

from models import (
    Category, Status, TodoItem, Workstream,
    STATUS_ICONS, STATUS_ORDER,
    _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity, _ACTIVITY_PRIORITY


# ─── Color Palette (high contrast on true black) ─────────────────────
# Matches alacritty GitHub Dark theme — bright text on black background

C_BLUE = "#58a6ff"       # structural, borders
C_PURPLE = "#d2a8ff"     # headings, personal category
C_CYAN = "#56d4dd"       # active states, work category
C_GREEN = "#56d364"      # success, done
C_YELLOW = "#e3b341"     # warnings, queued
C_ORANGE = "#d7875f"     # secondary accents
C_RED = "#ffa198"        # errors, blocked
C_GOLD = "#e3b341"       # crystallized todos, distilled knowledge
C_LIGHT = "#e6edf3"      # primary text (terminal foreground)
C_MID = "#b1bac4"        # secondary text (terminal normal white)
C_DIM = "#6e7681"        # subdued (terminal bright-black)
C_FAINT = "#484f58"      # near-invisible — IDs, decorative

# ─── Background Palette ──────────────────────────────────────────────
BG_BASE = "#000000"      # true black — matches terminal
BG_SURFACE = "#060606"   # barely lifted — focused panels
BG_RAISED = "#0d1117"    # bars, headers, inputs


# ─── Theme maps ─────────────────────────────────────────────────────

STATUS_THEME = {
    Status.QUEUED: C_DIM,
    Status.IN_PROGRESS: C_CYAN,
    Status.AWAITING_REVIEW: C_PURPLE,
    Status.DONE: C_GREEN,
    Status.BLOCKED: C_RED,
}

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


# ─── View Mode ──────────────────────────────────────────────────────

class ViewMode(str, Enum):
    WORKSTREAMS = "workstreams"
    SESSIONS = "sessions"
    ARCHIVED = "archived"


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


# ─── Rich Markup Helpers ────────────────────────────────────────────

def _status_markup(status: Status) -> str:
    c = STATUS_THEME[status]
    return f"[{c}]{STATUS_ICONS[status]} {status.value}[/{c}]"


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
    if ws.is_stale and ws.status != Status.DONE:
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
    import re
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
        return f"[bold {C_CYAN}]◉[/bold {C_CYAN}]"
    if activity in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
        color = C_DIM if seen else C_YELLOW
        return f"[{color}]●[/{color}]"
    return f"[{C_DIM}]·[/{C_DIM}]"


def _activity_badge(activity: ThreadActivity, seen: bool = False) -> str:
    """Return a Rich-markup pill/badge for non-idle activity states.

    When seen=True, "your turn" renders dim to indicate the user has
    already visited this session since its last activity.
    """
    if activity == ThreadActivity.THINKING:
        return f"[italic {C_CYAN}]thinking…[/italic {C_CYAN}]"
    if activity in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_READY):
        color = C_DIM if seen else C_YELLOW
        return f"[{color}]your turn[/{color}]"
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
    """True if user has viewed this session since its last activity."""
    if not last_seen:
        return False
    seen_ts = last_seen.get(session.session_id)
    if not seen_ts or not session.last_activity:
        return False
    seen_dt = _parse_iso(seen_ts)
    activity_dt = _parse_iso(session.last_activity)
    if not seen_dt or not activity_dt:
        return False
    return seen_dt >= activity_dt


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
    ThreadActivity.THINKING: 9,       # "thinking…"
    ThreadActivity.AWAITING_INPUT: 9, # "your turn"
    ThreadActivity.RESPONSE_READY: 9, # "your turn"
    ThreadActivity.IDLE: 0,
}


_BAR_CATS = [
    ("mutate", C_ORANGE),
    ("bash",   C_LIGHT),
    ("read",   C_DIM),
    ("search", C_DIM),
    ("agent",  C_PURPLE),
]

_BAR_LEGEND_LABELS = [
    ("coding", C_ORANGE),
    ("bash",   C_LIGHT),
    ("research", C_DIM),
    ("agents", C_PURPLE),
]


def tool_bar_legend() -> str:
    """Return a Rich-markup legend for the tool usage bar."""
    parts = [f"[{c}]▄ {label}[/{c}]" for label, c in _BAR_LEGEND_LABELS]
    return "  ".join(parts)


def _tool_bar(tool_counts: dict[str, int], width: int = 6) -> str:
    """Render a mini stacked bar chart of tool usage by category.

    Colors: coding=orange, bash=light, research=dim, agent=purple.
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
) -> str:
    """Render a session as a formatted multi-line OptionList entry.

    Layout — 4 lines:
      {icon} {title}                                     {badge}
         {model}  {msgs}  {duration}  {age}              {sid}
         ▏▏▏▏▏░░░  app.py sessions.py +4                {project}
         {role}: {snippet}
    """
    INDENT = "    "  # 4 spaces — nested under title
    LINE_WIDTH = title_width + 20  # right-alignment anchor

    icon = _activity_icon(act, throbber_frame, seen=seen)
    badge = _activity_badge(act, seen=seen)
    badge_w = _BADGE_WIDTHS.get(act, 0)
    model = _short_model(s.model)
    title_raw = _session_title(s)[:title_width]
    title_esc = _rich_escape(title_raw)
    sid = s.session_id[:8]
    msgs_str = f"{s.message_count} msgs"
    duration = s.duration_display
    age_str = s.age

    # Title styling: idle sessions are dimmed, everything else is normal weight.
    if act == ThreadActivity.IDLE:
        title_fmt = f"[{C_DIM}]{title_esc}[/{C_DIM}]"
    else:
        title_fmt = f"[{C_LIGHT}]{title_esc}[/{C_LIGHT}]"

    # Line 1: " {icon} {title}          {badge}"
    prefix_w = 3  # visible: space + icon + space
    if badge:
        fill = max(2, LINE_WIDTH - prefix_w - len(title_raw) - badge_w)
        line1 = f" {icon} {title_fmt}{' ' * fill}{badge}"
    else:
        line1 = f" {icon} {title_fmt}"

    # Line 2: only show model if not opus; stats dim, age mid, sid faint
    model_part = "" if model == "opus" else f"[{C_BLUE}]{model:<8}[/{C_BLUE}]"
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

    # Line 3: tool usage bar + file touchpoints + project path (always rendered)
    bar = _tool_bar(s.tool_counts)
    files = _file_touchpoints(s.files_mutated)
    proj_label = ""
    if ws_repo_path and s.project_path and s.project_path.rstrip("/") != ws_repo_path.rstrip("/"):
        proj_label = f"[{C_FAINT}]{Path(s.project_path).name}[/{C_FAINT}]"

    left = "  ".join(p for p in (bar, files) if p)
    if not left:
        left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
    if proj_label:
        import re
        left_plain = re.sub(r"\[/?[^\]]*\]", "", left)
        proj_plain = re.sub(r"\[/?[^\]]*\]", "", proj_label)
        gap = max(2, LINE_WIDTH - 4 - len(left_plain) - len(proj_plain))
        line3 = f"{INDENT}{left}{' ' * gap}{proj_label}" if left else f"{INDENT}{' ' * (LINE_WIDTH - 4 - len(proj_plain))}{proj_label}"
    else:
        line3 = f"{INDENT}{left}"
    lines.append(line3)

    # Line 4: last message snippet — role colored, snippet in mid
    if s.last_message_text:
        max_snippet = title_width + 12
        snippet = _rich_escape(s.last_message_text[:max_snippet])
        if len(s.last_message_text) > max_snippet:
            snippet += "…"
        is_user = s.last_message_role == "user"
        role_tag = "you" if is_user else "claude"
        role_color = C_GREEN if is_user else C_PURPLE
        lines.append(f"{INDENT}[{role_color}]{role_tag}:[/{role_color}] [{C_DIM}]{snippet}[/{C_DIM}]")

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
    msgs_str = f"{s.message_count} msgs"
    duration = s.duration_display
    age_str = s.age
    sid = s.session_id[:8]

    # Line 1: search icon + title, hit count right-aligned (badge position)
    prefix_w = 3  # " ✸ "
    fill = max(2, LINE_WIDTH - prefix_w - len(title_raw) - len(hits_str))
    line1 = f" \u2738 {title}{' ' * fill}[{C_YELLOW}]{hits_str}[/{C_YELLOW}]"

    # Line 2: only show model if not opus; stats dim, age mid, sid faint
    model_part = "" if model == "opus" else f"[{C_BLUE}]{model:<8}[/{C_BLUE}]"
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

    # Line 3: tool usage bar + file touchpoints + project path (always rendered)
    bar = _tool_bar(s.tool_counts)
    files = _file_touchpoints(s.files_mutated)
    proj_label = ""
    if ws_repo_path and s.project_path and s.project_path.rstrip("/") != ws_repo_path.rstrip("/"):
        proj_label = f"[{C_FAINT}]{Path(s.project_path).name}[/{C_FAINT}]"

    left = "  ".join(p for p in (bar, files) if p)
    if not left:
        left = f"[{C_FAINT}]{'─' * 6}[/{C_FAINT}]"
    if proj_label:
        import re
        left_plain = re.sub(r"\[/?[^\]]*\]", "", left)
        proj_plain = re.sub(r"\[/?[^\]]*\]", "", proj_label)
        gap = max(2, LINE_WIDTH - 4 - len(left_plain) - len(proj_plain))
        line3 = f"{INDENT}{left}{' ' * gap}{proj_label}" if left else f"{INDENT}{' ' * (LINE_WIDTH - 4 - len(proj_plain))}{proj_label}"
    else:
        line3 = f"{INDENT}{left}"
    lines.append(line3)

    # Snippet line — role colored
    role_tag = "you" if hit.role == "user" else "claude"
    role_color = C_GREEN if hit.role == "user" else C_PURPLE
    highlighted = _highlight_snippet(hit.snippet, hit.match_ranges)
    lines.append(f"{INDENT}[{role_color}]{role_tag}:[/{role_color}] {highlighted}")
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


# ─── Todo rendering ────────────────────────────────────────────────

TODO_UNDONE_ICON = "\u25cb"   # ○
TODO_DONE_ICON = "\u25cf"     # ●
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
        text_fmt = f"[{C_GREEN}]{text_esc}[/{C_GREEN}]"
    elif is_crystal:
        icon = TODO_CRYSTAL_ICON
        text_fmt = f"[bold {C_GOLD}]{text_esc}[/bold {C_GOLD}]"
    else:
        icon = TODO_UNDONE_ICON
        text_fmt = text_esc
    icon_color = C_GOLD if is_crystal and not is_archived else (C_DIM if is_archived else C_LIGHT)
    ctx_hint = f" [{C_GOLD}]\u2726[/{C_GOLD}]" if is_crystal and item.context else (f" [{C_DIM}]+ctx[/{C_DIM}]" if item.context else "")
    tag = f" [{C_GOLD}]crystallized[/{C_GOLD}]" if is_crystal and not is_archived else ""
    age = _relative_time(item.created_at)
    line1 = f" [{icon_color}]{icon}[/{icon_color}]  {text_fmt}{ctx_hint}"
    line2 = f"      [{C_DIM}]{age}[/{C_DIM}]{tag}"
    return f"{line1}\n{line2}"
