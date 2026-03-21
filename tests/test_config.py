"""Tests for config.py — keybinding overrides."""

from unittest.mock import patch
from config import build_app_bindings, get_key, DEFAULT_KEYS


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
