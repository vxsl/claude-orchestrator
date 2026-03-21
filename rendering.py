"""Rendering helpers — color palette, Rich markup, display formatting.

Pure functions with no Textual dependency. Used by screens, app, and state.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from models import (
    Category, Status, Workstream,
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
        f"[/{C_DIM}]{tokens}[{C_DIM}] tok · {s.age}[/{C_DIM}]"
    )
    return f"{line1}\n{line2}"
