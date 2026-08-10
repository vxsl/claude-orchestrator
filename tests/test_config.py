"""Tests for config.py — keybinding overrides."""

from unittest.mock import patch
from config import (
    build_app_bindings, get_key, get_session_key, key_label, key_set,
    DEFAULT_KEYS, SESSION_KEYS,
)


class TestDefaults:
    def test_all_defaults_have_keys(self):
        for action, (keys, desc, show, priority) in DEFAULT_KEYS.items():
            assert keys, f"Action {action} has empty default keys"

    def test_build_app_bindings_returns_list(self):
        bindings = build_app_bindings()
        assert isinstance(bindings, list)
        assert len(bindings) == len(DEFAULT_KEYS)

    def test_toggle_archive_default_is_u(self):
        assert get_key("toggle_archive") == "u"

    def test_build_bindings_has_toggle_archive(self):
        bindings = build_app_bindings()
        actions = [b.action for b in bindings]
        assert "toggle_archive" in actions
        # Old separate archive/unarchive should not be present
        assert "archive" not in actions
        assert "unarchive" not in actions


class TestOverrides:
    def test_override_replaces_default(self):
        with patch("config.load_config", return_value={"keybindings": {"toggle_archive": "x"}}):
            assert get_key("toggle_archive") == "x"

    def test_override_in_bindings(self):
        with patch("config.load_config", return_value={"keybindings": {"quit": "Q"}}):
            bindings = build_app_bindings()
            quit_binding = [b for b in bindings if b.action == "quit"][0]
            assert quit_binding.key == "Q"

    def test_unset_override_uses_default(self):
        with patch("config.load_config", return_value={"keybindings": {}}):
            assert get_key("toggle_archive") == "u"

    def test_missing_config_uses_defaults(self):
        with patch("config.load_config", return_value={}):
            bindings = build_app_bindings()
            assert len(bindings) == len(DEFAULT_KEYS)


class TestSessionKeys:
    """Session-screen keys: consumed directly by each engine's session
    view, never fed to build_app_bindings (there's no matching action_*)."""

    def test_all_session_defaults_have_keys(self):
        for action, (keys, desc) in SESSION_KEYS.items():
            assert keys, f"Session action {action} has empty default keys"

    def test_auto_mode_default_is_f9_not_ctrl_y(self):
        # ctrl+y was hit by accident (it's yank in claude's input line).
        assert get_session_key("toggle_auto_mode") == "f9"

    def test_session_keys_not_app_bindings(self):
        actions = [b.action for b in build_app_bindings()]
        for action in SESSION_KEYS:
            assert action not in actions

    def test_session_key_override(self):
        with patch("config.load_config",
                   return_value={"keybindings": {"toggle_auto_mode": "f12"}}):
            assert get_session_key("toggle_auto_mode") == "f12"

    def test_unknown_session_action_is_empty(self):
        assert get_session_key("nope") == ""


class TestKeyHelpers:
    def test_key_set_splits_and_strips(self):
        assert key_set("f9, ctrl+y ,") == {"f9", "ctrl+y"}

    def test_key_set_empty(self):
        assert key_set("") == set()

    def test_key_label_forms(self):
        assert key_label("f9") == "F9"
        assert key_label("f12") == "F12"
        assert key_label("ctrl+y") == "C-y"
        assert key_label("f9,ctrl+y") == "F9"  # first key wins
        assert key_label("u") == "u"


class TestPanelNavigation:
    def test_panel_nav_not_in_bindings(self):
        """Panel nav is handled via on_key, not the binding system."""
        bindings = build_app_bindings()
        actions = [b.action for b in bindings]
        assert "next_panel" not in actions
        assert "prev_panel" not in actions
