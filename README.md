# orch — Claude Session Orchestrator

A terminal-native dashboard for managing parallel workstreams across Claude sessions, worktrees, tickets, and notes. Built with Python and [Textual](https://textual.textualize.io/).

Think of it as `htop` for your development intent — one place to see what you're working on, where the context lives, and how to resume any thread of work.

## Installation

```bash
# Already installed — the symlink is at ~/bin/orch
ls -la ~/bin/orch  # -> ~/dev/claude-orchestrator/orch

# Optional: enable shell completions
eval "$(orch completions bash)"   # bash
eval "$(orch completions zsh)"    # zsh
```

Requires Python 3.12+ and the `textual` package.

## Quick Start

```bash
orch tui                                          # Launch the TUI dashboard
orch add "fix auth bug" -c work                   # Add a workstream
orch brain "fix auth, review MR, deploy blocked"  # Parse brain dump → workstreams
orch list                                         # List active workstreams
orch sessions                                     # Discover Claude sessions
```

## TUI Dashboard

Launch with `orch` (no args), `orch tui`, or `orch dash`. The TUI is the primary interface.

```
 ┌─────────────────────────────── orchestrator ────────────────────────────────┐
 │ 6 streams  ● 4  ✗ 0  ◉ 1  ✓ 0  1 stale                                   │
 │ [1:All] 2:Work 3:Personal 4:Active 5:Stale   Sort:Status                   │
 │ ID       Name                       Category Status            Age  Updated │
 │ b1f3eb82 Claude orchestrator v1 🔗1 meta     ● in-progress     3m   3m ago  │
 │ 83a85cb1 hud-daemon Claude usage 🔗2personal ● in-progress     3m   3m ago  │
 │ 16e3e676 FE testing deep dive 🔗2   work     ● in-progress     3m   3m ago  │
 │ a2de55c3 CLAUDE.md 🔗2              work     ◉ awaiting-review 3m   1m ago  │
 │ 34390e82 UB-6732 + UB-6730 🔗4 ⏰   work     ● in-progress     3m   3m ago  │
 │ 499addb9 AI Show & Tell 🔗1         work     ● in-progress     3m   3m ago  │
 │  4 items shown  │  ? help  b brain  : cmd  F1-F5 sort  1-5 filter           │
 └─────────────────────────────────────────────────────────────────────────────┘
```

### Keybindings

| Key | Action |
|-----|--------|
| `j`/`k`/`↑`/`↓` | Navigate rows |
| `g` / `G` | Jump to top / bottom |
| `Enter` | Open detail view |
| `a` | Add new workstream |
| `b` | Brain dump (multi-line, Ctrl+S to submit) |
| `s` / `S` | Cycle status forward / backward |
| `c` | Spawn Claude session in tmux |
| `r` | Resume linked session / open worktree |
| `l` | Add link to workstream |
| `e` | Edit notes |
| `o` | Open links (xdg-open, tmux, editor) |
| `x` | Archive workstream |
| `d` | Delete workstream (with confirmation) |
| `1`–`5` | Filter: All / Work / Personal / Active / Stale |
| `/` | Search by name (live filtering) |
| `:` | Command palette (vim-style) |
| `Escape` | Clear search / close screen |
| `F1`–`F5` | Sort: Status / Updated / Created / Category / Name |
| `R` | Refresh data from disk |
| `?` | Help screen |
| `q` | Quit |

### Command Palette

Press `:` to open the command palette. Type a command and press Enter.

| Command | Action |
|---------|--------|
| `:status <status>` | Set status of selected workstream |
| `:link <kind:value>` | Add link to selected |
| `:note <text>` | Add timestamped note to selected |
| `:archive` | Archive selected |
| `:unarchive` | Unarchive selected |
| `:search <query>` | Search workstreams |
| `:sort <mode>` | Sort (status/updated/created/category/name) |
| `:filter <mode>` | Filter (all/work/personal/active/stale) |
| `:spawn` | Spawn Claude session for selected |
| `:resume` | Resume linked session |
| `:brain <text>` | Parse brain dump inline |
| `:export [path]` | Export to markdown |
| `:help` | Show help screen |

### Live Session Status

Workstreams with active tmux sessions show a ⚡ indicator in the table. The TUI polls tmux every 30 seconds to detect live sessions by matching worktree paths and window names.

## Command Reference

### Core

```bash
orch add <name> [-d desc] [-c work|personal|meta] [-s status] [-l kind:value]
orch list [-c category] [-s status] [-S sort] [--search query] [--archived]
orch show <id>           # Full detail view (supports ID prefix: orch show ef7d)
orch status <id> <status>
```

### Brain Dump

Parse stream-of-consciousness text into structured workstreams:

```bash
orch brain "fix the auth bug, also review Logan's MR, and the deploy is blocked on the migration"
```

```
  Parsed 3 tasks:

  1. fix the auth bug
     ○ queued  work

  2. review Logan's MR
     ◉ awaiting-review  personal

  3. deploy blocked on migration
     ✗ blocked  work

  Add these 3 workstreams? [Y/n]
```

Splitting heuristics: commas, semicolons, "also", "don't forget", numbered lists, bullet points. Status inferred from keywords ("blocked", "review", "PR", "working on"). Category inferred from domain keywords ("deploy", "ticket", "API" → work; "tooling", "config" → meta).

Use `-y` to skip the confirmation prompt.

### Links

```bash
orch link <id> ticket:UB-1234
orch link <id> worktree:~/work/repos/ul
orch link <id> url:https://github.com/org/repo/pull/42 -l "PR #42"
orch link <id> claude-session:511de923-3d67-44ed-8c5e-46c8ef8bd09f
orch link <id> file:~/workstreams/notes.md
orch link <id> slack:C012345
```

**Link kinds:** `worktree`, `ticket`, `claude-session`, `file`, `url`, `slack`

In the TUI, press `o` to open links:
- **url** → `xdg-open`
- **worktree** → new tmux window at that path
- **file** → opens in `$EDITOR` via tmux (or `xdg-open`)
- **claude-session** → `claude --resume <id>` in tmux

### Notes

```bash
orch note <id> "Vitest config working, need browser testing next"
```

Notes are timestamped automatically. Edit in the TUI with `e`.

### Claude Sessions

```bash
orch sessions                    # Discover recent sessions
orch sessions -p orchestrator    # Filter by project name
orch sessions -n 5               # Limit results
```

```
  511de923  orchestrator-v2
    project: ~/dev/claude-orchestrator
    tokens: 7.6M  cost: $116.96  msgs: 107  model: claude-opus-4-6  age: 2h ago
```

Scans `~/.claude/projects/` for JSONL session files. Shows token usage, estimated cost, message count, and model.

```bash
orch link <id> claude-session:<session-id>    # Link session to workstream
orch resume <id>                               # Resume linked session
orch spawn <id>                                # New Claude session for workstream
```

### Tmux Integration

```bash
orch watch <id>    # Open tmux window at workstream's worktree
orch spawn <id>    # New Claude session in tmux with workstream context
orch resume <id>   # Resume linked Claude session in tmux
```

All three require being inside a tmux session.

### Archival

```bash
orch archive              # Archive all done workstreams
orch archive <id>         # Archive a specific workstream
orch unarchive <id>       # Restore an archived workstream
orch history              # List archived workstreams
orch list --archived      # Also works
```

Archived workstreams are hidden from the main view but kept in the data file.

### Export / Import

```bash
orch export                          # → ~/workstreams/active.md
orch export -o /tmp/work.md          # Custom path
orch import ~/workstreams/active.md  # Round-trip from export
orch import -y tasks.md              # Skip confirmation
```

Generates Obsidian-friendly markdown grouped by category. Import parses it back into workstreams.

### Shell Completions

```bash
orch completions bash    # Print bash completion script
orch completions zsh     # Print zsh completion script

# Activate:
eval "$(orch completions bash)"
eval "$(orch completions zsh)"
```

Completes commands, flags, and workstream IDs.

## Data

- **Store:** `~/dev/claude-orchestrator/data.json`
- **Backups:** `~/dev/claude-orchestrator/backups/` (auto-created before destructive ops, keeps last 20)
- **Format:** JSON with a `workstreams` array

Each workstream has: `id`, `name`, `description`, `status`, `category`, `links`, `notes`, `archived`, `created_at`, `updated_at`, `status_changed_at`.

**Statuses:** `queued` (○), `in-progress` (●), `awaiting-review` (◉), `done` (✓), `blocked` (✗)

**Categories:** `work`, `personal`, `meta`

## Architecture

```
orch (launcher)  →  cli.py (argparse, 20 commands)
                     ├── models.py (Workstream, Store, Status, Category, Link)
                     ├── brain.py (stream-of-consciousness parser)
                     ├── sessions.py (Claude session discovery)
                     └── app.py (Textual TUI)
```
