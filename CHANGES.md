# What Changed — Full Redesign (2026-03-22)

Two sessions completed a 6-step redesign that removed dead concepts and deeply integrated dev-workflow-tools data into the orchestrator.

## What's new for users

### Worktree auto-discovery (Step 4 — the centerpiece)

If you use git worktrees, orch now **automatically creates workstreams** for each one. Every 30 seconds it scans your known repos, and for any non-main worktree branch:

- Creates a workstream named from the Jira ticket summary (e.g. `UB-1234: Fix login timeout`) if the branch follows the `UB-1234-slug` convention and the ticket is in your Jira cache. Falls back to branch name.
- Enriches the workstream row with **Jira status** (color-coded), **MR badge** (when a merge request exists), and **ticket-solve status** (when active).
- Auto-archives when the worktree directory disappears (branch deleted/merged).

**Why it matters:** You no longer need to manually create workstreams for branches. Start a worktree with `rr.sh`, and orch picks it up within 30 seconds with full Jira context already filled in.

### Fuzzy command palette (Step 5)

Pressing `:` now opens a **fuzzy-searchable picker** over all 25+ commands instead of a raw text input. Type a few characters to find what you need — no need to remember exact command names or aliases.

Commands that require a selected workstream are dimmed when none is selected, so you can see what's available without trial-and-error.

**Why it matters:** The old `:` input required knowing exact command names. Now you can type `sh` to find `ship`, `br` to find `branches`, etc. New users can browse all available commands.

### Category auto-detection (Step 3)

Repos with a `gitlab` remote auto-categorize as **Work**. Repos with a `github` remote stay **Personal**. Only applies when the workstream hasn't been manually categorized.

**Why it matters:** No more manually setting category for every workstream. Your work repos are work, your personal repos are personal.

### File picker from DetailScreen (Step 6)

Press `f` in a DetailScreen to open fzedit (file picker) in the workstream's directory.

**Why it matters:** You can jump into code from the detail view without going back to the home screen.

### Origin enum removed (Step 1)

The `origin` field (manual vs discovered) has been removed from workstreams. All workstreams are now equal — there's no "discovered" tag or different treatment based on how they were created.

**Why it matters:** Simpler data model. AI-discovered workstreams behave identically to manually created ones.

### ViewMode enum removed (Step 2)

The three-view system (Workstreams / Sessions / Archived) is gone. There is now a single workstream table. Sessions appear in the preview pane and in DetailScreen. Archived items are accessible via filter `6`.

**Why it matters:** The sessions and archived views added complexity but weren't used — sessions are always visible in the preview pane, and the archived filter gives the same access with less UI chrome.

## Enrichment data shown on workstream rows

Each row in the home view can now show a chain of metadata:

```
 ● UB-1234: Fix login timeout  ⚡branches  UB-1234-fix*+2
     work · UB-1234 In Progress · MR · solving · ~/dev/repo-wt · 3 sess · 1.2M · 5m ago
     Fix the login timeout bug when session expires during...
```

Line 2 metadata (left to right): category, ticket key + Jira status, MR badge, ticket-solve status, worktree path, session count, token usage, last updated.

## Technical details

### What was removed
- `Origin` enum and `origin` field (models.py, state.py, rendering.py, synthesizer)
- `ViewMode` enum and 3-view system (rendering.py, state.py, app.py, config.py)
- `action_cycle_status` / `s`/`S` keybindings (status is auto-derived from session state)
- `CommandInput` inline widget (replaced by FuzzyPickerScreen)
- Sessions table and archived table `OptionList` widgets

### What was added
- **models.py:** 5 transient enrichment fields (`ticket_key`, `ticket_summary`, `ticket_status`, `mr_url`, `ticket_solve_status`) — not persisted to disk
- **actions.py:** `get_mr_cache()`, `get_ticket_solve_status()`, `discover_worktrees()`, `extract_ticket_key()`, `get_git_remote_host()`, `open_file_picker()`
- **state.py:** `CommandDef` + `COMMAND_REGISTRY` (25 commands), `get_command_items()`, `discover_and_enrich_worktrees()`, `_infer_category_from_remote()`
- **rendering.py:** Jira/MR/ticket-solve badges on workstream row line 2
- **app.py:** 30s worktree discovery poll, FuzzyPickerScreen command palette
- **screens.py:** File picker binding in DetailScreen

### Implementation order (across two sessions)

| Step | Commit | What |
|------|--------|------|
| 1 | `60b1911` | Remove Origin enum |
| 2 | `1d7bf26` | Remove ViewMode enum |
| 3 | `236915e` | Category auto-detection from git remote |
| 6 | `f38d5ce` | File picker from DetailScreen |
| 5 | `01bb0c9` | Command palette as FuzzyPicker |
| 4 | `cec2fd3` | Worktree auto-discovery + Jira/MR/ticket-solve enrichment |

Steps 1-3 and 6 were done via parallel worktree agents in session 1. Steps 5 and 4 were done sequentially in session 2 (parallel agents caused 11 merge conflicts in app.py in session 1).

### Test coverage

631 tests pass. 3 pre-existing failures unrelated to the redesign:
- `test_thinking_animated` — throbber frame assertion
- `test_last_user_activity_falls_back` — timestamp fallback
- `test_note_adds_to_workstream` — checks `.notes` instead of `.todos`
