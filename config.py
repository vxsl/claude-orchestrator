"""User configuration — keybinding overrides from ~/.claude-orchestrator/config.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path


CONFIG_DIR = Path.home() / ".claude-orchestrator"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Default keybinding map: action -> (keys, description, show, priority)
DEFAULT_KEYS: dict[str, tuple[str, str, bool, bool]] = {
    # Navigation
    "cursor_down": ("j,down,ctrl+n", "Down", False, False),
    "cursor_up": ("k,up,ctrl+p", "Up", False, False),
    "cursor_top": ("g", "Top", False, False),
    "cursor_bottom": ("G", "Bottom", False, False),
    "half_page_down": ("ctrl+d", "½PgDn", False, False),
    "half_page_up": ("ctrl+u", "½PgUp", False, False),
    "select_item": ("enter,l", "Open", True, False),
    # Tab switching
    "next_tab": ("ctrl+b", "›Tab", True, True),
    "prev_tab": ("ctrl+x", "Tab‹", True, True),
    # Actions
    "add": ("a", "Add", True, False),
    "brain_dump": ("b", "Brain", False, False),
    # s/S freed up (was: cycle status — removed, status is auto-derived now)
    "spawn": ("c", "Spawn", True, False),
    "repo_spawn": ("C", "Repo", True, False),
    "resume": ("r", "Resume", True, False),
    "link_action": ("W", "Link", True, False),
    "quick_note": ("n", "", False, False),
    "edit_notes": ("e", "", False, False),
    "rename": ("E", "", False, False),
    "open_links": ("o", "", False, False),
    "toggle_archive": ("u", "Archive", False, False),
    "delete_item": ("d", "", False, False),
    "toggle_trust": ("t", "", False, False),
    # Filters
    "filter('all')": ("1", "", False, False),
    "filter('stale')": ("2", "", False, False),
    "filter('archived')": ("3", "", False, False),
    "search": ("slash", "/", True, False),
    # Sort
    "sort('activity')": ("f1", "", False, False),
    "sort('updated')": ("f2", "", False, False),
    "sort('created')": ("f3", "", False, False),
    "sort('category')": ("f4", "", False, False),
    "sort('name')": ("f5", "", False, False),
    # Tabs
    "close_tab": ("x", "", False, False),
    # Dev-workflow
    "ship": ("P", "", False, False),
    "ticket": ("T", "", False, False),
    "branches": ("B", "", False, False),
    "rr": ("ctrl+g", "", False, False),
    # Command palette
    "command_palette": ("colon", ":", True, False),
    # Other
    "toggle_preview": ("p", "", False, False),
    # Embedded tig panes. f8 rather than a letter: it also has to work on the
    # session screen, where letters go to claude.
    "toggle_git_panes": ("f8", "", False, False),
    "refresh": ("R", "", False, False),
    "help": ("question_mark", "?", True, False),
    "quit": ("q", "Quit", True, False),
}


# Keys scoped to the Claude session screen: action -> (keys, description).
# These are NOT app-level bindings (build_app_bindings only walks
# DEFAULT_KEYS) — each engine's session view consumes them directly. They
# honour the same [keybindings] section of config.toml.
SESSION_KEYS: dict[str, tuple[str, str]] = {
    # f9, deliberately not ctrl+y: starting auto mode spawns a coordinator
    # plus implementer sessions, and ctrl+y is muscle-memory yank inside
    # claude's input line — it was being hit by accident.
    "toggle_auto_mode": ("f9", "Auto mode"),
}


def get_session_key(action: str) -> str:
    """Key(s) for a session-screen action, respecting user overrides."""
    overrides = _user_overrides()
    if action in overrides:
        return overrides[action]
    default = SESSION_KEYS.get(action)
    return default[0] if default else ""


def key_set(keys: str) -> set[str]:
    """Split a comma-separated key spec into individual key names."""
    return {k.strip() for k in keys.split(",") if k.strip()}


def key_label(keys: str) -> str:
    """Display form of the first key in a spec: ctrl+y → C-y, f9 → F9."""
    first = next(iter(keys.split(",")), "").strip()
    if first.startswith("ctrl+"):
        return "C-" + first[5:]
    if first[:1] == "f" and first[1:].isdigit():
        return first.upper()
    return first


def load_config() -> dict:
    """Load config.toml, returning empty dict on missing/invalid file."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _user_overrides() -> dict[str, str]:
    """Return action -> keys mapping from [keybindings] section."""
    cfg = load_config()
    return cfg.get("keybindings", {})


def get_key(action: str) -> str:
    """Get the key(s) for an action, respecting user overrides."""
    overrides = _user_overrides()
    if action in overrides:
        return overrides[action]
    default = DEFAULT_KEYS.get(action)
    return default[0] if default else ""


