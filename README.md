# orch — Claude Brain Thread Manager

A terminal-native hub for managing parallel threads of work across Claude sessions. Think of each thread as a topic you're exploring with Claude — the tool tracks context, auto-discovers sessions, and lets you resume any thread with a single keypress.

Built with Python and [Textual](https://textual.textualize.io/). Designed to match the fzedit/jira-fzf mellow color palette.

## Quick Start

```bash
# Launch: Alt+Space (xmonad NSP) or:
orch                    # TUI dashboard (default)
orch tui                # same thing
```

The TUI is the primary interface. Everything else is secondary.

## TUI Views

Press `Tab` to cycle between three views:

### Threads (main view)

Your brain threads — topics you're working on with Claude. Each thread shows:
- Status icon (colored: ● active, ◉ review, ✓ done, ✗ blocked, ○ queued)
- Name with inline indicators (⚡ live tmux, ⏰ stale, link type icons, session count)
- Category (work / personal / meta)
- Last updated time

The **preview pane** (right side, toggle with `p`) shows:
- Description
- **Auto-discovered Claude sessions** — activity summary (session count, messages, cost), recent thread titles
- Context (linked directories)
- Notes
- Timeline

### Sessions

All Claude sessions from `~/.claude/projects/`, sorted by recent activity. Shows:
- Session title
- Linked thread name (or project directory if unlinked)
- Model, cost, age

Press `r` to resume a session, `l` to link it to a thread.

### Archived

Threads you've archived. Press `u` to unarchive, `d` to delete permanently.

## Keybindings

### Navigation

| Key | Action |
|-----|--------|
| `j` / `k` / `↓` / `↑` | Move down / up |
| `Ctrl+N` / `Ctrl+P` | Move down / up (vim) |
| `Ctrl+D` / `Ctrl+U` | Half-page down / up |
| `g` / `G` | Jump to top / bottom |
| `Enter` | Open detail / resume session |
| `Tab` | Cycle views (Threads → Sessions → Archived) |
| `Escape` | Back / close / clear search |

### Thread Actions

| Key | Action |
|-----|--------|
| `r` | **Resume** — auto-finds the most recent Claude session for this thread and resumes it. Falls back to opening the linked directory. |
| `c` | **New session** — opens a prompt editor, then launches Claude with thread context injected |
| `n` | **Quick note** — inline timestamped note (type and press Enter) |
| `s` / `S` | Cycle status forward / backward |
| `a` | Add new thread |
| `b` | Brain dump — multi-line editor that parses stream-of-consciousness into threads |
| `E` | Rename thread (inline) |
| `e` | Edit notes (full editor) |
| `l` | Add link |
| `o` | Open links |
| `x` | Archive |
| `d` | Delete (with confirmation) |

### Filters & Sort

| Key | Action |
|-----|--------|
| `1`–`5` | Filter: All / Work / Personal / Active / Stale |
| `/` | Live search by name/description |
| `F1`–`F5` | Sort: Status / Updated / Created / Category / Name |

### Other

| Key | Action |
|-----|--------|
| `:` | Command palette (vim-style) |
| `p` | Toggle preview pane |
| `R` | Refresh all data |
| `?` | Help screen |
| `q` | Quit |

## Auto-Session Discovery

The killer feature. When you link a directory to a thread (worktree or file link), orch automatically finds all Claude sessions in that directory by scanning `~/.claude/projects/`. No manual session linking needed.

**How it works:**
1. You create a thread and add a directory link: `:link worktree:~/dev/my-project`
2. Orch scans `~/.claude/projects/` for sessions whose project path matches
3. The preview pane shows all matching sessions with activity stats
4. `r` resumes the most recent one automatically

Explicit `claude-session` links still work as manual overrides and take priority.

## Claude Session Wrapper

When you launch Claude from orch (`r` or `c`), it runs through `orch-claude` — a wrapper that provides:

### Header Bar
A 1-line tmux pane at the top showing:
```
 ORCH  FE testing deep dive  in-progress  work  │  45 msgs  2.3M  $34.50  │  Ctrl+D exit
```
Updates every 5 seconds with live stats from the session JSONL.

### Context Injection
Claude receives your thread context via `--append-system-prompt`:
```
You are working on the brain thread: "FE testing deep dive"
Description: Vitest DX + browser testing + coverage badge for Dove
Status: in-progress
Category: work
```

### Auto-Linking
When you exit Claude (`Ctrl+D` or `/exit`), the session ID is automatically linked back to the thread. No manual `orch link` needed.

### Post-Session Summary
```
── session ended ──
FE testing deep dive  │  45 msgs  ·  2.3M  ·  $34.50
✓ session linked to workstream
press any key to return to orch
```

## Command Palette

Press `:` for vim-style commands:

| Command | Action |
|---------|--------|
| `:status <status>` | Set status (queued, in-progress, awaiting-review, done, blocked) |
| `:link <kind:value>` | Add link (worktree, ticket, claude-session, file, url, slack) |
| `:note <text>` | Add timestamped note |
| `:search <query>` | Search threads |
| `:sort <mode>` | Sort (status, updated, created, category, name) |
| `:filter <mode>` | Filter (all, work, personal, active, stale) |
| `:archive` / `:unarchive` | Archive/restore |
| `:export [path]` | Export to markdown |
| `:brain <text>` | Parse brain dump inline |
| `:workstreams` / `:sessions` / `:archived` | Switch views |
| `:help` | Help screen |

## CLI Reference

The CLI is secondary to the TUI but useful for scripting and quick operations.

```bash
orch add "fix auth bug" -c work [-d desc] [-s status] [-l kind:value]
orch list [-c category] [-s status] [-S sort] [--search query] [--archived]
orch show <id>
orch status <id> <status>
orch brain "fix auth, review MR, deploy blocked" [-y]
orch link <id> kind:value [-l label]
orch note <id> "note text"
orch sessions [-p project] [-n limit]
orch resume <id>
orch spawn <id>
orch watch <id>
orch archive [id]
orch unarchive <id>
orch history
orch export [-o path]
orch import <file> [-y]
orch completions bash|zsh
```

## xmonad Integration

```
Alt+Space           → orch (main dashboard)
Alt+Ctrl+Space      → fzedit
Alt+Shift+Ctrl+Space → jira-fzf
```

All three NSP windows run inside tmux with `destroy-unattached` (closing the window kills the session for a fresh start next time). Copy mode works via `Alt+k`.

## Data

- **Store:** `~/dev/claude-orchestrator/data.json`
- **Backups:** `~/dev/claude-orchestrator/backups/` (auto before destructive ops, keeps last 20)
- **Sessions:** Auto-discovered from `~/.claude/projects/<project-dir>/<session-id>.jsonl`

## Architecture

```
orch (bash launcher)
├── cli.py          — argparse CLI (20+ commands, completions)
├── app.py          — Textual TUI (2300+ lines)
│   ├── 3 views: Threads, Sessions, Archived
│   ├── Preview pane with auto-session discovery
│   ├── Modal screens (detail, notes, links, add, brain dump, spawn, confirm)
│   └── vim bindings, command palette, live tmux polling
├── orch-claude     — bash wrapper for Claude sessions (header, context, auto-link)
├── orch-header     — bash live status bar for header pane
├── models.py       — Workstream, Store, Status, Category, Link
├── brain.py        — stream-of-consciousness parser
├── sessions.py     — Claude session discovery from JSONL
└── tests/          — 183 tests (models, brain, sessions, TUI)
```

## Tests

```bash
python -m pytest tests/ -v       # Run all 183 tests
python -m pytest tests/ -q       # Quick summary
```
