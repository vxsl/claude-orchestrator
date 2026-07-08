"""Guard: shared modules must stay importable without Textual.

The tui engine migration (see MIGRATION.md) requires every module shared
between the Textual app and the new engine to be framework-free. Importing
one of these must not pull textual into sys.modules — an engine conditional
or a top-level textual import in shared code is a migration regression.

Each module is checked in a fresh interpreter so this test is immune to
import-order effects from the rest of the suite.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PURE_MODULES = [
    "state",
    "rendering",
    "actions",
    "sessions",
    "threads",
    "models",
    "config",
    "auto_mode",
    "notifications",
    "cleanup",
    "term_host",
    "vterm_backend",
    "tui.keys",
    "tui.layout",
    "tui.frame",
    "tui.term",
    "tui.view",
    "tui.app",
    "tui.widgets",
    "tui.testing",
]


@pytest.mark.parametrize("module", PURE_MODULES)
def test_module_imports_without_textual(module):
    code = (
        f"import {module}; import sys; "
        "sys.exit(1 if 'textual' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import {module} pulled textual into sys.modules "
        f"(or failed to import: {result.stderr.strip()})"
    )
