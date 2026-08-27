"""HelpView — port of screens.HelpScreen (P4-B; "session" added in P5).

Context-sensitive, fuzzy-filterable key reference. Contexts ported:
home, detail, sessions, session (the embedded Claude screen), todo,
trash. Selecting any row just closes the help, as the original.
"""

from __future__ import annotations

from config import get_session_key, key_label
from rendering import C_DIM, C_PURPLE, C_YELLOW

from .modals import FuzzyModalView

_HINT = f"[{C_DIM}]Type to filter  │  ^H back  │  Esc close[/{C_DIM}]"


class HelpView(FuzzyModalView):
    # ── markup helpers (ported verbatim) ──────────────────────────
    _hdr_n = 0  # class-level counter for unique ids

    @staticmethod
    def _hdr(text: str) -> tuple[str, str]:
        HelpView._hdr_n += 1
        slug = text.lower().replace(" ", "-").replace("'", "")
        return (f"hdr-{slug}-{HelpView._hdr_n}",
                f"[bold {C_PURPLE}]── {text} ──[/]")

    @staticmethod
    def _desc(text: str, n: int = 0) -> tuple[str, str]:
        HelpView._hdr_n += 1
        return (f"desc-{n}-{HelpView._hdr_n}", f"[italic {C_DIM}]{text}[/]")

    @staticmethod
    def _key(kid: str, keys: str, desc: str) -> tuple[str, str]:
        return (kid, f"[{C_YELLOW}]{keys}[/]  {desc}")

    # ── per-context help content (ported from HelpScreen) ─────────

    @classmethod
    def _home_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("Your workstreams — everything you're working on, in one place.", 1),
            D("Each row shows activity state, Claude sessions, and project info.", 2),
            D("Sessions are auto-discovered from your Claude usage and grouped here.", 3),
            D("", 4),
            H("Getting around"),
            K("nav-jk", "j / k", "Move down / up"),
            K("nav-pg", "Ctrl+D / U", "Half-page down / up"),
            K("nav-gg", "g / G", "Jump to top / bottom"),
            K("nav-enter", "Enter / l", "Open workstream"),
            K("nav-tab", "Ctrl+B / X", "Next / prev tab"),
            K("nav-close", "x", "Close tab"),
            K("nav-back", "Ctrl+H", "Back"),
            H("Start working"),
            K("ws-spawn", "c", "New Claude session in this workstream"),
            K("ws-resume", "r", "Resume most recent session"),
            K("ws-brain", "b", "Brain dump — stream of consciousness → launch"),
            K("ws-repo", "C", "Pick a repo, then spawn Claude there"),
            H("Manage"),
            K("ws-add", "a", "Add new workstream"),
            K("ws-note", "n", "Quick todo"),
            K("ws-todos", "e", "Full todo list"),
            K("ws-rename", "E", "Rename workstream"),
            K("ws-link", "W", "Add link (worktree, ticket, url, …)"),
            K("ws-open", "o", "Open links in browser/editor"),
            K("ws-archive", "u", "Archive / unarchive"),
            K("ws-delete", "d", "Delete"),
            H("Find things"),
            K("flt-search", "/", "Search across all workstreams"),
            K("cmd-palette", ":", "Command palette (fuzzy search all commands)"),
            K("flt-1", "1–6", "Filter: Active / Work / Personal / All / Stale / Archived"),
            K("srt-1", "F1–F5", "Sort: Activity / Updated / Created / Category / Name"),
            H("Dev workflow"),
            K("dw-ship", "P", "Ship — oneshot (staged → branch → MR)"),
            K("dw-ticket", "T", "Browse Jira tickets"),
            K("dw-branch", "B", "Browse branches and worktrees"),
            H("Other"),
            K("preview", "p", "Toggle preview pane"),
            K("refresh", "R", "Refresh"),
            K("help", "?", "This help"),
            K("quit", "q", "Quit"),
        ]

    @classmethod
    def _detail_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("The detail view for a workstream — all its sessions and metadata.", 1),
            D("Active sessions appear at top, archived below.", 2),
            D("The “earlier” divider separates today's work from older sessions.", 3),
            D("", 4),
            H("Navigate"),
            K("nav-jk", "j / k", "Move down / up"),
            K("nav-pg", "Ctrl+D / U", "Half-page down / up"),
            K("nav-enter", "Enter / l", "Open selected session"),
            K("nav-back", "h / Ctrl+H", "Go back to workstream list"),
            K("nav-panel", "Ctrl+J / K", "Cycle between panels"),
            H("Sessions"),
            K("sess-spawn", "c", "New Claude session"),
            K("sess-resume", "r / Ctrl+L", "Resume selected session"),
            K("sess-peek", "p", "Peek at session messages"),
            K("sess-archive", "Space", "Archive / restore session"),
            K("sess-defer", "z", "Shelve session (set aside for later)"),
            K("sess-yank", "y", "Copy resume command to clipboard"),
            K("sess-trash", "X", "Move to trash"),
            H("Workstream"),
            K("ws-note", "n", "Quick todo"),
            K("ws-todos", "e", "Full todo list"),
            K("ws-link", "W", "Add link"),
            K("ws-open", "o", "Open links"),
            K("ws-files", "f", "Browse files in workstream directory"),
            K("ws-tig", "t", "Git status/log (tig, fullscreen)"),
            K("ws-archive", "u", "Archive workstream"),
            K("nav-close", "x", "Close tab"),
            H("Notifications"),
            K("notif-dismiss", "d", "Dismiss notification"),
            K("notif-all", "D", "Dismiss all notifications"),
            H("Find things"),
            K("flt-search", "/", "Search session content"),
            K("flt-titles", "\\\\", "Search session titles only"),
            K("cmd-palette", ":", "Command palette"),
            K("help", "?", "This help"),
        ]

    @classmethod
    def _sessions_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("All active sessions across all your workstreams.", 1),
            D("A cross-cutting view — see everything that's running.", 2),
            D("", 3),
            H("Navigate"),
            K("nav-jk", "j / k", "Move down / up"),
            K("nav-enter", "Enter / l", "Open selected session"),
            K("nav-back", "Ctrl+H", "Go back"),
            H("Sessions"),
            K("sess-resume", "r", "Resume selected session"),
            K("sess-archive", "Space", "Archive session"),
            H("Other"),
            K("cmd-palette", ":", "Command palette"),
            K("help", "?", "This help"),
        ]

    @classmethod
    def _session_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("A live Claude session in an embedded terminal.", 1),
            D("This session is persistent — leave and it keeps running in the background.", 2),
            D("The sidebar shows git status and log (tig) for context.", 3),
            D("", 4),
            H("Navigate"),
            K("sess-back", "Ctrl+H", "Detach and go back (session keeps running)"),
            K("sess-back2", "Ctrl+\\\\", "Detach and go back (alternate)"),
            K("sess-panel", "Ctrl+J / K", "Cycle between terminal and tig panels"),
            K("sess-gitpanes", "F8", "Toggle the git (tig) panes — off stops their polling"),
            K("sess-zoom", "Ctrl+Z", "Zoom current panel full-screen"),
            K("sess-archive", "Ctrl+Space", "Archive session and go back"),
            D("(with the session list focused: archives the highlighted one, stays put)", 5),
            H("Session"),
            K("sess-extract", "Ctrl+E", "Extract a todo from the conversation"),
            K("sess-jump", "Ctrl+R", "Jump to a previous message"),
            K("sess-switch", "Ctrl+Shift+J / K", "Switch to next / prev session"),
            K("sess-auto", key_label(get_session_key("toggle_auto_mode")),
              "Start / cancel auto mode (asks first)"),
            H("Terminal"),
            K("term-scroll", "Ctrl+U / D", "Scroll up / down (half-page)"),
            K("term-type", "", "Type normally to interact with Claude"),
            D("", 10),
            H("Session lifecycle"),
            D("∙ thinking — Claude is actively working (animated indicator)", 20),
            D("∙ your turn — Claude is waiting for your input", 21),
            D("∙ committed — session ended with a git commit (work landed)", 22),
            D("∙ archived — filed away, always recoverable", 23),
        ]

    @classmethod
    def _todo_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("Your todo list for this workstream.", 1),
            D("Each todo can be spawned as a new Claude session.", 2),
            D("", 3),
            H("Actions"),
            K("todo-spawn", "Enter / c", "Spawn Claude session from this todo"),
            K("todo-toggle", "Space", "Toggle done / undone"),
            K("todo-add", "a", "Add new todo"),
            K("todo-edit", "e", "Edit todo text"),
            K("todo-ctx", "E", "Edit context / notes"),
            K("todo-del", "d", "Delete todo"),
            H("Navigate"),
            K("nav-jk", "j / k", "Move down / up"),
            K("nav-move", "J / K", "Move todo up / down in list"),
            K("nav-back", "Backspace", "Go back"),
        ]

    @classmethod
    def _trash_items(cls) -> list[tuple[str, str]]:
        H, D, K = cls._hdr, cls._desc, cls._key
        return [
            H("What you're looking at"),
            D("Deleted sessions and workstreams you can recover.", 1),
            D("", 2),
            H("Actions"),
            K("trash-restore", "u", "Restore selected item"),
            K("trash-purge", "D", "Permanently delete"),
            H("Navigate"),
            K("nav-jk", "j / k", "Move down / up"),
            K("nav-back", "Ctrl+H", "Go back"),
            K("help", "?", "This help"),
        ]

    def __init__(self, context: str = "home") -> None:
        self._help_ctx = context
        titles = {"home": "Workstreams", "detail": "Workstream Detail",
                  "sessions": "All Sessions", "session": "Claude Session",
                  "todo": "Todo List", "trash": "Trash"}
        label = titles.get(context, "Help")
        super().__init__(title=f"{label} — Help", hint=_HINT)

    def _get_items(self) -> list[tuple[str, str]]:
        getter = {
            "home": self._home_items,
            "detail": self._detail_items,
            "sessions": self._sessions_items,
            "session": self._session_items,
            "todo": self._todo_items,
            "trash": self._trash_items,
        }.get(self._help_ctx, self._home_items)
        return getter()

    def _on_selected(self, item_id) -> None:
        self.dismiss(None)
