"""Persisted UI state — "where you were" across orch restarts.

orch used to reopen cold: home tab, cursor on the first row, filter/sort back
to defaults, every workstream tab you had open gone.  This module persists the
small set of UI choices that make a relaunch feel like resuming — filter mode,
sort mode, preview visibility, whether the embedded git (tig) panes are on, the
highlighted workstream, the open tab set plus which tab was active, and the
claude session each tab had open — to
~/.cache/claude-orchestrator/ui-state.json.

Engine-neutral (no Textual, no tui imports): both engines capture and apply
through the same functions.  Every I/O path is best-effort — a missing,
truncated, or hand-mangled file falls back to defaults rather than blocking
startup, and a failed save is never allowed to take the app down on exit.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

UI_STATE_FILE = Path.home() / ".cache" / "claude-orchestrator" / "ui-state.json"

# Accepted values, mirroring AppState.filter_mode / sort_mode. A file holding
# anything else (older schema, hand-edit, a mode we since renamed) falls back
# to the default rather than putting the app in an unreachable state.
FILTER_MODES = ("all", "stale", "archived")
SORT_MODES = ("updated", "activity", "created", "category", "name")

HOME_TAB = "home"
SESSIONS_TAB = "current_sessions"

# Restoring an unbounded tab list would spawn a DetailView per tab at startup
# (each with its own liveness worker). Keep the tail; the oldest tabs drop.
MAX_RESTORED_TABS = 12


@dataclass
class UiState:
    """The persisted slice of UI state. Defaults match a cold AppState."""

    filter_mode: str = "all"
    sort_mode: str = "updated"
    preview_visible: bool = True
    # Embedded tig panes (Detail lower panel, session sidebar). Off is a
    # real preference, not a fallback: two tig children per screen poll git
    # every few seconds, which is visible CPU on a big repo.
    git_panes_visible: bool = True
    home_ws_id: str | None = None
    tab_ws_ids: list[str] = field(default_factory=list)
    active_tab_id: str = HOME_TAB
    # ws_id -> the claude session that tab had open. Seeds the engines'
    # _tab_active_session map, so activating the tab re-attaches that
    # session exactly like a tab switch does (dead tmux sessions no-op).
    tab_sessions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "filter_mode": self.filter_mode,
            "sort_mode": self.sort_mode,
            "preview_visible": self.preview_visible,
            "git_panes_visible": self.git_panes_visible,
            "home_ws_id": self.home_ws_id,
            "tab_ws_ids": list(self.tab_ws_ids),
            "active_tab_id": self.active_tab_id,
            "tab_sessions": dict(self.tab_sessions),
        }

    @classmethod
    def from_dict(cls, data: object) -> "UiState":
        """Build from parsed JSON, coercing anything unexpected to a default."""
        if not isinstance(data, dict):
            return cls()
        ui = cls()

        filter_mode = data.get("filter_mode")
        if isinstance(filter_mode, str) and filter_mode in FILTER_MODES:
            ui.filter_mode = filter_mode

        sort_mode = data.get("sort_mode")
        if isinstance(sort_mode, str) and sort_mode in SORT_MODES:
            ui.sort_mode = sort_mode

        preview = data.get("preview_visible")
        if isinstance(preview, bool):
            ui.preview_visible = preview

        git_panes = data.get("git_panes_visible")
        if isinstance(git_panes, bool):
            ui.git_panes_visible = git_panes

        home_ws_id = data.get("home_ws_id")
        if isinstance(home_ws_id, str) and home_ws_id:
            ui.home_ws_id = home_ws_id

        tab_ws_ids = data.get("tab_ws_ids")
        if isinstance(tab_ws_ids, list):
            seen: set[str] = set()
            for ws_id in tab_ws_ids:
                if isinstance(ws_id, str) and ws_id and ws_id not in seen:
                    seen.add(ws_id)
                    ui.tab_ws_ids.append(ws_id)
            ui.tab_ws_ids = ui.tab_ws_ids[-MAX_RESTORED_TABS:]

        active = data.get("active_tab_id")
        if isinstance(active, str) and active:
            ui.active_tab_id = active

        sessions = data.get("tab_sessions")
        if isinstance(sessions, dict):
            ui.tab_sessions = {
                k: v for k, v in sessions.items()
                if isinstance(k, str) and isinstance(v, str) and k and v
            }

        return ui


def ui_state_path() -> Path:
    """Where UI state lives.

    ORCH_UI_STATE_PATH redirects it — the tests use that so a run never
    reads or clobbers the developer's real state, and it pairs with
    ORCH_STORE_PATH for driving a second orch against a copied store.
    """
    override = os.environ.get("ORCH_UI_STATE_PATH")
    return Path(override).expanduser() if override else UI_STATE_FILE


def load_ui_state(path: Path | None = None) -> UiState:
    """Read persisted UI state. Never raises — defaults on any problem."""
    target = path or ui_state_path()
    try:
        return UiState.from_dict(json.loads(target.read_text()))
    except (OSError, ValueError):
        return UiState()


def save_ui_state(state: UiState, path: Path | None = None) -> bool:
    """Write UI state atomically. Returns True on success, never raises.

    Called from exit paths and from a periodic flush, so a read-only cache
    dir or a full disk must degrade to "state isn't remembered", not a crash.
    """
    target = path or ui_state_path()
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def capture_ui_state(
    app_state,
    tabs,
    home_ws_id: str | None = None,
    tab_sessions: dict[str, str] | None = None,
) -> UiState:
    """Snapshot the restorable UI state from AppState + TabManager.

    `home_ws_id` (the workstream under the home cursor) and `tab_sessions`
    (ws_id -> open claude session id) live in engine widgets and the screen
    stack, so they come in as parameters.
    """
    active = tabs.active_tab
    open_ws_ids = [t.ws_id for t in tabs.tabs if t.ws_id][-MAX_RESTORED_TABS:]
    return UiState(
        filter_mode=app_state.filter_mode,
        sort_mode=app_state.sort_mode,
        preview_visible=app_state.preview_visible,
        git_panes_visible=app_state.git_panes_visible,
        home_ws_id=home_ws_id,
        tab_ws_ids=open_ws_ids,
        active_tab_id=(active.ws_id or active.id) if active else HOME_TAB,
        # Only for tabs that are actually open — a closed tab's session is
        # not something we should silently re-attach on the next launch.
        tab_sessions={
            ws_id: sid
            for ws_id, sid in (tab_sessions or {}).items()
            if ws_id in set(open_ws_ids) and sid
        },
    )


def apply_ui_state(ui: UiState, app_state) -> None:
    """Push the persisted view options onto AppState.

    Tabs and the home cursor need engine widgets, so they stay with the
    caller (restorable_tabs / resolve_active_tab feed that part).
    """
    app_state.filter_mode = ui.filter_mode
    app_state.sort_mode = ui.sort_mode
    app_state.preview_visible = ui.preview_visible
    app_state.git_panes_visible = ui.git_panes_visible


def restorable_tabs(ui: UiState, app_state) -> list:
    """The saved tabs whose workstream still exists, in saved order.

    Workstreams deleted since the last run are dropped silently; archived
    ones still resolve (Store.get spans both lists), so a tab you left open
    on an archived workstream comes back.
    """
    out = []
    for ws_id in ui.tab_ws_ids[-MAX_RESTORED_TABS:]:
        ws = app_state.get_ws(ws_id)
        if ws is not None:
            out.append(ws)
    return out


def restorable_tab_sessions(ui: UiState, restored_ws_ids: list[str]) -> dict[str, str]:
    """Saved per-tab sessions, narrowed to the tabs that actually reopened.

    The engines merge this into their tab-switch resume map, so a restored
    tab re-attaches its session the moment it becomes active — and a session
    whose tmux is gone simply doesn't come back (the resume path checks).
    """
    keep = set(restored_ws_ids)
    return {ws_id: sid for ws_id, sid in ui.tab_sessions.items() if ws_id in keep}


def resolve_active_tab(ui: UiState, restored_ws_ids: list[str]) -> str:
    """The tab id to activate after restore, or "home" if it's unavailable.

    Guards the case where the active tab's workstream was deleted between
    runs — better to land on home than on a tab that can't be shown.
    """
    active = ui.active_tab_id
    if active == SESSIONS_TAB:
        return SESSIONS_TAB
    if active in restored_ws_ids:
        return active
    return HOME_TAB
