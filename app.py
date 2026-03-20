"""Claude Orchestrator TUI — primary interface for managing parallel workstreams."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll, Container
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Rule,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from models import (
    Category, Link, Status, Store, Workstream,
    STATUS_ICONS, STATUS_COLORS, CATEGORY_COLORS, STATUS_ORDER,
    _relative_time,
)


# ─── Rich markup helpers ──────────────────────────────────────────────

def _status_markup(status: Status) -> str:
    color = STATUS_COLORS[status]
    icon = STATUS_ICONS[status]
    return f"[{color}]{icon} {status.value}[/{color}]"


def _category_markup(cat: Category) -> str:
    color = CATEGORY_COLORS[cat]
    return f"[{color}]{cat.value}[/{color}]"


def _link_icon(kind: str) -> str:
    return {
        "worktree": "\U0001f333",
        "ticket": "\U0001f3ab",
        "claude-session": "\U0001f916",
        "slack": "\U0001f4ac",
        "file": "\U0001f4c4",
        "url": "\U0001f517",
    }.get(kind, "\u2022")


LINK_KINDS = ["worktree", "ticket", "claude-session", "file", "url", "slack"]


# ─── Help Screen ──────────────────────────────────────────────────────

class HelpScreen(ModalScreen[None]):
    """Full keyboard reference."""

    BINDINGS = [
        Binding("question_mark,escape,q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        help_text = """\
[bold reverse] ORCHESTRATOR KEYBOARD REFERENCE [/bold reverse]

[bold cyan]Navigation[/bold cyan]
  [yellow]j / \u2193[/yellow]        Move down
  [yellow]k / \u2191[/yellow]        Move up
  [yellow]g / G[/yellow]        Jump to top / bottom
  [yellow]Enter[/yellow]      View detail
  [yellow]Escape[/yellow]     Back / close

[bold cyan]Actions[/bold cyan]
  [yellow]a[/yellow]          Add new workstream
  [yellow]b[/yellow]          Brain dump (multi-line)
  [yellow]s / S[/yellow]      Cycle status forward / backward
  [yellow]c[/yellow]          Spawn Claude session in tmux
  [yellow]r[/yellow]          Resume linked session / worktree
  [yellow]l[/yellow]          Add link to workstream
  [yellow]e[/yellow]          Edit notes
  [yellow]o[/yellow]          Open links
  [yellow]x[/yellow]          Archive workstream
  [yellow]d[/yellow]          Delete workstream

[bold cyan]Filters[/bold cyan]
  [yellow]1[/yellow]          All
  [yellow]2[/yellow]          Work only
  [yellow]3[/yellow]          Personal only
  [yellow]4[/yellow]          Active (in-progress + review)
  [yellow]5[/yellow]          Stale (>24h no update)
  [yellow]/[/yellow]          Search by name
  [yellow]Escape[/yellow]     Clear search

[bold cyan]Sort[/bold cyan]
  [yellow]F1[/yellow]         Sort by status [dim](default)[/dim]
  [yellow]F2[/yellow]         Sort by last updated
  [yellow]F3[/yellow]         Sort by created
  [yellow]F4[/yellow]         Sort by category
  [yellow]F5[/yellow]         Sort by name

[bold cyan]Command Palette[/bold cyan]
  [yellow]:[/yellow]          Open command palette
  [dim]  status, link, note, archive, unarchive,
  search, sort, filter, spawn, resume,
  export, brain, delete, help[/dim]

[bold cyan]Other[/bold cyan]
  [yellow]R[/yellow]          Refresh data
  [yellow]?[/yellow]          This help screen
  [yellow]q[/yellow]          Quit\
"""
        with Vertical(id="help-container"):
            yield Static(help_text, id="help-content")

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 58;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #help-content {
        padding: 0 1;
    }
    """


# ─── Notes Screen ────────────────────────────────────────────────────

class NotesScreen(ModalScreen[None]):
    """Edit notes for a workstream."""

    BINDINGS = [
        Binding("escape", "save_and_close", "Save & back", priority=True),
    ]

    DEFAULT_CSS = """
    NotesScreen {
        align: center middle;
    }
    #notes-container {
        width: 80;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #notes-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #notes-editor {
        height: 20;
        margin: 0 0 1 0;
    }
    #notes-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="notes-container"):
            yield Label(f"Notes: {self.ws.name}", id="notes-title")
            yield TextArea(self.ws.notes or "", id="notes-editor")
            yield Static("[dim]Esc[/dim] save & back", id="notes-hint")

    def action_save_and_close(self):
        editor = self.query_one("#notes-editor", TextArea)
        self.ws.notes = editor.text
        self.store.update(self.ws)
        self.dismiss()


# ─── Links Screen ────────────────────────────────────────────────────

class LinksScreen(ModalScreen[None]):
    """View and open links for a workstream."""

    BINDINGS = [
        Binding("escape,q", "dismiss", "Back"),
        Binding("enter", "open_link", "Open"),
    ]

    DEFAULT_CSS = """
    LinksScreen {
        align: center middle;
    }
    #links-container {
        width: 80;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #links-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #links-list {
        height: auto;
        max-height: 20;
    }
    #links-hint {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="links-container"):
            yield Label(f"Links: {self.ws.name}", id="links-title")
            options = []
            for i, lnk in enumerate(self.ws.links):
                icon = _link_icon(lnk.kind)
                options.append(Option(f"{icon}  [{lnk.kind}] {lnk.label}: {lnk.value}", id=str(i)))
            if not options:
                options.append(Option("(no links)", id="none", disabled=True))
            yield OptionList(*options, id="links-list")
            yield Static("[dim]Enter[/dim] open  [dim]Esc[/dim] back", id="links-hint")

    def action_open_link(self):
        option_list = self.query_one("#links-list", OptionList)
        idx = option_list.highlighted
        if idx is not None and idx < len(self.ws.links):
            link = self.ws.links[idx]
            _open_link(link)
            self.app.notify(f"Opening {link.label}...", timeout=2)


