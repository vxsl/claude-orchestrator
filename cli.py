#!/usr/bin/env python3
"""CLI interface for the orchestrator — add workstreams, check status, launch TUI."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from models import (
    Category, Link, Store, TodoItem, Workstream,
    CATEGORY_COLORS,
    _relative_time,
)


# ─── ANSI color helpers ──────────────────────────────────────────────

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "gray": "\033[90m",
}


CATEGORY_ANSI = {
    Category.WORK: ANSI["blue"],
    Category.PERSONAL: ANSI["magenta"],
    Category.META: ANSI["gray"],
}

LINK_ICONS = {
    "worktree": "\U0001f333",
    "ticket": "\U0001f3ab",
    "claude-session": "\U0001f916",
    "slack": "\U0001f4ac",
    "file": "\U0001f4c4",
    "url": "\U0001f517",
}


def _c(color: str, text: str) -> str:
    return f"{ANSI.get(color, '')}{text}{ANSI['reset']}"


def _cat_str(cat: Category) -> str:
    color = CATEGORY_ANSI[cat]
    return f"{color}{cat.value}{ANSI['reset']}"


def _ws_line(ws: Workstream, show_age: bool = True) -> str:
    """Format a workstream as a single line."""
    icon = "\U0001f4e6" if ws.archived else "\u25cf"
    parts = [
        f"  {icon}",
        f"{_c('dim', '[')}{ws.id[:8]}{_c('dim', ']')}",
        f"{_c('bold', ws.name)}",
        f"({_cat_str(ws.category)})",
    ]
    if show_age:
        parts.append(_c("dim", _relative_time(ws.updated_at)))
    if ws.links:
        parts.append(_c("dim", f"\U0001f517{len(ws.links)}"))
    if ws.is_stale:
        parts.append(_c("yellow", "\u23f0"))
    return " ".join(parts)


def _resolve_ws(store: Store, ws_id: str) -> Workstream:
    """Resolve a workstream by ID/prefix or exit with error."""
    ws = store.get(ws_id)
    if not ws:
        print(_c("red", f"  Not found: {ws_id}"))
        sys.exit(1)
    return ws


# ─── Commands ────────────────────────────────────────────────────────

def cmd_add(args):
    store = Store()
    ws = Workstream(
        name=args.name,
        description=args.description or "",
        category=Category(args.category),
    )
    if args.link:
        for link_str in args.link:
            parts = link_str.split(":", 1)
            if len(parts) >= 2:
                ws.links.append(Link(kind=parts[0], label=parts[0], value=parts[1]))
    store.add(ws)
    print(f"  {_c('green', '\u2713')} Added: {_ws_line(ws, show_age=False)}")


def cmd_list(args):
    store = Store()

    if args.archived:
        streams = store.archived
    else:
        streams = store.active

    if args.category:
        streams = [w for w in streams if w.category.value == args.category]
    if args.search:
        q = args.search.lower()
        streams = [w for w in streams if q in w.name.lower() or q in w.description.lower()]

    sort_by = getattr(args, "sort", "updated") or "updated"
    streams = store.sorted(streams, sort_by)

    if not streams:
        print(_c("dim", "  No workstreams."))
        return

    if sort_by == "category":
        current_cat = None
        for ws in streams:
            if ws.category != current_cat:
                current_cat = ws.category
                print(f"\n  {_c('bold', _cat_str(ws.category).upper())}")
            print(_ws_line(ws))
    else:
        for ws in streams:
            print(_ws_line(ws))

    print(f"\n  {_c('dim', f'{len(streams)} items')}")


def cmd_show(args):
    """Show full details of a workstream."""
    store = Store()
    ws = _resolve_ws(store, args.id)

    print(f"\n  {_c('bold', ws.name)}")
    print(f"  {_cat_str(ws.category)}")
    print(f"  {_c('dim', 'ID:')} {ws.id}")
    print()
    print(f"  {_c('dim', 'Created:')}   {ws.created_at[:19]}  ({_relative_time(ws.created_at)})")
    print(f"  {_c('dim', 'Updated:')}   {ws.updated_at[:19]}  ({_relative_time(ws.updated_at)})")
    if ws.archived:
        print(f"  {_c('dim', 'Archived')}")
    print()

    print(f"  {_c('dim', 'Description:')}")
    print(f"  {ws.description or _c('dim', '(none)')}")
    print()

    print(f"  {_c('dim', f'Links ({len(ws.links)}):')}")
    if ws.links:
        for lnk in ws.links:
            icon = LINK_ICONS.get(lnk.kind, "\u2022")
            print(f"    {icon} [{lnk.kind}] {lnk.label}: {lnk.value}")
    else:
        print(f"    {_c('dim', '(none)')}")
    print()

    print(f"  {_c('dim', 'Notes:')}")
    if ws.notes:
        for line in ws.notes.split("\n"):
            print(f"  {line}")
    else:
        print(f"    {_c('dim', '(none)')}")
    print()


def cmd_archive_id(args):
    """Archive a specific workstream by ID."""
    store = Store()
    ws = _resolve_ws(store, args.id)
    store.archive(ws.id)
    print(f"  {_c('green', '\u2713')} Archived: {_c('bold', ws.name)}")


def cmd_tui(args):
    import logging
    log_file = Path("~/.cache/orch/debug.log").expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("orch").setLevel(logging.DEBUG)
    import traceback, os
    from app import OrchestratorApp
    try:
        OrchestratorApp().run()
    except Exception:
        crash_log = os.environ.get("ORCH_CRASH_LOG", "")
        if crash_log:
            with open(crash_log, "w") as f:
                traceback.print_exc(file=f)
        raise


def cmd_seed(args):
    """Seed with sample workstreams."""
    store = Store()
    if store.workstreams:
        print(f"  Store already has {len(store.workstreams)} workstreams. Use --force to overwrite.")
        if not args.force:
            return
        store.backup()

    store.workstreams = []

    tonight = [
        ("xmonad cleanup", "Clean WIP commits, remove PII, organize", Category.PERSONAL, True,
         [Link("file", "sandbox", "~/cleanup/xmonad/")]),
        ("bin cleanup", "Clean WIP commits, organize 17+ files, remove PII", Category.PERSONAL, True,
         [Link("file", "sandbox", "~/cleanup/bin/")]),
        (".dotfiles cleanup", "Clean WIP commits, remove PII, organize configs", Category.PERSONAL, True,
         [Link("file", "sandbox", "~/cleanup/dotfiles/")]),
        ("dev-workflow-tools cleanup", "Clean 5+ WIP commits, fix bash/zsh compat (PR #1)", Category.PERSONAL, True,
         [Link("file", "sandbox", "~/cleanup/dev-workflow-tools/"),
          Link("url", "PR #1", "https://github.com/vxsl/dev-workflow-tools/pull/1")]),
        ("FE testing deep dive", "Vitest DX + browser testing + coverage badge for Dove", Category.WORK, False,
         [Link("worktree", "ul main", "~/work/repos/ul"),
          Link("file", "prompt", "~/workstreams/01-fe-testing.md")]),
        ("CLAUDE.md", "FE-focused CLAUDE.md for work repo", Category.WORK, False,
         [Link("worktree", "ul main", "~/work/repos/ul"),
          Link("file", "prompt", "~/workstreams/02-claude-md.md")]),
        ("UB-6732 + UB-6730", "Complete calendar periods + expression-aware time ranges", Category.WORK, False,
         [Link("worktree", "UB-6668", "~/work/repos/ul.UB-6668-implement-new-metric-centric-time-handli"),
          Link("ticket", "UB-6732", "UB-6732"), Link("ticket", "UB-6730", "UB-6730"),
          Link("file", "prompt", "~/workstreams/05-ub-tickets.md")]),
        ("AI Show & Tell presentation", "Worktrees + AI workflow slides + speaker notes", Category.WORK, False,
         [Link("file", "prompt", "~/workstreams/04-presentation.md")]),
        ("Claude orchestrator v1", "TUI dashboard for managing parallel AI workstreams", Category.META, False,
         [Link("file", "source", "~/dev/claude-orchestrator/")]),
        ("hud-daemon Claude usage", "Add Claude usage at-a-glance to eww HUD", Category.PERSONAL, False,
         [Link("file", "sandbox", "~/cleanup/bin/hud-daemon"),
          Link("file", "prompt", "~/workstreams/06-hud-daemon.md")]),
    ]

    for name, desc, cat, archived, links in tonight:
        ws = Workstream(name=name, description=desc, category=cat, archived=archived, links=links)
        store.add(ws)

    print(f"  {_c('green', '\u2713')} Seeded {len(tonight)} workstreams.")


def cmd_brain(args):
    """Parse stream-of-consciousness text into workstreams."""
    from brain import parse_brain_dump

    text = " ".join(args.text)
    tasks = parse_brain_dump(text)

    if not tasks:
        print(_c("dim", "  No tasks found in input."))
        return

    print(f"\n  {_c('bold', f'Parsed {len(tasks)} tasks:')}\n")
    for i, task in enumerate(tasks, 1):
        print(f"  {_c('dim', str(i) + '.')} {_c('bold', task.name)}")
        print(f"     {_cat_str(task.category)}")
        if task.raw_text != task.name:
            print(f"     {_c('dim', task.raw_text[:80])}")
        print()

    if args.yes:
        answer = "y"
    else:
        try:
            answer = input(f"  Add these {len(tasks)} workstreams? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return

    if answer and answer not in ("y", "yes", ""):
        print("  Cancelled.")
        return

    store = Store()
    for task in tasks:
        ws = Workstream(
            name=task.name,
            description=task.raw_text,
            category=task.category,
        )
        store.add(ws)
    print(f"\n  {_c('green', '\u2713')} Added {len(tasks)} workstreams.")


def cmd_link(args):
    """Add a link to a workstream."""
    store = Store()
    ws = _resolve_ws(store, args.id)

    parts = args.link.split(":", 1)
    if len(parts) < 2:
        print(_c("red", "  Link format: kind:value (e.g. ticket:UB-1234)"))
        print(_c("dim", "  Kinds: worktree, ticket, claude-session, file, url, slack"))
        sys.exit(1)

    kind, value = parts
    label = args.label or kind
    ws.add_link(kind=kind, value=value, label=label)
    store.update(ws)
    print(f"  {_c('green', '\u2713')} Added {kind} link to {_c('bold', ws.name)}: {value}")


def cmd_note(args):
    """Add a todo item to a workstream (same as TUI 'n' key)."""
    store = Store()
    ws = _resolve_ws(store, args.id)

    note_text = " ".join(args.text)
    if not note_text.strip():
        print(_c("red", "  Note text cannot be empty"))
        sys.exit(1)

    item = TodoItem(text=note_text.strip())
    ws.todos.append(item)
    store.update(ws)
    print(f"  {_c('green', '\u2713')} Todo added to {_c('bold', ws.name)}: {note_text.strip()}")


def cmd_archive(args):
    """Archive done workstreams or a specific workstream."""
    store = Store()

    if args.id:
        ws = _resolve_ws(store, args.id)
        store.archive(ws.id)
        print(f"  {_c('green', '\u2713')} Archived: {_c('bold', ws.name)}")
    else:
        print(_c("dim", "  Specify a workstream ID to archive."))


def cmd_unarchive(args):
    """Restore an archived workstream."""
    store = Store()
    ws = _resolve_ws(store, args.id)
    if not ws.archived:
        print(_c("dim", f"  {ws.name} is not archived."))
        return
    store.unarchive(ws.id)
    print(f"  {_c('green', '\u2713')} Restored: {_c('bold', ws.name)}")


def cmd_history(args):
    """Show archived workstreams."""
    store = Store()
    archived = store.archived
    if not archived:
        print(_c("dim", "  No archived workstreams."))
        return

    print(f"\n  {_c('bold', f'{len(archived)} archived workstreams:')}\n")
    for ws in archived:
        print(_ws_line(ws))
    print()


def cmd_sessions(args):
    """Discover and display Claude sessions."""
    from sessions import discover_sessions

    project = args.project or ""
    limit = args.limit or 20

    sessions = discover_sessions(
        limit=limit,
        project_filter=project,
        min_messages=1,
    )

    if not sessions:
        print(_c("dim", "  No sessions found."))
        return

    print()
    for s in sessions:
        title = s.display_name
        tokens = s.tokens_display
        model = s.model or "unknown"

        print(
            f"  {_c('cyan', s.session_id[:8])}  "
            f"{_c('bold', title[:50])}"
        )
        print(
            f"    {_c('dim', 'project:')} {s.project_path.replace(str(Path.home()), '~')}"
        )
        print(
            f"    {_c('dim', 'tok:')} {tokens}  "
            f"{_c('dim', 'msgs:')} {s.message_count}  "
            f"{_c('dim', 'model:')} {model}  "
            f"{_c('dim', 'age:')} {s.age}"
        )
        print()

    print(f"  {_c('dim', f'{len(sessions)} sessions')}")


def cmd_resume(args):
    """Resume a Claude session linked to a workstream."""
    store = Store()
    ws = _resolve_ws(store, args.id)

    session_links = [lnk for lnk in ws.links if lnk.kind == "claude-session"]
    if not session_links:
        print(_c("yellow", f"  No Claude session linked to {_c('bold', ws.name)}"))
        print(_c("dim", f"  Link one with: orch link {ws.id} claude-session:<session-id>"))
        return

    session_id = session_links[-1].value
    print(f"  Resuming session {_c('cyan', session_id[:12])} for {_c('bold', ws.name)}...")

    if os.environ.get("TMUX"):
        subprocess.run(
            ["tmux", "new-window", "-n", f"claude:{ws.name[:20]}",
             "claude", "--resume", session_id],
        )
    else:
        os.execvp("claude", ["claude", "--resume", session_id])


def cmd_watch(args):
    """Open a tmux window at the workstream's worktree."""
    if not os.environ.get("TMUX"):
        print(_c("red", "  Not in a tmux session."))
        sys.exit(1)

    store = Store()
    ws = _resolve_ws(store, args.id)

    worktree_links = [lnk for lnk in ws.links if lnk.kind == "worktree"]
    if not worktree_links:
        print(_c("yellow", f"  No worktree linked to {_c('bold', ws.name)}"))
        print(_c("dim", f"  Link one with: orch link {ws.id} worktree:/path/to/repo"))
        return

    path = os.path.expanduser(worktree_links[0].value)
    if not os.path.isdir(path):
        print(_c("yellow", f"  Worktree path does not exist: {path}"))
        return
    print(f"  Opening tmux window for {_c('bold', ws.name)} at {path}...")
    subprocess.run(["tmux", "new-window", "-n", ws.name[:20], "-c", path])


