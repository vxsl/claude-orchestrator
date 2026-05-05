"""Per-project trust list for skipping Claude permission prompts.

A trusted project is a directory under which orch-launched Claude sessions
are invoked with --dangerously-skip-permissions. Trust is stored as a list
of absolute paths; a session cwd is trusted if it equals or descends from
any trusted path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "claude-orchestrator"
TRUST_PATH = CONFIG_DIR / "trusted-projects.json"


def _normalize(p: str | os.PathLike) -> str:
    return str(Path(p).expanduser().resolve())


def _load() -> list[str]:
    if not TRUST_PATH.exists():
        return []
    try:
        data = json.loads(TRUST_PATH.read_text())
    except Exception:
        return []
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return []
    return [str(p) for p in paths if isinstance(p, str)]


def _save(paths: list[str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TRUST_PATH.write_text(
        json.dumps({"paths": sorted(set(paths))}, indent=2) + "\n"
    )


def list_trusted() -> list[str]:
    return _load()


def is_trusted(cwd: str | os.PathLike) -> bool:
    if not cwd:
        return False
    try:
        c = Path(_normalize(cwd))
    except Exception:
        return False
    for trusted in _load():
        try:
            t = Path(trusted)
        except Exception:
            continue
        if c == t or t in c.parents:
            return True
    return False


def add(path: str | os.PathLike) -> bool:
    """Add a path. Returns True if newly added, False if already trusted."""
    norm = _normalize(path)
    paths = _load()
    if norm in paths:
        return False
    paths.append(norm)
    _save(paths)
    return True


def remove(path: str | os.PathLike) -> bool:
    """Remove a path. Returns True if removed, False if not present."""
    norm = _normalize(path)
    paths = _load()
    if norm not in paths:
        return False
    paths.remove(norm)
    _save(paths)
    return True


def toggle(path: str | os.PathLike) -> bool:
    """Toggle trust. Returns True if now trusted, False if no longer trusted."""
    norm = _normalize(path)
    paths = _load()
    if norm in paths:
        paths.remove(norm)
        _save(paths)
        return False
    paths.append(norm)
    _save(paths)
    return True