# ─── Add Screen ──────────────────────────────────────────────────────

class AddScreen(ModalScreen[Workstream | None]):
    """Add a new workstream."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    AddScreen {
        align: center middle;
    }
    #add-container {
        width: 70;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #add-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #add-container Input {
        margin: 0 0 1 0;
    }
    #add-container Select {
        margin: 0 0 1 0;
    }
    #add-hint {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="add-container"):
            yield Label("New Workstream", id="add-title")
            yield Input(placeholder="Name", id="add-name")
            yield Input(placeholder="Description (optional)", id="add-desc")
            yield Select(
                [(c.value, c) for c in Category],
                value=Category.PERSONAL,
                id="add-category",
            )
            yield Static(
                "[dim]Enter[/dim] create  [dim]Esc[/dim] cancel",
                id="add-hint",
            )

    def on_mount(self):
        self.query_one("#add-name", Input).focus()

    @on(Input.Submitted, "#add-name")
    def on_name_submitted(self):
        self.query_one("#add-desc", Input).focus()

    @on(Input.Submitted, "#add-desc")
    def on_desc_submitted(self):
        self._create()

    def _create(self):
        name = self.query_one("#add-name", Input).value.strip()
        if not name:
            self.app.notify("Name cannot be empty", severity="error", timeout=2)
            return
        desc = self.query_one("#add-desc", Input).value.strip()
        cat = self.query_one("#add-category", Select).value
        ws = Workstream(name=name, description=desc, category=cat)
        self.dismiss(ws)

    def action_cancel(self):
        self.dismiss(None)


# ─── Detail Screen ───────────────────────────────────────────────────

class DetailScreen(ModalScreen[None]):
    """Full detail view for a single workstream."""

    BINDINGS = [
        Binding("q,escape", "dismiss", "Back"),
        Binding("s", "cycle_status", "Status \u2192"),
        Binding("S", "cycle_status_back", "Status \u2190"),
        Binding("c", "spawn", "Spawn"),
        Binding("r", "resume", "Resume"),
        Binding("l", "add_link", "Link+"),
        Binding("e", "edit_notes", "Edit notes"),
        Binding("o", "open_links", "Open links"),
        Binding("x", "archive", "Archive"),
    ]

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 80;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #detail-header {
        text-style: bold;
        text-align: center;
        padding-bottom: 1;
    }
    #detail-body {
        padding: 0 1;
    }
    #detail-help {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, ws: Workstream, store: Store):
        super().__init__()
        self.ws = ws
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-container"):
            yield Static(self._render_header(), id="detail-header")
            yield Rule()
            yield Static(self._render_body(), id="detail-body")
            yield Rule()
            yield Static(
                "[dim]s/S[/dim] status  [dim]c[/dim] spawn  [dim]r[/dim] resume  "
                "[dim]l[/dim] link+  [dim]e[/dim] notes  [dim]o[/dim] open  "
                "[dim]x[/dim] archive  [dim]q[/dim] back",
                id="detail-help",
            )

    def _render_header(self) -> str:
        return (
            f"[bold]{self.ws.name}[/bold]\n"
            f"{_category_markup(self.ws.category)}  {_status_markup(self.ws.status)}"
        )

    def _render_body(self) -> str:
        lines = []

        # Timestamps
        lines.append(f"[bold dim]\u2500\u2500\u2500 Timestamps \u2500\u2500\u2500[/bold dim]")
        lines.append(f"  [dim]Created:[/dim]   {self.ws.created_at[:19]}  [dim]({_relative_time(self.ws.created_at)})[/dim]")
        lines.append(f"  [dim]Updated:[/dim]   {self.ws.updated_at[:19]}  [dim]({_relative_time(self.ws.updated_at)})[/dim]")
        lines.append(f"  [dim]Status \u0394:[/dim]  {self.ws.status_changed_at[:19]}  [dim]({_relative_time(self.ws.status_changed_at)})[/dim]")
        if self.ws.archived:
            lines.append(f"  [dim italic]Archived[/dim italic]")
        lines.append("")

        # Description
        lines.append(f"[bold dim]\u2500\u2500\u2500 Description \u2500\u2500\u2500[/bold dim]")
        lines.append(f"  {self.ws.description or '[dim](none)[/dim]'}")
        lines.append("")

        # Links
        lines.append(f"[bold dim]\u2500\u2500\u2500 Links ({len(self.ws.links)}) \u2500\u2500\u2500[/bold dim]")
        if self.ws.links:
            for lnk in self.ws.links:
                icon = _link_icon(lnk.kind)
                lines.append(f"  {icon}  [{lnk.kind}] [bold]{lnk.label}[/bold]: {lnk.value}")
        else:
            lines.append("  [dim](none)[/dim]")
        lines.append("")

        # Notes
        lines.append(f"[bold dim]\u2500\u2500\u2500 Notes \u2500\u2500\u2500[/bold dim]")
        if self.ws.notes:
            for line in self.ws.notes.split("\n"):
                lines.append(f"  {line}")
        else:
            lines.append("  [dim](none)[/dim]")

        return "\n".join(lines)

    def _refresh(self):
        self.query_one("#detail-header", Static).update(self._render_header())
        self.query_one("#detail-body", Static).update(self._render_body())

    def action_cycle_status(self):
        statuses = list(Status)
        idx = statuses.index(self.ws.status)
        self.ws.set_status(statuses[(idx + 1) % len(statuses)])
        self.store.update(self.ws)
        self._refresh()

    def action_cycle_status_back(self):
        statuses = list(Status)
        idx = statuses.index(self.ws.status)
        self.ws.set_status(statuses[(idx - 1) % len(statuses)])
        self.store.update(self.ws)
        self._refresh()

    def action_edit_notes(self):
        def on_notes_close(_):
            self.store.load()
            self.ws = self.store.get(self.ws.id) or self.ws
            self._refresh()
        self.app.push_screen(NotesScreen(self.ws, self.store), callback=on_notes_close)

    def action_open_links(self):
        if self.ws.links:
            self.app.push_screen(LinksScreen(self.ws, self.store))
        else:
            self.app.notify("No links to open", timeout=2)

    def action_archive(self):
        self.ws.archived = True
        self.store.update(self.ws)
        self.app.notify(f"Archived: {self.ws.name}", timeout=2)
        self.dismiss()

    def action_spawn(self):
        if not _has_tmux():
            self.app.notify("Not in a tmux session", severity="error", timeout=2)
            return

        def on_prompt(prompt: str | None):
            if prompt is None:
                return
            cwd = _ws_working_dir(self.ws)
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"\U0001f916{self.ws.name[:18]}",
                 "-c", cwd, "claude", "-p", prompt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.app.notify(f"Session spawned for {self.ws.name}", timeout=2)

        self.app.push_screen(SpawnPromptScreen(self.ws), callback=on_prompt)

    def action_resume(self):
        _do_resume(self.ws, self.app)

    def action_add_link(self):
        def on_link(link: Link | None):
            if link:
                self.ws.links.append(link)
                self.ws.touch()
                self.store.update(self.ws)
                self._refresh()
                self.app.notify(f"Added {link.kind} link", timeout=2)

        self.app.push_screen(AddLinkScreen(self.ws.name), callback=on_link)


# ─── Brain Dump Screen ───────────────────────────────────────────────

class BrainDumpScreen(ModalScreen[str | None]):
    """Multi-line text input for stream-of-consciousness brain dump."""

    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    DEFAULT_CSS = """
    BrainDumpScreen {
        align: center middle;
    }
    #brain-container {
        width: 80;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #brain-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #brain-desc {
        color: $text-muted;
        padding-bottom: 1;
    }
    #brain-editor {
        height: 12;
        margin: 0 0 1 0;
    }
    #brain-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="brain-container"):
            yield Label("[bold]Brain Dump[/bold]", id="brain-title")
            yield Static(
                "[dim]Type your stream of consciousness. Commas, newlines, "
                "'also'/'and then' split into tasks.[/dim]",
                id="brain-desc",
            )
            yield TextArea("", id="brain-editor")
            yield Static("[dim]Ctrl+S[/dim] submit  [dim]Esc[/dim] cancel", id="brain-hint")

    def on_mount(self):
        self.query_one("#brain-editor", TextArea).focus()

    def action_submit(self):
        text = self.query_one("#brain-editor", TextArea).text.strip()
        if not text:
            self.app.notify("Nothing to parse", severity="warning", timeout=2)
            return
        self.dismiss(text)

    def action_cancel(self):
        self.dismiss(None)


