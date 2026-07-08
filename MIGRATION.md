# Textual → tui engine migration

**Status: in progress** (started 2026-07-08). The TUI is migrating from Textual
to a hand-rolled immediate-mode engine (`tui/` package: raw stdin input,
alt-screen, line-diffed Rich painting). Decision and profiling evidence:
Textual's refresh pipeline + CSS machinery burned ~80% of the app's CPU;
the plan lives with the session notes.

## Rules while both engines exist

- **New behavior goes in shared modules** (`state.py`, `actions.py`,
  `rendering.py`, `sessions.py`, `models.py`, `term_host.py`) — never in
  `app.py` / `screens.py` / `claude_session_screen.py` / `widgets.py`
  unless unavoidable. Those files are frozen and get deleted at cutover.
- If you must change old-UI behavior, **add a row to the drift log below**
  so the port doesn't silently lose it.
- Shared modules must import without Textual — `tests/test_purity.py`
  enforces this; don't add engine conditionals to shared code.
- Keys come from `config.py DEFAULT_KEYS`; visuals from `rendering.py`.
  Both engines consume the same sources.
- Entry gate (live since P3): `ORCH_ENGINE=tui` env var selects the new
  engine in `cli.cmd_tui`; default stays `textual` until cutover.
- Data isolation for new-engine testing: `OrchApp(store_path=...)` or the
  `ORCH_STORE_PATH` env var points the store at a copy instead of the real
  `data.json` (models.py has no env override; this lives in OrchApp only).

## Screen port ledger

| Screen | Ported | Parity-verified | Notes |
|---|---|---|---|
| Home (app.py) | 2026-07-08 | — | P3: tui/views/home.py + tui/orch_app.py. No embedded tig panes yet (P5/P6 — lists own the whole body). Detail/tabs/add/notes/rename/palette/help = stub toasts. `c` spawn + `r` resume work; resume = suspend-attach fallback (multi-match resumes most recent; SessionPicker is P4-B). `/` filters ws names only (session content search P4). Pollers ported: tmux, git-status, worktrees, sessions, liveness (+bridge/watcher, rate limiters), idle cleanup. P4-A wired: `C` repo-spawn, `d` delete + `t` trust (ConfirmView), `u` archive/unarchive (no confirm, as in app.py). Deferred: AI naming/titling/description refresh in the session poller, global search worker. |
| ConfirmScreen | 2026-07-08 | — | P4-A: tui/views/confirm.py. Wired at home: `d` delete confirm, `t` trust confirm. |
| _TodoEditScreen | 2026-07-08 | — | P4-A: tui/views/todo_edit.py (FormModalView). Its caller (TodoScreen) is P4-B. |
| BrainPreviewScreen | 2026-07-08 | — | P4-A: tui/views/brain_preview.py. Its caller (BrainDumpScreen flow) is P4-B. |
| LinkSessionScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py. Callers (CurrentSessions/Detail) are P4-C. |
| RepoPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py; wired to `C` repo-spawn. Deviation (all FuzzyModalView pickers): ctrl+h/\x08 deletes instead of dismissing (engine FuzzyList semantics); physical backspace on an empty query still cancels. |
| WorkstreamPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py (`__new__` sentinel preserved); wired into the `C` flow. |
| QuickNoteScreen | — | — | P4-B |
| HelpScreen | — | — | P4-B |
| TodoScreen | — | — | P4-B |
| _TodoContextScreen | — | — | P4-B |
| LinksScreen | — | — | P4-B |
| AddScreen | — | — | P4-B |
| AddLinkScreen | — | — | P4-B |
| BrainDumpScreen | — | — | P4-B |
| SessionPickerScreen | — | — | P4-B |
| AutoModeStartScreen | — | — | P4-B |
| TrashScreen | — | — | P4-B |
| CurrentSessionsScreen | — | — | P4-C |
| DetailScreen (lite) | — | — | P4-C: no embedded tig (suspend fallback) |
| ClaudeSessionScreen | — | — | P5 |
| DetailScreen (full) | — | — | P6 |

## Drift log

Changes made to old-UI files after 2026-07-08 that the port must pick up:

| Date | File | Change | Picked up in port? |
|---|---|---|---|
| (none yet) | | | |