def cmd_spawn(args):
    """Spawn a new Claude session in a tmux pane for a workstream."""
    if not os.environ.get("TMUX"):
        print(_c("red", "  Not in a tmux session."))
        sys.exit(1)

    store = Store()
    ws = _resolve_ws(store, args.id)

    # Determine working directory from links
    cwd = None
    for link in ws.links:
        if link.kind in ("worktree", "file"):
            expanded = os.path.expanduser(link.value)
            if os.path.isdir(expanded):
                cwd = expanded
                break

    cwd = cwd or os.getcwd()

    prompt = f"Working on: {ws.name}"
    if ws.description:
        prompt += f"\nDescription: {ws.description}"

    print(f"  Spawning Claude for {_c('bold', ws.name)} in {cwd}...")
    subprocess.run([
        "tmux", "new-window", "-n", f"\U0001f916{ws.name[:18]}",
        "-c", cwd,
        "claude", "-p", prompt,
    ])


def cmd_distill(args):
    """Distill session context — compact or crystallize."""
    store = Store()

    # Resolve workstream: explicit arg > env var
    ws_id = getattr(args, "ws_id", None) or os.environ.get("ORCH_WS_ID")
    if not ws_id:
        print(_c("red", "  No workstream ID. Set ORCH_WS_ID or pass --ws-id."))
        sys.exit(1)
    ws = _resolve_ws(store, ws_id)

    mode = args.distill_mode

    if mode == "crystallize":
        text = args.text
        context = args.context or ""

        # Read from stdin if --context is "-"
        if context == "-":
            context = sys.stdin.read()

        todo = TodoItem(text=text, context=context, origin="crystallized")
        ws.todos.append(todo)
        store.update(ws)
        print(f"  {_c('green', '✓')} Crystallized todo on {_c('bold', ws.name)}: {text}")
        if context:
            lines = context.strip().split("\n")
            preview = lines[0][:80] + ("..." if len(lines[0]) > 80 or len(lines) > 1 else "")
            print(f"    {_c('dim', 'context:')} {preview}")

    elif mode == "compact":
        summary = args.summary
        # Read from stdin if --summary is "-"
        if summary == "-":
            summary = sys.stdin.read()

        cont_dir = Path.home() / ".cache" / "claude-orchestrator" / "continuations"
        cont_dir.mkdir(parents=True, exist_ok=True)
        cont_file = cont_dir / f"{ws.id}.md"
        cont_file.write_text(summary)
        print(f"  {_c('green', '✓')} Continuation context saved for {_c('bold', ws.name)}")
        print(f"    {_c('dim', 'Next session on this workstream will pick it up automatically.')}")

    else:
        print(_c("red", f"  Unknown distill mode: {mode}"))
        sys.exit(1)