# ─── Brain Preview Screen ────────────────────────────────────────────

class BrainPreviewScreen(ModalScreen[bool]):
    """Preview parsed brain dump tasks before adding."""

    BINDINGS = [
        Binding("enter,y", "confirm", "Add all"),
        Binding("escape,n", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    BrainPreviewScreen {
        align: center middle;
    }
    #brain-preview-container {
        width: 80;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #brain-preview-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #brain-preview-body {
        padding: 0 1;
        max-height: 30;
    }
    #brain-preview-hint {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, tasks: list):
        super().__init__()
        self.tasks = tasks

    def compose(self) -> ComposeResult:
        with Vertical(id="brain-preview-container"):
            yield Static(
                f"[bold]Parsed {len(self.tasks)} tasks[/bold]",
                id="brain-preview-title",
            )
            yield Rule()
            body_lines = []
            for i, task in enumerate(self.tasks, 1):
                body_lines.append(f"  [bold]{i}.[/bold] {task.name}")
                body_lines.append(f"     {_status_markup(task.status)}  {_category_markup(task.category)}")
                if task.raw_text != task.name:
                    raw = task.raw_text[:80]
                    body_lines.append(f"     [dim]{raw}[/dim]")
                body_lines.append("")
            yield Static("\n".join(body_lines), id="brain-preview-body")
            yield Rule()
            yield Static(
                "[dim]Enter/y[/dim] add all  [dim]Esc/n[/dim] cancel",
                id="brain-preview-hint",
            )

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


# ─── Add Link Screen ─────────────────────────────────────────────────

class AddLinkScreen(ModalScreen[Link | None]):
    """Add a link to a workstream."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    AddLinkScreen {
        align: center middle;
    }
    #addlink-container {
        width: 70;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #addlink-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #addlink-container Input {
        margin: 0 0 1 0;
    }
    #addlink-container Select {
        margin: 0 0 1 0;
    }
    #addlink-hint {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, ws_name: str):
        super().__init__()
        self.ws_name = ws_name

    def compose(self) -> ComposeResult:
        with Vertical(id="addlink-container"):
            yield Label(f"Add Link: {self.ws_name}", id="addlink-title")
            yield Select(
                [(k, k) for k in LINK_KINDS],
                value="url",
                id="addlink-kind",
            )
            yield Input(
                placeholder="Value (URL, path, ticket ID, session ID...)",
                id="addlink-value",
            )
            yield Input(placeholder="Label (optional, defaults to kind)", id="addlink-label")
            yield Static(
                "[dim]Enter[/dim] add  [dim]Esc[/dim] cancel",
                id="addlink-hint",
            )

    def on_mount(self):
        self.query_one("#addlink-value", Input).focus()

    @on(Input.Submitted, "#addlink-value")
    def on_value_submitted(self):
        self.query_one("#addlink-label", Input).focus()

    @on(Input.Submitted, "#addlink-label")
    def on_label_submitted(self):
        self._create()

    def _create(self):
        kind = self.query_one("#addlink-kind", Select).value
        value = self.query_one("#addlink-value", Input).value.strip()
        label = self.query_one("#addlink-label", Input).value.strip()
        if not value:
            self.app.notify("Value cannot be empty", severity="error", timeout=2)
            return
        if not label:
            label = kind
        self.dismiss(Link(kind=kind, label=label, value=value))

    def action_cancel(self):
        self.dismiss(None)


# ─── Spawn Prompt Screen ─────────────────────────────────────────────

class SpawnPromptScreen(ModalScreen[str | None]):
    """Optional prompt before spawning a Claude session."""

    BINDINGS = [
        Binding("ctrl+s", "submit", "Spawn", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    DEFAULT_CSS = """
    SpawnPromptScreen {
        align: center middle;
    }
    #spawn-container {
        width: 80;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: heavy $accent;
    }
    #spawn-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #spawn-desc {
        color: $text-muted;
        padding-bottom: 1;
    }
    #spawn-editor {
        height: 8;
        margin: 0 0 1 0;
    }
    #spawn-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self, ws: Workstream):
        super().__init__()
        self.ws = ws

    def compose(self) -> ComposeResult:
        prompt = f"Working on: {self.ws.name}"
        if self.ws.description:
            prompt += f"\nDescription: {self.ws.description}"

        with Vertical(id="spawn-container"):
            yield Label(f"[bold]Spawn Claude: {self.ws.name}[/bold]", id="spawn-title")
            yield Static(
                "[dim]Edit the initial prompt, or submit as-is.[/dim]",
                id="spawn-desc",
            )
            yield TextArea(prompt, id="spawn-editor")
            yield Static("[dim]Ctrl+S[/dim] spawn  [dim]Esc[/dim] cancel", id="spawn-hint")

    def on_mount(self):
        self.query_one("#spawn-editor", TextArea).focus()

    def action_submit(self):
        text = self.query_one("#spawn-editor", TextArea).text.strip()
        self.dismiss(text or "")

    def action_cancel(self):
        self.dismiss(None)


