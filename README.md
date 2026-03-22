# orch — Thought to Thread

A terminal-native workstation for agentic coding. Every git worktree is a workstream. Every workstream is a tab. Every thought becomes a running Claude session.

Built with Python and [Textual](https://textual.textualize.io/) with embedded libvterm terminals, mellow GitHub Dark palette, and vim-first keybindings.

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
| `Tab` / `Shift+Tab` | Cycle through open tabs |
| `Enter` / `Ctrl+L` | Open workstream in new tab |
| `Ctrl+W` | Close current tab |
| `Ctrl+H` / `Backspace` | Back / dismiss |

The **Home tab** (always present) shows the workstream list with:
- Status icons and activity indicators
- Git branch name (yellow if dirty, +N/-N for ahead/behind)
- Session count and token usage
- Preview pane with sessions, notes, and context

## Keybindings

### Navigation

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up |
| `Ctrl+D` / `Ctrl+U` | Half-page down / up |
| `g` / `G` | Jump to top / bottom |
| `Ctrl+J` / `Ctrl+K` | Cycle panels |
| `/` | Search |
| `?` | Searchable help (fuzzy-filter all keybindings) |

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
| `L` | Add link |
| `o` | Open links |
| `x` | Archive / unarchive |
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
| `1`–`6` | Filter: All / Work / Personal / Active / Stale / Archived |
| `F1`–`F5` | Sort: Status / Updated / Created / Category / Name |

## Command Palette

Press `:` for vim-style commands. All dev-workflow tools are accessible:

```
:ship / :oneshot / :publish    Ship staged changes
:ticket / :jira [query]        Browse Jira tickets
:tc [title]                    Create Jira ticket
:solve UB-1234                 Run ticket-solve headlessly
:branches                      Browse branches & worktrees
:files                         Open file picker (fzedit)
:wip                           Quick WIP commit
:restage                       Unstage WIP commits

:status <status>               Set workstream status
:link <kind:value>             Add link
:note <text>                   Add todo
:search <query>                Search
:sort / :filter                Sort/filter workstreams
:export [path]                 Export to markdown
:brain <text>                  Parse brain dump inline
:help                          Help
```

## Claude Session Screen

When you launch or resume a Claude session, you get a full embedded terminal with:

- **3-line header** — live stats (title, model, elapsed, messages, tokens, tool usage)
- **Main terminal** — libvterm PTY with full scrollback and mouse support
- **Sidebar** — tig status + tig log for git context
- **Footer** — session ID, cwd, git branch, keybinding hints

| Key | Action |
|-----|--------|
| `Ctrl+E` | Extract todo from conversation |
| `Ctrl+J/K` | Cycle between terminal and tig panels |
| `Ctrl+H` | Detach (process survives, reattach later) |
| `Ctrl+D` | Exit session |

## Auto-Session Discovery

Link a directory to a workstream (worktree or file link), and orch automatically finds all Claude sessions in that directory by scanning `~/.claude/projects/`. No manual session linking needed.

Sessions show live activity: **thinking** (pulsing cyan), **your turn** (yellow badge), or idle.

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
app.py              — Textual shell: tabs, compose, event routing
├── widgets.py      — FuzzyPicker, ModalForm, TabBar, InlineInput
├── state.py        — AppState + TabManager (pure Python, no Textual)
├── screens.py      — Modal screens (Detail, Todo, pickers, forms)
├── rendering.py    — Color palette, Rich markup, display formatting
├── actions.py      — Git status, Jira cache, dev-tool integration
├── config.py       — Keybinding configuration with user overrides
├── models.py       — Workstream, Store, Status, Category, Link
├── sessions.py     — Claude session discovery from JSONL
├── terminal.py     — libvterm/pyte terminal emulation
├── claude_session_screen.py — Embedded Claude terminal with live stats
├── brain.py        — Stream-of-consciousness parser
├── threads.py      — Thread clustering and activity detection
└── tests/          — 270+ tests (state, models, actions, widgets, app)
```

### Design Principles

- **Modular split** — state.py has no Textual dependency (testable with fast sync tests)
- **Minimal blast radius** — each module is focused, changes stay contained
- **Code sharing** — FuzzyPicker and ModalForm used by all picker/form screens
- **Vim-first** — j/k navigation everywhere, `:` command palette, `/` search

## Dependencies

- **Python 3.12+**
- **Textual** — TUI framework
- **libvterm** (optional) — system library for terminal emulation. Falls back to pyte.
- **dev-workflow-tools** (optional) — `~/bin/dev-workflow-tools` for Jira/GitLab integration

## Data

- **Store:** `~/.claude/data.json`
- **Backups:** automatic, keeps last 20
- **Sessions:** `~/.claude/projects/<project>/<session>.jsonl`
- **Jira cache:** `~/.cache/jira-fzf/tickets.json` (from dev-workflow-tools)