def cmd_export(args):
    """Export active workstreams as markdown for Obsidian."""
    store = Store()
    streams = store.active

    output = args.output or os.path.expanduser("~/workstreams/active.md")

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

        cat_streams = store.sorted(cat_streams, "status")
        for ws in cat_streams:
            lines.append(f"### {ws.name}")
            lines.append(f"**Updated:** {_relative_time(ws.updated_at)}")
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
    print(f"  {_c('green', '\u2713')} Exported {len(streams)} workstreams to {output}")


def cmd_import(args):
    """Import workstreams from a markdown file (reverse of export)."""
    filepath = os.path.expanduser(args.file)
    if not os.path.isfile(filepath):
        print(_c("red", f"  File not found: {filepath}"))
        sys.exit(1)

    lines_raw = Path(filepath).read_text().split("\n")
    imported = []
    current_category = Category.PERSONAL
    current_ws = None

    in_notes = False
    in_links = False

    for line in lines_raw:
        stripped = line.strip()

        # Category header: ## Work
        if stripped.startswith("## "):
            cat_name = stripped.lstrip("# ").strip().lower()
            for cat in Category:
                if cat.value == cat_name:
                    current_category = cat
            continue

        # Skip document title
        if stripped.startswith("# ") and not stripped.startswith("## "):
            continue

        # Workstream header: ### ● name
        if stripped.startswith("### "):
            # Save previous workstream
            if current_ws:
                current_ws.notes = current_ws.notes.strip()
                imported.append(current_ws)

            title = stripped[4:].strip()
            # Remove status icon prefix (unicode chars before first word char)
            name = re.sub(r"^[^\w]*\s*", "", title).strip()
            if not name:
                current_ws = None
                continue

            current_ws = Workstream(
                name=name,
                category=current_category,
            )
            in_notes = False
            in_links = False
            continue

        if not current_ws:
            continue

        # Legacy status line or updated line — skip
        if stripped.startswith("**Status:**") or stripped.startswith("**Updated:**"):
            continue

        # Notes section
        if stripped == "**Notes:**":
            in_notes = True
            in_links = False
            continue

        # Links section
        if stripped == "**Links:**":
            in_links = True
            in_notes = False
            continue

        if in_notes:
            current_ws.notes += line.strip() + "\n"
            continue

        if in_links:
            # - `kind`: value
            match = re.match(r"- `(\w[\w-]*)`: (.+)", stripped)
            if match:
                current_ws.links.append(Link(kind=match.group(1), label=match.group(1), value=match.group(2)))
                continue
            # - [label](url)
            match = re.match(r"- \[(.+?)\]\((.+?)\)", stripped)
            if match:
                current_ws.links.append(Link(kind="url", label=match.group(1), value=match.group(2)))
                continue

        # Description (non-empty lines not caught above)
        if stripped and not stripped.startswith("*Exported"):
            if current_ws.description:
                current_ws.description += " " + stripped
            else:
                current_ws.description = stripped

    # Don't forget the last workstream
    if current_ws:
        current_ws.notes = current_ws.notes.strip()
        imported.append(current_ws)

    if not imported:
        print(_c("dim", "  No workstreams found in file."))
        return

    print(f"\n  {_c('bold', f'Found {len(imported)} workstreams:')}\n")
    for ws in imported:
        print(f"  {_ws_line(ws, show_age=False)}")
    print()

    if args.yes:
        answer = "y"
    else:
        try:
            answer = input(f"  Import these {len(imported)} workstreams? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return

    if answer and answer not in ("y", "yes", ""):
        print("  Cancelled.")
        return

    store = Store()
    for ws in imported:
        store.add(ws)
    print(f"  {_c('green', '\u2713')} Imported {len(imported)} workstreams.")


# ─── Shell completion ────────────────────────────────────────────────

def cmd_completions(args):
    """Generate shell completion script."""
    shell = args.shell or "bash"

    if shell == "bash":
        print(_bash_completions())
    elif shell == "zsh":
        print(_zsh_completions())
    else:
        print(_c("red", f"  Unsupported shell: {shell}"))
        print(_c("dim", "  Supported: bash, zsh"))
        sys.exit(1)


def _bash_completions() -> str:
    return """\
# bash completion for orch
# Add to ~/.bashrc:  eval "$(orch completions bash)"
_orch_completions() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="tui dash add list ls show brain link note archive unarchive history sessions resume watch spawn export import seed completions"

    case "$prev" in
        orch)
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
            return 0
            ;;
        -c|--category)
            COMPREPLY=( $(compgen -W "work personal meta" -- "$cur") )
            return 0
            ;;
        -S|--sort)
            COMPREPLY=( $(compgen -W "updated created category name" -- "$cur") )
            return 0
            ;;
        --shell)
            COMPREPLY=( $(compgen -W "bash zsh" -- "$cur") )
            return 0
            ;;
        show|link|note|archive|unarchive|resume|watch|spawn)
            # Complete with workstream IDs
            local ids
            ids=$(orch list 2>/dev/null | grep -oP '\\[\\K[a-f0-9]{8}' | sort -u)
            COMPREPLY=( $(compgen -W "$ids" -- "$cur") )
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        local opts="-h --help"
        case "${COMP_WORDS[1]}" in
            add)  opts="-h -d --description -c --category -l --link --archived" ;;
            list|ls)  opts="-h -c --category -S --sort --search --archived" ;;
            link) opts="-h -l --label" ;;
            sessions) opts="-h -p --project -n --limit" ;;
            export) opts="-h -o --output" ;;
            brain) opts="-h -y --yes" ;;
            import) opts="-h -y --yes" ;;
            seed) opts="-h --force" ;;
            completions) opts="-h --shell" ;;
        esac
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi
}
complete -F _orch_completions orch"""


def _zsh_completions() -> str:
    return """\
# zsh completion for orch
# Add to ~/.zshrc:  eval "$(orch completions zsh)"
_orch() {
    local -a commands statuses categories sorts shells

    commands=(
        'tui:Launch the TUI dashboard'
        'dash:Launch the TUI dashboard (alias)'
        'add:Add a workstream'
        'list:List workstreams'
        'ls:List workstreams (alias)'
        'show:Show full details of a workstream'
        'brain:Parse stream-of-consciousness text into workstreams'
        'link:Add a link to a workstream'
        'note:Add a note to a workstream'
        'archive:Archive done workstreams (or specific by ID)'
        'unarchive:Restore an archived workstream'
        'history:Show archived workstreams'
        'sessions:Discover Claude sessions'
        'resume:Resume a linked Claude session'
        'watch:Open a tmux window for a workstream worktree'
        'spawn:Spawn a new Claude session for a workstream'
        'export:Export active workstreams as markdown'
        'import:Import workstreams from a markdown file'
        'seed:Seed with sample workstreams'
        'completions:Generate shell completion script'
    )
    categories=(work personal meta)
    sorts=(updated created category name)
    shells=(bash zsh)

    _arguments -C \\
        '1:command:->command' \\
        '*::arg:->args'

    case "$state" in
        command)
            _describe 'command' commands
            ;;
        args)
            case "${words[1]}" in
                add)
                    _arguments \\
                        '1:name:' \\
                        '-d[Description]:description:' \\
                        '-c[Category]:category:($categories)' \\
                        '--archived[Create as archived]' \\
                        '*-l[Link]:link:'
                    ;;
                list|ls)
                    _arguments \\
                        '-c[Category]:category:($categories)' \\
                        '-S[Sort]:sort:($sorts)' \\
                        '--search[Search]:query:' \\
                        '--archived[Show archived]'
                    ;;
                show|link|note|archive|unarchive|resume|watch|spawn)
                    _arguments '1:id:'
                    ;;
                brain)
                    _arguments '1:text:' '-y[Skip confirmation]'
                    ;;
                import)
                    _arguments '1:file:_files' '-y[Skip confirmation]'
                    ;;
                sessions)
                    _arguments \\
                        '-p[Project filter]:project:' \\
                        '-n[Limit]:limit:'
                    ;;
                export)
                    _arguments '-o[Output file]:file:_files'
                    ;;
                completions)
                    _arguments '--shell[Shell]:shell:($shells)'
                    ;;
                seed)
                    _arguments '--force[Overwrite existing]'
                    ;;
            esac
            ;;
    esac
}
compdef _orch orch"""


# ─── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="orch",
        description="Claude Orchestrator \u2014 manage parallel workstreams",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
quick start:
  orch tui                             Launch the TUI dashboard
  orch list                            List all active workstreams
  orch add "fix auth bug" -c work      Add a new workstream
  orch brain "fix auth, review MR"     Parse brain dump into workstreams
  orch sessions                        Discover Claude sessions

use 'orch <command> --help' for detailed help on each command.
""",
    )
    sub = parser.add_subparsers(dest="command")

    # tui / dash
    p_tui = sub.add_parser("tui", help="Launch the TUI dashboard",
        epilog="  Keyboard reference: press ? inside the TUI")
    sub.add_parser("dash", help="Launch the TUI dashboard (alias for tui)")

    # add
    p_add = sub.add_parser("add", help="Add a workstream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch add "fix login bug"
  orch add "deploy v2.1" -c work
  orch add "review PR" -c work -l url:https://github.com/org/repo/pull/42
  orch add "update docs" -d "API docs are out of date" -l file:~/docs/api.md
""")
    p_add.add_argument("name", help="Name of the workstream")
    p_add.add_argument("-d", "--description", default="", help="Description")
    p_add.add_argument("-c", "--category", choices=[c.value for c in Category], default="personal",
                       help="Category (default: personal)")
    p_add.add_argument("--archived", action="store_true", default=False,
                       help="Create as archived")
    p_add.add_argument("-l", "--link", action="append",
                       help="Link as kind:value (e.g. ticket:UB-1234)")

    # list
    p_list = sub.add_parser("list", aliases=["ls"], help="List workstreams",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch list                            All active workstreams
  orch list -c work                    Only work items
  orch list -S updated                 Sort by last updated
  orch list --search "auth"            Search by name/description
  orch list --archived                 Show archived items
  orch list -c work -S category        Work items sorted by category
""")
    p_list.add_argument("-c", "--category", choices=[c.value for c in Category],
                       help="Filter by category")
    p_list.add_argument("-S", "--sort", choices=["updated", "created", "category", "name"],
                       default="updated", help="Sort order (default: updated)")
    p_list.add_argument("--search", help="Search by name/description")
    p_list.add_argument("--archived", action="store_true", help="Show archived items instead")

    # show
    p_show = sub.add_parser("show", help="Show full details of a workstream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch show ef7d63d0                   Show by full ID
  orch show ef7d                       Show by ID prefix (if unambiguous)
""")
    p_show.add_argument("id", help="Workstream ID (or unique prefix)")

    # brain
    p_brain = sub.add_parser("brain", help="Parse stream-of-consciousness text into workstreams",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch brain "fix the auth bug, also review Logan's MR"
  orch brain "deploy is blocked on migration, need to write tests"
  orch brain "1. fix auth 2. update docs 3. deploy v2"
  orch brain -y "quick task, another task"    (skip confirmation)

the parser splits on commas, semicolons, 'also', 'and then', numbered
lists, and bullet points. it infers status from keywords like 'blocked',
'review', 'working on'. it infers category from keywords like 'deploy',
'ticket', 'PR' (work) or 'tooling', 'config' (meta).
""")
    p_brain.add_argument("text", nargs="+", help="Your brain dump text")
    p_brain.add_argument("-y", "--yes", action="store_true",
                        help="Skip confirmation prompt")

    # link
    p_link = sub.add_parser("link", help="Add a link to a workstream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch link ef7d ticket:UB-1234
  orch link ef7d worktree:~/work/repos/ul
  orch link ef7d url:https://github.com/org/repo/pull/42 -l "PR #42"
  orch link ef7d claude-session:511de923-3d67-44ed-8c5e-46c8ef8bd09f
  orch link ef7d file:~/workstreams/notes.md

link kinds: worktree, ticket, claude-session, file, url, slack
""")
    p_link.add_argument("id", help="Workstream ID (or prefix)")
    p_link.add_argument("link", help="kind:value (e.g. ticket:UB-1234)")
    p_link.add_argument("-l", "--label", help="Custom label for the link")

    # note
    p_note = sub.add_parser("note", help="Add a todo item to a workstream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch note ef7d "Vitest config working, need browser testing next"
  orch note ef7d "Blocked on migration, talked to Logan"
""")
    p_note.add_argument("id", help="Workstream ID (or prefix)")
    p_note.add_argument("text", nargs="+", help="Note text")

    # archive
    p_archive = sub.add_parser("archive", help="Archive done workstreams (or specific by ID)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch archive                         Archive all done workstreams
  orch archive ef7d                    Archive a specific workstream
""")
    p_archive.add_argument("id", nargs="?",
                          help="Workstream ID to archive (omit to archive all done)")

    # unarchive
    p_unarchive = sub.add_parser("unarchive", help="Restore an archived workstream",
        epilog="  example: orch unarchive ef7d")
    p_unarchive.add_argument("id", help="Workstream ID (or prefix)")

    # history
    sub.add_parser("history", help="Show archived workstreams")

    # sessions
    p_sessions = sub.add_parser("sessions", help="Discover Claude sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch sessions                        Show recent sessions
  orch sessions -n 5                   Show last 5 sessions
  orch sessions -p orchestrator        Filter by project name

scans ~/.claude/projects/ for JSONL session files. shows token usage,
token usage, message count, and last activity.
""")
    p_sessions.add_argument("-p", "--project", help="Filter by project path substring")
    p_sessions.add_argument("-n", "--limit", type=int, default=20,
                           help="Max sessions to show (default: 20)")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a linked Claude session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
resumes the most recently linked Claude session for a workstream.
link a session first with: orch link <id> claude-session:<session-id>

in tmux: opens a new window. outside tmux: replaces current process.
""")
    p_resume.add_argument("id", help="Workstream ID (or prefix)")

    # watch
    p_watch = sub.add_parser("watch", help="Open a tmux window for a workstream's worktree",
        epilog="  requires tmux. opens the first linked worktree path.")
    p_watch.add_argument("id", help="Workstream ID (or prefix)")

    # spawn
    p_spawn = sub.add_parser("spawn", help="Spawn a new Claude session for a workstream",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
creates a new tmux window running 'claude' with the workstream's name
and description as context. uses the worktree path as working directory.
""")
    p_spawn.add_argument("id", help="Workstream ID (or prefix)")

    # distill
    p_distill = sub.add_parser("distill", help="Distill session context (compact or crystallize)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch distill crystallize --text "Refactor auth middleware" --context "..."
  orch distill compact --summary "We investigated X and decided Y..."
  echo "long context" | orch distill crystallize --text "task" --context -

auto-detects workstream from ORCH_WS_ID env var (set by orch-claude),
or pass --ws-id explicitly.
""")
    p_distill.add_argument("distill_mode", choices=["compact", "crystallize"],
                           help="compact: save context for next session. crystallize: save as todo.")
    p_distill.add_argument("--ws-id", dest="ws_id",
                           help="Workstream ID (default: ORCH_WS_ID env var)")
    p_distill.add_argument("--text", "-t",
                           help="Todo text (crystallize mode)")
    p_distill.add_argument("--context", "-c",
                           help="Detailed context (crystallize) or ignored. Use '-' for stdin.")
    p_distill.add_argument("--summary", "-s",
                           help="Continuation summary (compact mode). Use '-' for stdin.")

    # export
    p_export = sub.add_parser("export", help="Export active workstreams as markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch export                          Export to ~/workstreams/active.md
  orch export -o /tmp/work.md          Export to custom path

generates Obsidian-friendly markdown grouped by category.
""")
    p_export.add_argument("-o", "--output",
                         help="Output file path (default: ~/workstreams/active.md)")

    # import
    p_import = sub.add_parser("import", help="Import workstreams from a markdown file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch import ~/workstreams/active.md
  orch import -y tasks.md              Skip confirmation

parses markdown exported by 'orch export'. each ### heading becomes a
workstream. supports round-trip with Obsidian.
""")
    p_import.add_argument("file", help="Markdown file to import")
    p_import.add_argument("-y", "--yes", action="store_true",
                         help="Skip confirmation prompt")

    # seed
    p_seed = sub.add_parser("seed", help="Seed with sample workstreams",
        epilog="  use --force to overwrite existing data (creates backup first)")
    p_seed.add_argument("--force", action="store_true",
                       help="Overwrite existing workstreams")

    # completions
    p_comp = sub.add_parser("completions", help="Generate shell completion script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  orch completions bash                Print bash completions
  orch completions zsh                 Print zsh completions
  eval "$(orch completions bash)"      Activate in current shell
""")
    p_comp.add_argument("shell", nargs="?", default="bash",
                       choices=["bash", "zsh"],
                       help="Shell type (default: bash)")

    args = parser.parse_args()

    commands = {
        "tui": cmd_tui,
        "dash": cmd_tui,
        "add": cmd_add,
        "list": cmd_list,
        "ls": cmd_list,
        "show": cmd_show,
        "brain": cmd_brain,
        "link": cmd_link,
        "note": cmd_note,
        "archive": cmd_archive,
        "unarchive": cmd_unarchive,
        "history": cmd_history,
        "sessions": cmd_sessions,
        "resume": cmd_resume,
        "watch": cmd_watch,
        "spawn": cmd_spawn,
        "distill": cmd_distill,
        "export": cmd_export,
        "import": cmd_import,
        "seed": cmd_seed,
        "completions": cmd_completions,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            handler(args)
        except KeyboardInterrupt:
            print()
        except SystemExit:
            raise
        except Exception as e:
            print(f"\n  {_c('red', 'Error:')} {e}")
            sys.exit(1)
    elif args.command is None:
        # No command given — launch TUI by default
        cmd_tui(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