# ─── Search Input ────────────────────────────────────────────────────

class SearchInput(Input):
    """Inline search input that appears in the filter bar."""

    BINDINGS = [
        Binding("escape", "cancel_search", "Cancel", priority=True),
    ]

    def action_cancel_search(self):
        self.value = ""
        app = self.app
        app.search_text = ""
        app._refresh_table()
        self.display = False
        app.query_one("#main-table", DataTable).focus()


# ─── Command Palette Input ───────────────────────────────────────────

class CommandInput(Input):
    """Inline command palette input (vim : mode)."""

    BINDINGS = [
        Binding("escape", "cancel_command", "Cancel", priority=True),
    ]

    def action_cancel_command(self):
        self.value = ""
        self.display = False
        self.app.query_one("#main-table", DataTable).focus()


# ─── Confirm Delete Screen ──────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    """Confirmation dialog."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape,q", "deny", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-container {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: heavy $error;
    }
    #confirm-msg {
        text-align: center;
        padding: 1;
    }
    #confirm-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-container"):
            yield Static(self.message, id="confirm-msg")
            yield Static("[dim]y[/dim] yes  [dim]n[/dim] no", id="confirm-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_deny(self):
        self.dismiss(False)


# ─── Shared utilities ────────────────────────────────────────────────

def _has_tmux() -> bool:
    """Check if we're inside tmux."""
    return bool(os.environ.get("TMUX"))


def _ws_working_dir(ws: Workstream) -> str:
    """Get the best working directory for a workstream from its links."""
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value)
            if os.path.isdir(expanded):
                return expanded
    return os.getcwd()


def _do_resume(ws: Workstream, app: App):
    """Resume a linked session or worktree for a workstream."""
    # Check for claude-session links first
    session_links = [lnk for lnk in ws.links if lnk.kind == "claude-session"]
    if session_links:
        session_id = session_links[-1].value
        if _has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"claude:{ws.name[:20]}",
                 "claude", "--resume", session_id],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            app.notify(f"Resuming session {session_id[:8]}...", timeout=2)
        else:
            app.notify("Not in a tmux session", severity="error", timeout=2)
        return

    # Check for worktree links
    worktree_links = [lnk for lnk in ws.links if lnk.kind == "worktree"]
    if worktree_links:
        path = os.path.expanduser(worktree_links[0].value)
        if _has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", ws.name[:20], "-c", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            app.notify(f"Opening {path}", timeout=2)
        else:
            app.notify("Not in a tmux session", severity="error", timeout=2)
        return

    app.notify("No session or worktree linked", timeout=2)


