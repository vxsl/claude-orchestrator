# What Changed — Redesign Session (2026-03-22)

This document covers what I did in the overnight redesign session, what's new, what works differently, and what ambiguities I ran into.

## TL;DR

8 commits, ~1800 lines added/changed. The app now has:
- A **tab bar** for workstream navigation (was: 3-view Tab cycling)
- **Dev-workflow-tools** integrated into the command palette and keybindings
- **Git branch/status** shown on workstream rows
- **Jira ticket cache** reading (no API calls, reads your existing jira-fzf cache)
- All **picker screens** rebuilt on a shared FuzzyPicker widget (fuzzy search everywhere)
- All **form screens** rebuilt on a shared ModalForm base
- **Searchable help screen** (was: static text)
- **"Thought to thread"** flow: brain dump → launch, ticket → workstream, quick start

## New Interaction Patterns

### Tabs replace view cycling
- **Tab/Shift+Tab** now cycles through open workstream tabs (was: cycles Workstreams/Sessions/Archived views)
- Opening a workstream (Enter) adds it as a tab and pushes its DetailScreen
- **Ctrl+W** closes the current tab
- **Home tab** is always present — it's the workstream list
- Sessions view merged into the home preview pane (no separate tab)
- **Archived view replaced with filter 6** — press `6` to see archived workstreams in the same list

### Dev-workflow shortcuts
- **P** — ship staged changes (runs `oneshot`)
- **T** — browse Jira tickets from cache, link to workstream or create new one
- **B** — browse branches and worktrees for the selected workstream's repo
- All accessible from `:` command palette too (`:ship`, `:ticket`, `:branches`, `:solve`, `:wip`, `:restage`, `:files`)

### Fuzzy search everywhere
Every picker screen now has fuzzy search (was: flat lists or ad-hoc). Type to filter, j/k to navigate, Enter to select. This applies to: repo picker, workstream picker, session linker, help screen, ticket picker, branch picker.

### Brain dump → launch
- In the brain dump preview, press `l` (was: only `y` to add)
- "Add & Launch" mode creates the workstreams AND immediately opens the first one in a tab

## File Changes

| File | What changed |
|------|-------------|
| `widgets.py` | **NEW** — FuzzyPicker, FuzzyPickerScreen, ModalForm, TabBar, InlineInput |
| `state.py` | Added TabManager (pure Python tab tracking), archived filter mode, dev-workflow command palette commands |
| `app.py` | Tab system wiring, git status polling, dev-workflow action handlers, thought-to-thread flows |
| `screens.py` | Rebuilt RepoPickerScreen, WorkstreamPickerScreen, LinkSessionScreen on FuzzyPickerScreen. Rebuilt AddScreen, AddLinkScreen on ModalForm. Rebuilt HelpScreen as searchable FuzzyPicker. Enhanced BrainPreviewScreen with launch mode. |
| `rendering.py` | Added `git_status` parameter to `_render_ws_option` for branch display |
| `actions.py` | Added: `get_worktree_git_status`, `get_jira_cache`, `get_worktree_list`, `get_recent_branches`, `run_git_action`, `run_dev_tool`, `dev_tools_available` |
| `config.py` | Added keybindings: close_tab (Ctrl+W), ship (P), ticket (T), branches (B), filter archived (6) |
| `README.md` | Complete rewrite reflecting new architecture and features |
| `tests/` | ~40 new tests for TabManager, FuzzyPicker, git status, Jira cache, worktree list, branches, git actions, dev-workflow commands |

## Ambiguities & Design Decisions

### Tab state preservation
Tabs currently **don't preserve DetailScreen state** when switching. Each tab switch dismisses the current DetailScreen and pushes a new one. This means scroll position and focused pane reset. I chose this over converting DetailScreen from ModalScreen to Widget because:
1. DetailScreen has ~1400 lines of deeply integrated ModalScreen patterns
2. The reconstruction is fast (reads from store)
3. The user said "not concerned about hackiness, concerned about performance"

**If you want true state preservation**, the next step would be using Textual's ContentSwitcher with DetailScreen as an embedded Widget. That's a bigger refactor of screens.py.

### ViewMode enum still exists
I didn't fully remove `ViewMode` because the sessions table (accessible via `:sessions` command) still uses it. The enum is vestigial — in normal usage, only `WORKSTREAMS` is active. It could be cleaned up in a follow-up.

### Status enum untouched
The plan called for replacing manual Status (QUEUED/IN_PROGRESS/etc) with auto-derived status from git state. I **didn't do this** because:
1. It's deeply embedded (126 references across 12 files)
2. Removing it would break DetailScreen (which we're not touching)
3. The git status info is now *available* (shown on rows), so the auto-derivation could layer on top later

The `s/S` keybinding for manual status cycling still works.

### Origin enum untouched
Plan called for removing it. Decided to leave it — it's only 10 references and removing it would require a data migration for existing data.json files.

### Dev-workflow tools: graceful degradation
All dev-workflow actions check `dev_tools_available()` first. If `~/bin/dev-workflow-tools` doesn't exist, actions show a helpful error. The Jira cache reader also degrades gracefully — returns empty if no cache file exists.

### Ship action runs via `suspend()`
Ship (P) and file picker (f) use `self.suspend()` to hand control to the shell script. This means the TUI disappears momentarily while the tool runs. For interactive tools like `oneshot` and `fzedit`, this is correct. For background jobs like `ticket-solve`, we also use `suspend()` for now — a future improvement would be embedding them in a TerminalWidget.

### Jira ticket picker creates workstreams
When you pick a ticket with `T` and no workstream is selected, it creates a new workstream named after the ticket summary and opens it immediately. This is the "thought to thread" path. If a workstream IS selected, it just links the ticket.

## What I didn't get to

1. **File picker from DetailScreen** (`f` key) — the action_files exists but isn't wired to DetailScreen's bindings (it's on the home view). Adding it to DetailScreen would require modifying that screen.
2. **ticket-bot status indicator** in the status bar
3. **Worktree auto-discovery** (each git worktree = a workstream) — the infrastructure is there (`get_worktree_list`) but the auto-creation flow isn't wired up
4. **Category auto-detection** from git remote (gitlab = work, github = personal)
5. **Command palette as FuzzyPicker** — still uses the text input. Converting it would mean replacing the `:` binding with a FuzzyPickerScreen over all registered commands.

## Test Coverage

- 271 core tests pass (state, models, actions, widgets)
- App tests have 5 pre-existing flaky failures (async race conditions with `#preview-content` query, unrelated to these changes)
- 1 pre-existing rendering test failure (throbber animation)
- All 32 backspace/ctrl+h binding tests pass (was: 2 failing for RepoPickerScreen)

Run: `python -m pytest tests/ -x -q`