def build_app_bindings() -> list:
    """Build the main app BINDINGS list with user overrides applied.

    Textual-only adapter; the import is local so this module stays
    importable without Textual (the tui engine consumes DEFAULT_KEYS
    and get_key directly).
    """
    from textual.binding import Binding

    overrides = _user_overrides()
    bindings = []
    for action, (default_keys, desc, show, priority) in DEFAULT_KEYS.items():
        keys = overrides.get(action, default_keys)
        bindings.append(Binding(keys, action, desc, show=show, priority=priority))
    return bindings


# ── Auto-mode quota gate ──────────────────────────────────────────────
# Auto mode holds the loop when a Claude subscription limit is exhausted
# and resumes once it resets (see auto_mode.AutoMode._await_quota).
# Configure under [auto_mode] in config.toml:
#
#   [auto_mode]
#   quota_pause = true          # hold the loop at the threshold
#   quota_percent = 100         # percent of a window that counts as spent
#   quota_kinds = ["session", "weekly_all"]
#
# ORCH_AUTO_QUOTA_PAUSE=0/1 overrides `quota_pause` for a single run.

AUTO_QUOTA_DEFAULTS: dict = {
    "enabled": True,
    "percent": 100.0,
    # Mirrors usage.DEFAULT_BLOCKING_KINDS; duplicated as plain data so
    # config stays importable without pulling in the network module.
    "kinds": ("session", "weekly_all"),
}


# ── Git-status poller ─────────────────────────────────────────────────
# `git status` on a large worktree costs real CPU and orch tracks every
# worktree it has seen, so the poller works through them on a per-cycle
# wall-clock budget (see state.GitStatusPoller). Configure under
# [git_status] in config.toml:
#
#   [git_status]
#   enabled = true          # false stops the poller entirely (no dirty badges)
#   interval = 30           # seconds between cycles
#   budget_seconds = 1.5    # wall-clock spent per cycle
#   max_batch = 24          # ceiling for cheap repos, where the budget never binds
#
# ORCH_GIT_STATUS=0/1 overrides `enabled` for a single run.

GIT_STATUS_DEFAULTS: dict = {
    "enabled": True,
    "interval": 30.0,
    # Duplicated as plain data so config stays importable without state.py.
    "budget_seconds": 1.5,
    "max_batch": 24,
}


def git_status_config() -> dict:
    """Resolved poller settings: {'enabled', 'interval', 'budget_seconds', 'max_batch'}.

    Precedence: ORCH_GIT_STATUS env var > [git_status] in config.toml >
    GIT_STATUS_DEFAULTS. A malformed or non-positive value falls back to the
    default rather than raising — a typo must not stop orch from starting, and
    must not turn the budget into "poll nothing" or "poll everything".
    """
    import os

    section = load_config().get("git_status", {})
    if not isinstance(section, dict):
        section = {}

    enabled = bool(section.get("enabled", GIT_STATUS_DEFAULTS["enabled"]))
    env = os.environ.get("ORCH_GIT_STATUS")
    if env is not None and env.strip() != "":
        enabled = env.strip().lower() not in ("0", "false", "no", "off")

    def _positive(key, cast):
        try:
            value = cast(section.get(key, GIT_STATUS_DEFAULTS[key]))
        except (TypeError, ValueError):
            return cast(GIT_STATUS_DEFAULTS[key])
        return value if value > 0 else cast(GIT_STATUS_DEFAULTS[key])

    return {
        "enabled": enabled,
        "interval": _positive("interval", float),
        "budget_seconds": _positive("budget_seconds", float),
        "max_batch": _positive("max_batch", int),
    }


def auto_quota_config() -> dict:
    """Resolved quota-gate settings: {'enabled', 'percent', 'kinds'}.

    Precedence: ORCH_AUTO_QUOTA_PAUSE env var > [auto_mode] in
    config.toml > AUTO_QUOTA_DEFAULTS. Malformed values fall back to the
    default rather than raising — a typo in config.toml must not stop
    auto mode from starting.
    """
    import os

    section = load_config().get("auto_mode", {})
    if not isinstance(section, dict):
        section = {}

    enabled = bool(section.get("quota_pause", AUTO_QUOTA_DEFAULTS["enabled"]))
    env = os.environ.get("ORCH_AUTO_QUOTA_PAUSE")
    if env is not None and env.strip() != "":
        enabled = env.strip().lower() not in ("0", "false", "no", "off")

    try:
        percent = float(section.get("quota_percent", AUTO_QUOTA_DEFAULTS["percent"]))
    except (TypeError, ValueError):
        percent = AUTO_QUOTA_DEFAULTS["percent"]

    raw_kinds = section.get("quota_kinds", AUTO_QUOTA_DEFAULTS["kinds"])
    if isinstance(raw_kinds, (list, tuple)):
        kinds = tuple(str(k) for k in raw_kinds if str(k).strip())
    else:
        kinds = AUTO_QUOTA_DEFAULTS["kinds"]
    if not kinds:
        kinds = AUTO_QUOTA_DEFAULTS["kinds"]

    return {"enabled": enabled, "percent": percent, "kinds": kinds}