def _open_link(link: Link):
    """Open a link using the appropriate system handler."""
    value = os.path.expanduser(link.value)

    if link.kind == "url":
        subprocess.Popen(
            ["xdg-open", link.value],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif link.kind == "worktree":
        if _has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", link.label, "-c", value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["xdg-open", value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    elif link.kind == "file":
        if os.path.isdir(value):
            subprocess.Popen(
                ["xdg-open", value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif os.path.isfile(value):
            editor = os.environ.get("EDITOR", "nvim")
            if _has_tmux():
                subprocess.Popen(
                    ["tmux", "new-window", "-n", link.label, editor, value],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["xdg-open", value],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        else:
            subprocess.Popen(
                ["xdg-open", value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    elif link.kind == "ticket":
        pass
    elif link.kind == "claude-session":
        if _has_tmux():
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"claude:{link.label}",
                 "claude", "--resume", link.value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


# ─── Main App ────────────────────────────────────────────────────────

class OrchestratorApp(App):
    """Claude Orchestrator — workstream dashboard."""

    CSS = """
    Screen {
        background: $surface;
    }

    /* ── Status bar ── */
    #status-bar {
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        dock: top;
    }

    /* ── Filter bar ── */
    #filter-bar {
        height: 1;
        padding: 0 1;
        background: $boost;
        dock: top;
    }

    /* ── Main table ── */
    #main-table {
        height: 1fr;
    }
    DataTable > .datatable--header {
        text-style: bold;
        background: $primary-background;
        color: $text;
    }
    DataTable > .datatable--cursor {
        background: $accent 40%;
        color: $text;
        text-style: bold;
    }

    /* ── Inline inputs (search + command) ── */
    #search-input, #command-input {
        dock: bottom;
        height: 1;
        display: none;
        border: none;
        background: $boost;
    }
    #search-input:focus, #command-input:focus {
        border: none;
    }

    /* ── Summary bar ── */
    #summary-bar {
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
        dock: bottom;
    }
    """

    TITLE = "orchestrator"

    BINDINGS = [
        # Navigation
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("enter", "view_detail", "Detail", show=True),

        # Actions
        Binding("a", "add", "Add", show=True),
        Binding("b", "brain_dump", "Brain", show=True),
        Binding("s", "cycle_status", "Status", show=True),
        Binding("S", "cycle_status_back", "Status\u2190", show=False),
        Binding("c", "spawn", "Spawn", show=True),
        Binding("r", "resume", "Resume", show=True),
        Binding("l", "add_link", "Link+", show=True),
        Binding("e", "edit_notes", "Notes", show=False),
        Binding("o", "open_links", "Open", show=False),
        Binding("x", "archive", "Archive", show=False),
        Binding("d", "delete", "Delete", show=False),

        # Filters
        Binding("1", "filter('all')", "All", show=False),
        Binding("2", "filter('work')", "Work", show=False),
        Binding("3", "filter('personal')", "Personal", show=False),
        Binding("4", "filter('active')", "Active", show=False),
        Binding("5", "filter('stale')", "Stale", show=False),
        Binding("slash", "search", "/Search", show=True),

        # Sort
        Binding("f1", "sort('status')", "Sort:Status", show=False),
        Binding("f2", "sort('updated')", "Sort:Updated", show=False),
        Binding("f3", "sort('created')", "Sort:Created", show=False),
        Binding("f4", "sort('category')", "Sort:Category", show=False),
        Binding("f5", "sort('name')", "Sort:Name", show=False),

        # Command palette
        Binding("colon", "command_palette", ":Cmd", show=True),

        # Other
        Binding("R", "refresh", "Refresh", show=False),
        Binding("question_mark", "help", "?Help", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.store = Store()
        self.filter_mode: str = "all"
        self.sort_mode: str = "status"
        self.search_text: str = ""
        self._tmux_paths: set[str] = set()
        self._tmux_names: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._render_status_bar(), id="status-bar")
        yield Static(self._render_filter_bar(), id="filter-bar")
        yield DataTable(id="main-table")
        yield SearchInput(placeholder="Search...", id="search-input")
        yield CommandInput(placeholder=":", id="command-input")
        yield Static("", id="summary-bar")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#main-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("ID", "Name", "Category", "Status", "Age", "Updated")
        self._refresh_table()
        # Start tmux polling
        self._poll_tmux()
        self.set_interval(30, self._poll_tmux)

    # ── Tmux session polling ──

    def _poll_tmux(self):
        """Kick off a background check for live tmux sessions."""
        self._do_tmux_check()

    @work(thread=True, exclusive=True)
    def _do_tmux_check(self):
        """Check tmux for live sessions in a background thread."""
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-a", "-F",
                 "#{window_name}\t#{pane_current_path}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return

            paths: set[str] = set()
            names: set[str] = set()
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if "\t" in line:
                    name, path = line.split("\t", 1)
                    names.add(name)
                    paths.add(path.rstrip("/"))
                else:
                    names.add(line.strip())

            self.call_from_thread(self._apply_tmux_status, paths, names)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    def _apply_tmux_status(self, paths: set[str], names: set[str]):
        """Apply tmux status and refresh table if changed."""
        if paths != self._tmux_paths or names != self._tmux_names:
            self._tmux_paths = paths
            self._tmux_names = names
            self._refresh_table()

    def _ws_has_tmux(self, ws: Workstream) -> bool:
        """Check if a workstream has a live tmux session."""
        for link in ws.links:
            if link.kind == "worktree":
                expanded = os.path.expanduser(link.value).rstrip("/")
                # Check if any tmux pane is at or under this worktree path
                for tmux_path in self._tmux_paths:
                    if tmux_path == expanded or tmux_path.startswith(expanded + "/"):
                        return True
        # Check window names (spawn creates windows named 🤖{name})
        spawn_name = f"\U0001f916{ws.name[:18]}"
        if spawn_name in self._tmux_names:
            return True
        if ws.name[:20] in self._tmux_names:
            return True
        return False

    # ── Bar rendering ──

    def _render_status_bar(self) -> str:
        total = len(self.store.active)
        in_prog = len([w for w in self.store.active if w.status == Status.IN_PROGRESS])
        blocked = len([w for w in self.store.active if w.status == Status.BLOCKED])
        review = len([w for w in self.store.active if w.status == Status.AWAITING_REVIEW])
        done = len([w for w in self.store.active if w.status == Status.DONE])
        stale = len(self.store.stale())

        parts = [
            f"[bold]{total}[/bold] streams",
            f"[yellow]{STATUS_ICONS[Status.IN_PROGRESS]} {in_prog}[/yellow]",
            f"[red]{STATUS_ICONS[Status.BLOCKED]} {blocked}[/red]",
            f"[cyan]{STATUS_ICONS[Status.AWAITING_REVIEW]} {review}[/cyan]",
            f"[green]{STATUS_ICONS[Status.DONE]} {done}[/green]",
        ]
        if stale:
            parts.append(f"[dim italic]{stale} stale[/dim italic]")

        return "  ".join(parts)

    def _render_filter_bar(self) -> str:
        filters = {
            "all": "1:All",
            "work": "2:Work",
            "personal": "3:Personal",
            "active": "4:Active",
            "stale": "5:Stale",
        }
        parts = []
        for key, label in filters.items():
            if self.filter_mode == key:
                parts.append(f"[bold reverse] {label} [/bold reverse]")
            else:
                parts.append(f"[dim]{label}[/dim]")

        sort_labels = {
            "status": "Status", "updated": "Updated", "created": "Created",
            "category": "Category", "name": "Name",
        }
        sort_label = sort_labels.get(self.sort_mode, self.sort_mode)
        parts.append(f"  [dim]Sort:[/dim][bold]{sort_label}[/bold]")

        if self.search_text:
            parts.append(f"  [dim]Search:[/dim][yellow]{self.search_text}[/yellow]")

        return " ".join(parts)

    def _render_summary_bar(self, count: int) -> str:
        return (
            f"  {count} items shown  "
            f"[dim]\u2502[/dim]  [dim]?[/dim] help  "
            f"[dim]b[/dim] brain  "
            f"[dim]:[/dim] cmd  "
            f"[dim]F1-F5[/dim] sort  "
            f"[dim]1-5[/dim] filter"
        )

    # ── Table rendering ──

    def _get_filtered_streams(self) -> list[Workstream]:
        """Get filtered and sorted workstreams."""
        if self.filter_mode == "all":
            streams = list(self.store.active)
        elif self.filter_mode == "work":
            streams = [w for w in self.store.active if w.category == Category.WORK]
        elif self.filter_mode == "personal":
            streams = [w for w in self.store.active if w.category == Category.PERSONAL]
        elif self.filter_mode == "active":
            streams = [w for w in self.store.active if w.is_active]
        elif self.filter_mode == "stale":
            streams = self.store.stale()
        else:
            streams = list(self.store.active)

        if self.search_text:
            q = self.search_text.lower()
            streams = [w for w in streams if q in w.name.lower() or q in w.description.lower()]

        return self.store.sorted(streams, self.sort_mode)

    def _refresh_table(self):
        table = self.query_one("#main-table", DataTable)

        # Preserve cursor position across refresh
        old_key = None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            old_key = str(row_key.value)
        except Exception:
            pass

        table.clear()

        streams = self._get_filtered_streams()

        for ws in streams:
            icon = STATUS_ICONS[ws.status]

            # Stale indicator
            stale_mark = " \u23f0" if ws.is_stale and ws.status != Status.DONE else ""

            # Links indicator
            link_count = f" \U0001f517{len(ws.links)}" if ws.links else ""

            # Tmux live indicator
            tmux_mark = " \u26a1" if self._ws_has_tmux(ws) else ""

            table.add_row(
                ws.id[:8],
                ws.name + link_count + stale_mark + tmux_mark,
                ws.category.value,
                f"{icon} {ws.status.value}",
                _relative_time(ws.created_at),
                _relative_time(ws.updated_at),
                key=ws.id,
            )

        # Restore cursor to the same workstream
        if old_key:
            for i, row_key in enumerate(table.rows):
                if str(row_key.value) == old_key:
                    table.move_cursor(row=i)
                    break

        # Update bars
        self.query_one("#status-bar", Static).update(self._render_status_bar())
        self.query_one("#filter-bar", Static).update(self._render_filter_bar())
        self.query_one("#summary-bar", Static).update(self._render_summary_bar(len(streams)))

    def _selected_ws(self) -> Workstream | None:
        table = self.query_one("#main-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return self.store.get(str(row_key.value))
        except Exception:
            return None

    # ── Navigation actions ──

    def action_cursor_down(self):
        self.query_one("#main-table", DataTable).action_cursor_down()

    def action_cursor_up(self):
        self.query_one("#main-table", DataTable).action_cursor_up()

    def action_cursor_top(self):
        table = self.query_one("#main-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0)

    def action_cursor_bottom(self):
        table = self.query_one("#main-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)

    # ── Detail & editing ──

    def action_view_detail(self):
        self._open_detail()

    @on(DataTable.RowSelected, "#main-table")
    def on_row_selected(self, event: DataTable.RowSelected):
        self._open_detail()

    def _open_detail(self):
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                DetailScreen(ws, self.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def action_add(self):
        def on_result(ws: Workstream | None):
            if ws:
                self.store.add(ws)
                self.notify(f"Created: {ws.name}", timeout=2)
            self._refresh_table()

        self.push_screen(AddScreen(), callback=on_result)

    def action_cycle_status(self):
        ws = self._selected_ws()
        if ws:
            statuses = list(Status)
            idx = statuses.index(ws.status)
            ws.set_status(statuses[(idx + 1) % len(statuses)])
            self.store.update(ws)
            self._refresh_table()
            self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_cycle_status_back(self):
        ws = self._selected_ws()
        if ws:
            statuses = list(Status)
            idx = statuses.index(ws.status)
            ws.set_status(statuses[(idx - 1) % len(statuses)])
            self.store.update(ws)
            self._refresh_table()
            self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)

    def action_edit_notes(self):
        ws = self._selected_ws()
        if ws:
            self.push_screen(
                NotesScreen(ws, self.store),
                callback=lambda _: self._on_return_from_modal(),
            )

    def action_open_links(self):
        ws = self._selected_ws()
        if not ws:
            return
        if ws.links:
            if len(ws.links) == 1:
                _open_link(ws.links[0])
                self.notify(f"Opening {ws.links[0].label}...", timeout=2)
            else:
                self.push_screen(LinksScreen(ws, self.store))
        else:
            self.notify("No links", timeout=1)

    def action_archive(self):
        ws = self._selected_ws()
        if ws:
            self.store.archive(ws.id)
            self.notify(f"Archived: {ws.name}", timeout=2)
            self._refresh_table()

    def action_delete(self):
        ws = self._selected_ws()
        if ws:
            def on_confirm(confirmed: bool):
                if confirmed:
                    self.store.remove(ws.id)
                    self.notify(f"Deleted: {ws.name}", timeout=2)
                    self._refresh_table()

            self.push_screen(
                ConfirmScreen(f"[bold red]Delete[/bold red] [bold]{ws.name}[/bold]?"),
                callback=on_confirm,
            )

    # ── Brain dump ──

    def action_brain_dump(self):
        """Open brain dump multi-line input."""
        def on_text(text: str | None):
            if text is None:
                return
            self._do_brain(text)

        self.push_screen(BrainDumpScreen(), callback=on_text)

    def _do_brain(self, text: str):
        """Parse brain dump text and show preview."""
        from brain import parse_brain_dump

        tasks = parse_brain_dump(text)
        if not tasks:
            self.notify("No tasks found in input", severity="warning", timeout=2)
            return

        def on_confirm(confirmed: bool):
            if confirmed:
                for task in tasks:
                    ws = Workstream(
                        name=task.name,
                        description=task.raw_text,
                        category=task.category,
                        status=task.status,
                    )
                    self.store.add(ws)
                self.notify(f"Added {len(tasks)} workstreams", timeout=2)
                self._refresh_table()

        self.push_screen(BrainPreviewScreen(tasks), callback=on_confirm)

    # ── Spawn & resume ──

    def action_spawn(self):
        """Spawn a new Claude session for the selected workstream."""
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        if not _has_tmux():
            self.notify("Not in a tmux session", severity="error", timeout=2)
            return

        def on_prompt(prompt: str | None):
            if prompt is None:
                return
            cwd = _ws_working_dir(ws)
            subprocess.Popen(
                ["tmux", "new-window", "-n", f"\U0001f916{ws.name[:18]}",
                 "-c", cwd, "claude", "-p", prompt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.notify(f"Session spawned for {ws.name}", timeout=2)
            # Refresh tmux status soon
            self.set_timer(2, self._poll_tmux)

        self.push_screen(SpawnPromptScreen(ws), callback=on_prompt)

    def action_resume(self):
        """Resume a linked session or open worktree for the selected workstream."""
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return
        _do_resume(ws, self)

    # ── Add link ──

    def action_add_link(self):
        """Add a link to the selected workstream."""
        ws = self._selected_ws()
        if not ws:
            self.notify("No workstream selected", timeout=2)
            return

        def on_link(link: Link | None):
            if link:
                ws.links.append(link)
                ws.touch()
                self.store.update(ws)
                self._refresh_table()
                self.notify(f"Added {link.kind} link to {ws.name}", timeout=2)

        self.push_screen(AddLinkScreen(ws.name), callback=on_link)

    # ── Filter actions ──

    def action_filter(self, mode: str):
        self.filter_mode = mode
        self._refresh_table()

    def action_sort(self, mode: str):
        self.sort_mode = mode
        self._refresh_table()

    def action_search(self):
        self.query_one("#command-input", CommandInput).display = False
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = True
        search_input.value = self.search_text
        search_input.focus()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted):
        self.search_text = event.value.strip()
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = False
        self._refresh_table()
        self.query_one("#main-table", DataTable).focus()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed):
        self.search_text = event.value.strip()
        self._refresh_table()

    # ── Command palette ──

    def action_command_palette(self):
        """Open the command palette (vim : mode)."""
        self.query_one("#search-input", SearchInput).display = False
        cmd_input = self.query_one("#command-input", CommandInput)
        cmd_input.display = True
        cmd_input.value = ""
        cmd_input.focus()

    @on(Input.Submitted, "#command-input")
    def on_command_submitted(self, event: Input.Submitted):
        cmd_text = event.value.strip()
        cmd_input = self.query_one("#command-input", CommandInput)
        cmd_input.display = False
        self.query_one("#main-table", DataTable).focus()
        if cmd_text:
            self._execute_command(cmd_text)

    def _execute_command(self, cmd_text: str):
        """Parse and execute a command palette command."""
        parts = cmd_text.strip().split(None, 1)
        if not parts:
            return

        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        ws = self._selected_ws()

        # ── status ──
        if cmd in ("status", "s") and cmd != "sort" and cmd != "search" and cmd != "spawn":
            if not ws:
                self.notify("No workstream selected", severity="error", timeout=2)
                return
            if not arg:
                self.notify("Usage: status <queued|in-progress|awaiting-review|done|blocked>", timeout=3)
                return
            try:
                ws.set_status(Status(arg))
                self.store.update(ws)
                self._refresh_table()
                self.notify(f"{ws.name} \u2192 {STATUS_ICONS[ws.status]} {ws.status.value}", timeout=1)
            except ValueError:
                self.notify(f"Invalid status: {arg}", severity="error", timeout=2)

        # ── link ──
        elif cmd in ("link", "ln"):
            if not ws:
                self.notify("No workstream selected", severity="error", timeout=2)
                return
            if ":" not in arg:
                self.notify("Usage: link kind:value (e.g. ticket:UB-1234)", severity="error", timeout=2)
                return
            kind, value = arg.split(":", 1)
            if kind not in LINK_KINDS:
                self.notify(f"Unknown kind: {kind}. Use: {', '.join(LINK_KINDS)}", severity="error", timeout=2)
                return
            ws.add_link(kind=kind, value=value, label=kind)
            self.store.update(ws)
            self._refresh_table()
            self.notify(f"Added {kind} link to {ws.name}", timeout=2)

        # ── note ──
        elif cmd in ("note", "n"):
            if not ws:
                self.notify("No workstream selected", severity="error", timeout=2)
                return
            if not arg:
                self.notify("Usage: note <text>", timeout=2)
                return
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"[{timestamp}] {arg}"
            if ws.notes:
                ws.notes += "\n" + entry
            else:
                ws.notes = entry
            self.store.update(ws)
            self.notify(f"Note added to {ws.name}", timeout=2)

        # ── archive ──
        elif cmd in ("archive", "a"):
            if not ws:
                self.notify("No workstream selected", severity="error", timeout=2)
                return
            self.store.archive(ws.id)
            self._refresh_table()
            self.notify(f"Archived: {ws.name}", timeout=2)

        # ── unarchive ──
        elif cmd in ("unarchive", "ua"):
            if not ws:
                self.notify("No workstream selected", severity="error", timeout=2)
                return
            self.store.unarchive(ws.id)
            self._refresh_table()
            self.notify(f"Unarchived: {ws.name}", timeout=2)

        # ── delete ──
        elif cmd in ("delete", "del"):
            if ws:
                self.action_delete()

        # ── search ──
        elif cmd == "search":
            self.search_text = arg
            self._refresh_table()

        # ── sort ──
        elif cmd == "sort":
            valid = ("status", "updated", "created", "category", "name")
            if arg in valid:
                self.sort_mode = arg
                self._refresh_table()
            else:
                self.notify(f"Sort by: {', '.join(valid)}", severity="error", timeout=2)

        # ── filter ──
        elif cmd in ("filter", "f"):
            valid = ("all", "work", "personal", "active", "stale")
            if arg in valid:
                self.filter_mode = arg
                self._refresh_table()
            else:
                self.notify(f"Filter: {', '.join(valid)}", severity="error", timeout=2)

        # ── spawn ──
        elif cmd == "spawn":
            self.action_spawn()

        # ── resume ──
        elif cmd == "resume":
            self.action_resume()

        # ── export ──
        elif cmd == "export":
            self._do_export(arg)

        # ── brain ──
        elif cmd == "brain":
            if arg:
                self._do_brain(arg)
            else:
                self.action_brain_dump()

        # ── help ──
        elif cmd == "help":
            self.push_screen(HelpScreen())

        else:
            self.notify(f"Unknown command: {cmd}", severity="error", timeout=2)

    def _do_export(self, path: str = ""):
        """Export workstreams to markdown."""
        streams = self.store.active
        output = path or os.path.expanduser("~/workstreams/active.md")

        lines = [
            "# Active Workstreams",
            f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]
        for cat in Category:
            cat_streams = [w for w in streams if w.category == cat]
            if not cat_streams:
                continue
            lines.append(f"## {cat.value.title()}")
            lines.append("")
            cat_streams = self.store.sorted(cat_streams, "status")
            for ws in cat_streams:
                ws_icon = STATUS_ICONS[ws.status]
                lines.append(f"### {ws_icon} {ws.name}")
                lines.append(f"**Status:** {ws.status.value} | **Updated:** {_relative_time(ws.updated_at)}")
                if ws.description:
                    lines.append(f"\n{ws.description}")
                if ws.links:
                    lines.append("\n**Links:**")
                    for lnk in ws.links:
                        if lnk.kind == "url":
                            lines.append(f"- [{lnk.label}]({lnk.value})")
                        else:
                            lines.append(f"- `{lnk.kind}`: {lnk.value}")
                if ws.notes:
                    lines.append(f"\n**Notes:**\n{ws.notes}")
                lines.append("")

        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text("\n".join(lines) + "\n")
        self.notify(f"Exported {len(streams)} workstreams to {output}", timeout=3)

    # ── Other actions ──

    def action_refresh(self):
        self.store.load()
        self._refresh_table()
        self._poll_tmux()
        self.notify("Refreshed", timeout=1)

    def action_help(self):
        self.push_screen(HelpScreen())

    def _on_return_from_modal(self):
        self.store.load()
        self._refresh_table()


if __name__ == "__main__":
    OrchestratorApp().run()
