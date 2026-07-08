"""Fuzzy-picker modals (P4-A): LinkSessionView, RepoPickerView,
WorkstreamPickerView — thin FuzzyModalView subclasses of the Textual
originals (screens.py LinkSessionScreen / RepoPickerScreen /
WorkstreamPickerScreen), item builders ported verbatim.
"""

from __future__ import annotations

from pathlib import Path

from rendering import C_DIM, C_GREEN, _rich_escape

from .modals import FuzzyModalView

SENTINEL_NEW = "__new__"  # matches screens._SENTINEL_NEW


def _ws_item(ws) -> tuple[str, str]:
    return (ws.id, f"● {_rich_escape(ws.name)}  [{C_DIM}]{ws.category.value}[/{C_DIM}]")


class LinkSessionView(FuzzyModalView):
    """Pick a workstream to link a session to. Dismisses with the
    Workstream (None when cancelled or the pick vanished)."""

    def __init__(self, store, session) -> None:
        self._store = store
        super().__init__(title=f"Link session: {session.display_name}")

    def _get_items(self):
        return [_ws_item(ws) for ws in self._store.active]

    def _on_selected(self, item_id) -> None:
        self.dismiss(self._store.get(item_id))


class RepoPickerView(FuzzyModalView):
    """fzf-style repo picker. Dismisses with the repo path string."""

    def __init__(self, repos: list[str], ws_counts: dict[str, int]) -> None:
        self.all_repos = repos
        self.ws_counts = ws_counts
        super().__init__(title="Select Repository")

    def _get_items(self):
        home_str = str(Path.home())
        # Repos with workstreams first, then alphabetical
        with_ws = sorted(
            (r for r in self.all_repos if self.ws_counts.get(r, 0) > 0),
            key=lambda r: Path(r).name.lower(),
        )
        without_ws = sorted(
            (r for r in self.all_repos if self.ws_counts.get(r, 0) == 0),
            key=lambda r: Path(r).name.lower(),
        )
        items = []
        for repo in with_ws + without_ws:
            name = Path(repo).name
            short = repo.replace(home_str, "~")
            n_ws = self.ws_counts.get(repo, 0)
            if n_ws > 0:
                label = f"[bold]{name}[/bold]  [dim]({n_ws} ws)[/dim]  [{C_DIM}]{short}[/{C_DIM}]"
            else:
                label = f"[{C_DIM}]{name}  {short}[/{C_DIM}]"
            items.append((repo, label))
        return items


class WorkstreamPickerView(FuzzyModalView):
    """Pick a workstream for a repo, or create a new one.

    Dismisses with a Workstream, SENTINEL_NEW ("__new__") when "Create
    new" was picked, or None on cancel.
    """

    def __init__(self, workstreams: list, repo_path: str) -> None:
        self.workstreams = workstreams
        self.repo_path = repo_path
        super().__init__(title=f"Workstreams in {Path(repo_path).name}")

    def _get_items(self):
        items = [_ws_item(ws) for ws in self.workstreams]
        items.append((SENTINEL_NEW, f"[{C_GREEN}]+ Create new workstream[/{C_GREEN}]"))
        return items

    def _on_selected(self, item_id) -> None:
        if item_id == SENTINEL_NEW:
            self.dismiss(SENTINEL_NEW)
            return
        for ws in self.workstreams:
            if ws.id == item_id:
                self.dismiss(ws)
                return
        self.dismiss(None)
