"""Regression: every core module must import without syntax or import errors."""

import importlib
import pytest

MODULES = [
    "actions",
    "app",
    "brain",
    "claude_session_screen",
    "cli",
    "config",
    "description_refresher",
    "models",
    "notifications",
    "rendering",
    "screens",
    "session_bridge",
    "sessions",
    "state",
    "thread_namer",
    "threads",
    "widgets",
    "workstream_synthesizer",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)
