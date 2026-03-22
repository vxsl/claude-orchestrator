"""User configuration — keybinding overrides from ~/.claude-orchestrator/config.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from textual.binding import Binding


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
    # View switching
    "next_view": ("tab", "Tab", True, True),
    "prev_view": ("shift+tab", "", False, True),
    # Actions
    "add": ("a", "Add", True, False),
    "brain_dump": ("b", "Brain", False, False),
    "cycle_status": ("s", "Status", True, False),
    "cycle_status_back": ("S", "Status←", False, False),
    "spawn": ("c", "Spawn", True, False),
    "repo_spawn": ("C", "Repo", True, False),
    "resume": ("r", "Resume", True, False),
    "link_action": ("L", "Link", True, False),
    "quick_note": ("n", "", False, False),
    "edit_notes": ("e", "", False, False),
    "rename": ("E", "", False, False),
    "open_links": ("o", "", False, False),
    "toggle_archive": ("u", "Archive", False, False),
    "delete_item": ("d", "", False, False),
    # Filters
    "filter('all')": ("1", "", False, False),
    "filter('work')": ("2", "", False, False),
    "filter('personal')": ("3", "", False, False),
    "filter('active')": ("4", "", False, False),
    "filter('stale')": ("5", "", False, False),
    "filter('archived')": ("6", "", False, False),
    "search": ("slash", "/", True, False),
    # Sort
    "sort('status')": ("f1", "", False, False),
    "sort('updated')": ("f2", "", False, False),
    "sort('created')": ("f3", "", False, False),
    "sort('category')": ("f4", "", False, False),
    "sort('name')": ("f5", "", False, False),
    # Tabs
    "close_tab": ("ctrl+w", "", False, True),
    # Command palette
    "command_palette": ("colon", ":", True, False),
    # Other
    "toggle_preview": ("p", "", False, False),
    "refresh": ("R", "", False, False),
    "help": ("question_mark", "?", True, False),
    "quit": ("q", "Quit", True, False),
}


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


def build_app_bindings() -> list[Binding]:
    """Build the main app BINDINGS list with user overrides applied."""
    overrides = _user_overrides()
    bindings = []
    for action, (default_keys, desc, show, priority) in DEFAULT_KEYS.items():
        keys = overrides.get(action, default_keys)
        bindings.append(Binding(keys, action, desc, show=show, priority=priority))
    return bindings
