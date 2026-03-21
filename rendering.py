"""Rendering helpers — color palette, Rich markup, display formatting.

Pure functions with no Textual dependency. Used by screens, app, and state.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from models import (
    Category, Status, TodoItem, Workstream,
    STATUS_ICONS, STATUS_ORDER,
    _relative_time,
)
from sessions import ClaudeSession
from threads import Thread, ThreadActivity, session_activity


# ─── Color Palette (matching fzedit / jira-fzf) ─────────────────────
# ANSI 256-color equivalents as hex for the mellow, desaturated palette

C_BLUE = "#87afaf"       # 109 — borders, structural
C_PURPLE = "#af87ff"     # 141 — headings, personal category
C_CYAN = "#5fd7ff"       # 81  — active states, work category
C_GREEN = "#87d787"      # 114 — success, done
C_YELLOW = "#ffd75f"     # 221 — warnings, queued
C_ORANGE = "#d7875f"     # 173 — secondary accents
C_RED = "#d75f5f"        # 167 — errors, blocked
C_LIGHT = "#a0a0a0"      # soft foreground text
C_DIM = "#585858"        # subdued — present but not loud

# ─── Background Palette (hardcoded to bypass Textual's auto-tinting) ──
BG_BASE = "#141414"      # deepest — screen background
BG_SURFACE = "#1a1a1a"   # slightly lifted — tables, panes
BG_RAISED = "#222222"    # bars, headers, inputs


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

_ACTIVITY_PRIORITY = {
    ThreadActivity.THINKING: 0,
    ThreadActivity.AWAITING_INPUT: 1,
    ThreadActivity.RESPONSE_FRESH: 2,
    ThreadActivity.RESPONSE_READY: 3,
    ThreadActivity.IDLE: 4,
}


def _activity_icon(activity: ThreadActivity, throbber_frame: int = 0) -> str:
    """Return a Rich-markup activity indicator. Animated for THINKING."""
    if activity == ThreadActivity.THINKING:
        frame = THROBBER_FRAMES[throbber_frame % len(THROBBER_FRAMES)]
        return f"[bold {C_CYAN}]{frame}[/bold {C_CYAN}]"
    if activity == ThreadActivity.AWAITING_INPUT:
        return f"[{C_YELLOW}]◉[/{C_YELLOW}]"
    if activity == ThreadActivity.RESPONSE_FRESH:
        return f"[bold {C_GREEN}]●[/bold {C_GREEN}]"
    if activity == ThreadActivity.RESPONSE_READY:
        return f"[{C_ORANGE}]●[/{C_ORANGE}]"
    return f"[{C_DIM}]·[/{C_DIM}]"


def _activity_badge(activity: ThreadActivity) -> str:
    """Return a Rich-markup pill/badge for non-idle activity states."""
    if activity == ThreadActivity.THINKING:
        return f"[italic {C_CYAN}]thinking…[/italic {C_CYAN}]"
    if activity == ThreadActivity.AWAITING_INPUT:
        return f"[{C_YELLOW}]your turn[/{C_YELLOW}]"
    if activity == ThreadActivity.RESPONSE_FRESH:
        return f"[bold {C_GREEN}]done[/bold {C_GREEN}]"
    if activity == ThreadActivity.RESPONSE_READY:
        return f"[{C_ORANGE}]done[/{C_ORANGE}]"
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


def _render_session_option(
    s: ClaudeSession, act: ThreadActivity, throbber_frame: int = 0,
    title_width: int = 48,
) -> str:
    """Render a session as a formatted two-line OptionList entry."""
    icon = _activity_icon(act, throbber_frame)
    badge = _activity_badge(act)
    model = _short_model(s.model)
    title = _session_title(s)[:title_width]
    tokens = _colored_tokens(s)

    if act == ThreadActivity.IDLE:
        title_fmt = f"[{C_DIM}]{title}[/{C_DIM}]"
    elif act in (ThreadActivity.THINKING, ThreadActivity.AWAITING_INPUT):
        title_fmt = f"[bold]{title}[/bold]"
    elif act == ThreadActivity.RESPONSE_FRESH:
        title_fmt = f"[bold {C_GREEN}]{title}[/bold {C_GREEN}]"
    elif act == ThreadActivity.RESPONSE_READY:
        title_fmt = f"[{C_ORANGE}]{title}[/{C_ORANGE}]"
    else:
        title_fmt = title

    pad = " " * max(1, title_width + 2 - len(title))
    badge_part = f"{pad}{badge}" if badge else ""
    line1 = f" {icon}  {title_fmt}{badge_part}"
    line2 = (
        f"      [{C_DIM}]{model} · {s.message_count} msgs · "
        f"[/{C_DIM}]{tokens}[{C_DIM}] · {s.age}[/{C_DIM}]"
    )
    # Third line: last message snippet — styled by activity state
    if s.last_message_text:
        max_snippet = title_width + 10
        snippet = s.last_message_text[:max_snippet]
        if len(s.last_message_text) > max_snippet:
            snippet += "…"
        is_user = s.last_message_role == "user"
        role_tag = "you" if is_user else "claude"
        # Color the snippet to reflect what matters:
        # - thinking: cyan (active) — show what claude is working on
        # - awaiting input / fresh response: green — claude answered, your turn
        # - response ready (stale unread): orange — been waiting for you
        # - idle: dim — just context
        if act == ThreadActivity.THINKING:
            snip_color = C_CYAN
        elif act in (ThreadActivity.AWAITING_INPUT, ThreadActivity.RESPONSE_FRESH):
            snip_color = C_GREEN
        elif act == ThreadActivity.RESPONSE_READY:
            snip_color = C_ORANGE
        else:
            snip_color = C_DIM
        role_style = f"[{C_DIM}]" if is_user else f"[italic {snip_color}]"
        role_end = f"[/{C_DIM}]" if is_user else f"[/italic {snip_color}]"
        line3 = f"      [{C_DIM}]{role_tag}:[/{C_DIM}] {role_style}{snippet}{role_end}"
        return f"{line1}\n{line2}\n{line3}"
    return f"{line1}\n{line2}"


# ─── Todo rendering ────────────────────────────────────────────────

TODO_UNDONE_ICON = "\u25cb"   # ○
TODO_DONE_ICON = "\u25cf"     # ●
TODO_ARCHIVED_ICON = "\u25cc"  # ◌


def _render_todo_option(item: TodoItem, is_archived: bool = False) -> str:
    """Render a todo item as a formatted OptionList entry."""
    if is_archived:
        icon = TODO_ARCHIVED_ICON
        text_fmt = f"[{C_DIM}]{item.text}[/{C_DIM}]"
    elif item.done:
        icon = TODO_DONE_ICON
        text_fmt = f"[{C_GREEN}]{item.text}[/{C_GREEN}]"
    else:
        icon = TODO_UNDONE_ICON
        text_fmt = item.text
    ctx_hint = f" [{C_DIM}]+ctx[/{C_DIM}]" if item.context else ""
    age = _relative_time(item.created_at)
    line1 = f" [{C_DIM if is_archived else C_LIGHT}]{icon}[/{C_DIM if is_archived else C_LIGHT}]  {text_fmt}{ctx_hint}"
    line2 = f"      [{C_DIM}]{age}[/{C_DIM}]"
    return f"{line1}\n{line2}"
