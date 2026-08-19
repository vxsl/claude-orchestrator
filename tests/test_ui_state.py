"""Tests for ui_state — the persisted "where you were" slice of UI state."""

import json

import pytest

from models import Store, Workstream
from state import AppState, TabManager
from ui_state import (
    HOME_TAB,
    MAX_RESTORED_TABS,
    SESSIONS_TAB,
    UiState,
    apply_ui_state,
    capture_ui_state,
    load_ui_state,
    resolve_active_tab,
    restorable_tabs,
    save_ui_state,
)


@pytest.fixture
def state(tmp_path):
    return AppState(Store(path=tmp_path / "data.json"))


def _ws(state, name):
    ws = Workstream(name=name, description=f"{name} desc")
    state.store.add(ws)
    return ws


# ── round trip ──

def test_save_load_round_trip(tmp_path):
    path = tmp_path / "ui-state.json"
    ui = UiState(
        filter_mode="stale",
        sort_mode="name",
        preview_visible=False,
        home_ws_id="abc123",
        tab_ws_ids=["abc123", "def456"],
        active_tab_id="def456",
    )
    assert save_ui_state(ui, path) is True
    assert load_ui_state(path) == ui


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_ui_state(tmp_path / "nope.json") == UiState()


def test_load_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "ui-state.json"
    path.write_text("{not json at all")
    assert load_ui_state(path) == UiState()


def test_save_leaves_no_temp_file(tmp_path):
    path = tmp_path / "ui-state.json"
    save_ui_state(UiState(), path)
    assert [p.name for p in tmp_path.iterdir()] == ["ui-state.json"]


def test_save_unwritable_dir_returns_false(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    assert save_ui_state(UiState(), blocker / "sub" / "ui-state.json") is False


# ── tolerant parsing ──

def test_unknown_modes_fall_back_to_defaults(tmp_path):
    path = tmp_path / "ui-state.json"
    path.write_text(json.dumps({"filter_mode": "bogus", "sort_mode": "bogus"}))
    ui = load_ui_state(path)
    assert (ui.filter_mode, ui.sort_mode) == ("all", "updated")


def test_wrong_types_are_ignored(tmp_path):
    path = tmp_path / "ui-state.json"
    path.write_text(json.dumps({
        "preview_visible": "yes",     # not a bool
        "home_ws_id": 42,             # not a str
        "tab_ws_ids": "abc",          # not a list
        "active_tab_id": None,
    }))
    ui = load_ui_state(path)
    assert ui == UiState()


def test_tab_ids_deduped_and_capped(tmp_path):
    path = tmp_path / "ui-state.json"
    ids = [f"ws{i}" for i in range(MAX_RESTORED_TABS + 5)]
    path.write_text(json.dumps({"tab_ws_ids": ids + [ids[0]]}))
    restored = load_ui_state(path).tab_ws_ids
    assert len(restored) == MAX_RESTORED_TABS
    assert len(set(restored)) == len(restored)
    assert restored[-1] == ids[-1]  # newest tabs win


def test_non_dict_payload_returns_defaults(tmp_path):
    path = tmp_path / "ui-state.json"
    path.write_text("[1, 2, 3]")
    assert load_ui_state(path) == UiState()


# ── capture ──

def test_capture_records_modes_and_tabs(state):
    a, b = _ws(state, "alpha"), _ws(state, "beta")
    state.filter_mode = "stale"
    state.sort_mode = "name"
    state.preview_visible = False
    tabs = TabManager()
    tabs.open_tab(a.id, a.name)
    tabs.open_tab(b.id, b.name)

    ui = capture_ui_state(state, tabs, home_ws_id=a.id)

    assert ui.filter_mode == "stale"
    assert ui.sort_mode == "name"
    assert ui.preview_visible is False
    assert ui.home_ws_id == a.id
    assert ui.tab_ws_ids == [a.id, b.id]
    assert ui.active_tab_id == b.id  # open_tab activates what it opened


def test_capture_on_home_and_sessions_tabs(state):
    tabs = TabManager()
    assert capture_ui_state(state, tabs, None).active_tab_id == HOME_TAB
    tabs.switch_to(1)
    assert capture_ui_state(state, tabs, None).active_tab_id == SESSIONS_TAB


def test_capture_caps_tab_list(state):
    ids = [_ws(state, f"ws{i}").id for i in range(MAX_RESTORED_TABS + 3)]
    tabs = TabManager()
    for i, ws_id in enumerate(ids):
        tabs.open_tab(ws_id, f"ws{i}")
    ui = capture_ui_state(state, tabs, None)
    assert ui.tab_ws_ids == ids[-MAX_RESTORED_TABS:]


# ── apply / restore ──

def test_apply_pushes_modes_onto_state(state):
    apply_ui_state(
        UiState(filter_mode="archived", sort_mode="created", preview_visible=False),
        state,
    )
    assert state.filter_mode == "archived"
    assert state.sort_mode == "created"
    assert state.preview_visible is False


def test_restorable_tabs_skips_deleted_workstreams(state):
    a, b = _ws(state, "alpha"), _ws(state, "beta")
    ui = UiState(tab_ws_ids=[a.id, "gone-forever", b.id])
    assert [w.id for w in restorable_tabs(ui, state)] == [a.id, b.id]


def test_restorable_tabs_keeps_archived_workstreams(state):
    a = _ws(state, "alpha")
    state.store.archive(a.id)
    assert [w.id for w in restorable_tabs(UiState(tab_ws_ids=[a.id]), state)] == [a.id]


def test_resolve_active_tab():
    assert resolve_active_tab(UiState(active_tab_id="ws1"), ["ws1", "ws2"]) == "ws1"
    assert resolve_active_tab(UiState(active_tab_id="gone"), ["ws1"]) == HOME_TAB
    assert resolve_active_tab(UiState(active_tab_id=SESSIONS_TAB), []) == SESSIONS_TAB
    assert resolve_active_tab(UiState(), ["ws1"]) == HOME_TAB


def test_full_cycle_capture_save_load_restore(state, tmp_path):
    """The end-to-end shape the app relies on: quit, relaunch, land back."""
    a, b = _ws(state, "alpha"), _ws(state, "beta")
    state.filter_mode = "stale"
    tabs = TabManager()
    tabs.open_tab(a.id, a.name)
    tabs.open_tab(b.id, b.name)
    tabs.switch_to_id(a.id)
    path = tmp_path / "ui-state.json"
    save_ui_state(capture_ui_state(state, tabs, home_ws_id=b.id), path)

    fresh = AppState(state.store)
    fresh.filter_mode = "all"
    ui = load_ui_state(path)
    apply_ui_state(ui, fresh)
    restored = restorable_tabs(ui, fresh)
    new_tabs = TabManager()
    for ws in restored:
        new_tabs.open_tab(ws.id, ws.name)
    new_tabs.switch_to_id(resolve_active_tab(ui, [w.id for w in restored]))

    assert fresh.filter_mode == "stale"
    assert ui.home_ws_id == b.id
    assert [t.ws_id for t in new_tabs.tabs if t.ws_id] == [a.id, b.id]
    assert new_tabs.active_tab.ws_id == a.id
