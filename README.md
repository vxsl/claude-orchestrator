# orch — Thought to Thread

A terminal-native workstation for agentic coding. Every git worktree is a workstream. Every workstream is a tab. Every thought becomes a running Claude session.

Built with Python and [Textual](https://textual.textualize.io/) with embedded libvterm terminals, a Rust session engine, and vim-first keybindings.

<img width="3023" height="1890" alt="2026-03-24_14-05" src="https://github.com/user-attachments/assets/449b11fa-6dfe-4188-87f4-b079c1e7c708" />

<img width="3023" height="1889" alt="2026-03-24_14-08" src="https://github.com/user-attachments/assets/f5f01fb0-7669-416c-882d-c4796f0f050d" />

## Philosophy

**Thought to thread.** The gap between "I have an idea" and "Claude is working on it" should be zero friction. Three paths, all converging:

1. **Brain dump** — `b`, type stream-of-consciousness, `l` to launch immediately
2. **From ticket** — `T`, fuzzy-search Jira, Enter to create workstream and open it
3. **Quick start** — `c` on any workstream, or `C` to pick a repo

## Quick Start

```bash
orch                    # Launch TUI (default)
python app.py           # Or run directly
```

## Tabbed Navigation

The app uses a tabbed interface — each opened workstream gets its own persistent tab.

| Key | Action |
|-----|--------|
| `Ctrl+B` / `Ctrl+X` | Next / prev tab |
| `Enter` / `l` | Open workstream in new tab |
| `x` | Close current tab |
| `Ctrl+H` / `Backspace` | Back / dismiss |

The **Home tab** (always present) shows the workstream list with:
- Activity icons auto-derived from session state (thinking, your turn, idle)
- Git branch name (yellow if dirty, +N/-N for ahead/behind)
- Jira ticket key + status, MR badge, ticket-solve status (when enriched)
- Session count, token usage, and relative timestamps
- Tig sidebar showing git activity
- Filter presets: Active (default), Work, Personal, All, Stale, Archived

A permanent **Sessions tab** shows all active sessions across workstreams.

## Keybindings

Press `?` from any view for **context-sensitive help** — shows what you're looking at and the keybindings for that view. Type to filter.

### Navigation

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up |
| `Ctrl+D` / `Ctrl+U` | Half-page down / up |
| `g` / `G` | Jump to top / bottom |
| `Ctrl+J` / `Ctrl+K` | Cycle panels |
| `/` | Search |
| `?` | Context-sensitive help |

### Workstream Actions

| Key | Action |
|-----|--------|
| `c` | New Claude session (with workstream context) |
| `r` | Resume most recent session |
| `a` | Add new workstream |
| `b` | Brain dump → parse → optionally launch |
| `n` | Quick todo |
| `e` | Full todo list |
| `E` | Rename |
| `W` | Add link |
| `o` | Open links |
| `u` | Archive / unarchive |
| `d` | Delete |

### Dev-Workflow Integration

| Key | Action |
|-----|--------|
| `P` | **Ship** — run oneshot (staged → branch → commit → MR) |
| `T` | **Ticket** — browse Jira tickets, link or create workstream |
| `B` | **Branches** — browse worktrees and recent branches |
| `C` | **Repo spawn** — pick a repo, then spawn Claude |

### Filters & Sort

| Key | Action |
|-----|--------|
| `1`–`6` | Filter: Active / Work / Personal / All / Stale / Archived |
| `F1`–`F5` | Sort: Activity / Updated / Created / Category / Name |

## Command Palette

Press `:` to open a **fuzzy-searchable command palette**. Type to filter across all 25+ commands — no need to remember exact names.

Commands include:

| Category | Commands |
|----------|----------|
| Sessions | `spawn`, `resume` |
| Workstreams | `add`, `brain`, `rename`, `archive`, `unarchive`, `delete` |
| Editing | `note`, `link`, `open` |
| Dev-workflow | `ship`, `ticket`, `ticket-create`, `solve`, `branches`, `files`, `wip`, `restage` |
| Navigation | `search`, `filter`, `sort`, `export`, `help`, `refresh` |

Commands that need a selected workstream are dimmed when none is selected.

## Claude Session Screen

When you launch or resume a Claude session, you get a full embedded terminal with:

- **Header** — live stats (title, model, think time, messages, tokens, context bar, last prompt)
- **Main terminal** — libvterm PTY with full scrollback via tmux-backed persistence
- **Sidebar** — tig status + tig log for git context
- **Footer** — session ID, cwd, git branch, keybinding hints

| Key | Action |
|-----|--------|
| `Ctrl+H` | Detach and go back (session keeps running in background) |
| `Ctrl+\` | Detach and go back (alternate) |
| `Ctrl+J/K` | Cycle between terminal and tig panels |
| `Ctrl+Z` | Zoom current panel full-screen |
| `Ctrl+E` | Extract todo from conversation |
| `Ctrl+Space` | Archive session and go back |
| `?` | Help |

Sessions are persistent — they survive detaching, closing orch, and restarts. The session process runs in a tmux backend and can be reattached at any time.

### Session Lifecycle

Sessions progress through states automatically:
- **thinking** — Claude is actively working (animated throbber, tinted background)
- **your turn** — Claude is waiting for input
- **committed** — session ended with a git commit (work landed)
- **archived** — filed away, always recoverable

### Session Intelligence

- **Context bar** — shows how full the context window is per session
- **Think time** — tracks and displays total Claude work time
- **Auto-discovery** — sessions grouped into threads by project, branch, and time
- **AI-powered titles** — threads get descriptive names from content
- **Notifications** — desktop alerts when sessions need attention
- **"Earlier" dividers** — today's sessions separated from older ones

## Worktree Auto-Discovery

Orch scans your known repos every 30 seconds for git worktrees. When it finds one:

1. **Auto-creates a workstream** named from the Jira ticket summary (via branch name convention `UB-1234-slug`) or the branch name
2. **Enriches the row** with live data from dev-workflow-tools caches:
   - Jira ticket status (color-coded: cyan for in-progress, green for done)
   - MR indicator (purple badge when a merge request exists)
   - ticket-solve status (yellow "solving" / green "solved")
3. **Auto-archives** workstreams whose worktree directories no longer exist

Category is auto-detected from the git remote: `gitlab` repos default to Work, `github` to Personal.

Skips `main`, `master`, `develop`, `dev`, `staging`, `production` branches.

## Auto-Session Discovery

Link a directory to a workstream (worktree or file link), and orch automatically finds all Claude sessions in that directory by scanning `~/.claude/projects/`. No manual session linking needed.

Sessions show live activity: **thinking** (animated cyan throbber), **your turn** (yellow), or idle.

## Dev-Workflow Tools

Orch integrates [dev-workflow-tools](~/bin/dev-workflow-tools) when available:

- **oneshot** — staged changes → branch → commit → MR in one command
- **publish-changes** — create GitLab MRs with Jira integration
- **jira-fzf** — Jira ticket browser (cache read, no API calls from orch)
- **ticket-solve** — headless Claude ticket solver with worktree creation
- **fzedit** — interactive file finder
- **rr.sh** branch data — worktree status, recent branches

These appear where contextually relevant — ticket actions from the home view, ship from workstreams with staged changes, branches from repos.

## Architecture

```
app.py                    — Textual shell: tabs, compose, event routing
├── widgets.py            — FuzzyPicker, ModalForm, TabBar, InlineInput
├── state.py              — AppState + TabManager + command registry (pure Python, no Textual)
├── screens.py            — Modal screens (Detail, Todo, Help, pickers, forms)
├── rendering.py          — Color palette, Rich markup, display formatting
├── actions.py            — Git status, Jira/MR/ticket-solve caches, worktree discovery
├── config.py             — Keybinding configuration with user overrides
├── models.py             — Workstream, Store, Category, Link, TodoItem
├── sessions.py           — Claude session discovery from JSONL + Rust engine bridge
├── terminal.py           — libvterm terminal emulation widget
├── vterm_backend.py      — libvterm FFI backend for terminal rendering
├── claude_session_screen.py — Embedded Claude terminal with live stats + tig sidebar
├── brain.py              — Stream-of-consciousness parser
├── threads.py            — Thread clustering and activity detection
├── thread_namer.py       — AI-powered thread titling (Haiku)
├── workstream_synthesizer.py — AI-powered thread-to-workstream grouping
├── description_refresher.py  — Periodic workstream description re-evaluation
├── notifications.py      — Desktop notification system
├── watcher.py            — File/session watcher for live updates
├── session_bridge.py     — Bridge to Rust session engine
├── cli.py                — CLI interface (orch command)
├── orch.py               — Entry point / launcher
└── tests/                — 630+ tests (state, models, actions, widgets, app)
```

### Rust Session Engine

A Rust binary (`rust/orch-session-engine/`) provides fast JSONL parsing and SQLite caching for session data. Gives ~19x warm speedup over pure Python parsing. Falls back to Python if not built.

### Design Principles

- **Modular split** — state.py has no Textual dependency (testable with fast sync tests)
- **Minimal blast radius** — each module is focused, changes stay contained
- **Code sharing** — FuzzyPicker and ModalForm used by all picker/form screens
- **Vim-first** — j/k navigation everywhere, `:` command palette, `/` search

## Dependencies

- **Python 3.12+**
- **Textual** — TUI framework
- **libvterm** (optional) — system library for terminal emulation. Falls back to pyte.
- **Rust** (optional) — for the session engine. Falls back to Python.
- **dev-workflow-tools** (optional) — `~/bin/dev-workflow-tools` for Jira/GitLab integration

## Data

- **Store:** `data.json` (in project directory)
- **Backups:** automatic, keeps last 20
- **Sessions:** `~/.claude/projects/<project>/<session>.jsonl`
- **Session cache:** SQLite via Rust engine (auto-managed)
- **Jira cache:** `~/.cache/jira-fzf/tickets.json` (read-only, from dev-workflow-tools)
- **MR cache:** `~/.cache/jira-fzf/mr_cache.json` (read-only, from dev-workflow-tools)
- **Ticket-solve:** `~/.cache/ticket-solve/<TICKET>.json` (read-only, from ticket-solve)
