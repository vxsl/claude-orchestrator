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
| Home (app.py) | 2026-07-08 | — | P3: tui/views/home.py + tui/orch_app.py. No embedded tig panes yet (P5/P6 — lists own the whole body). `c` spawn + `r` resume work; resume = suspend-attach fallback. `/` filters ws names only (session content search P4). Pollers ported: tmux, git-status, worktrees, sessions, liveness (+bridge/watcher, rate limiters), idle cleanup. P4-A wired: `C` repo-spawn, `d` delete + `t` trust (ConfirmView), `u` archive/unarchive (no confirm, as in app.py). P4-B wired: `a` add, `n` quick note, `e` todos, `W` add link, `o` open links (1 link → direct open), `b` brain-dump chain, `?` help, `:` command palette (home._execute_command port; trash reachable through it), multi-match `r` opens SessionPickerView. Remaining stubs: detail/tabs (P4-C), `E` rename inline input, dev-workflow actions (P/T/B/rr + palette equivalents), palette `close`. Deferred: AI naming/titling/description refresh in the session poller, global search worker. |
| ConfirmScreen | 2026-07-08 | — | P4-A: tui/views/confirm.py. Wired at home: `d` delete confirm, `t` trust confirm. |
| _TodoEditScreen | 2026-07-08 | — | P4-A: tui/views/todo_edit.py (FormModalView). Its caller (TodoScreen) is P4-B. |
| BrainPreviewScreen | 2026-07-08 | — | P4-A: tui/views/brain_preview.py. Its caller (BrainDumpScreen flow) is P4-B. |
| LinkSessionScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py. Callers (CurrentSessions/Detail) are P4-C. |
| RepoPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py; wired to `C` repo-spawn. Deviation (all FuzzyModalView pickers): ctrl+h/\x08 deletes instead of dismissing (engine FuzzyList semantics); physical backspace on an empty query still cancels. |
| WorkstreamPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py (`__new__` sentinel preserved); wired into the `C` flow. |
| QuickNoteScreen | 2026-07-08 | — | P4-B: tui/views/quick_note.py; wired home `n` and TodoView `a`. Deviation (all P4 TextEdit/Form modals): ctrl+h cancels instead of deleting a char — the wave-A FormModalView "^H back" semantics. |
| HelpScreen | 2026-07-08 | — | P4-B: tui/views/help.py; wired home `?` (+ `?` in Todo/Trash). Contexts ported: home, todo, trash; detail/session/sessions land with their screens and fall back to home meanwhile. |
| TodoScreen | 2026-07-08 | — | P4-B: tui/views/todo.py (fullscreen ListModal + BlockList rows, header stats, context preview). Keys per original (a/e/E/d/space/enter/c/J/K/?); `l` suppressed (unbound in the original); `q` now really goes back (the original's footer promised it but only the app-level quit was bound). Space-toggle keeps the highlight by todo id, not index. |
| _TodoContextScreen | 2026-07-08 | — | P4-B: TodoContextView in tui/views/todo.py. Save-on-close preserved (^H/Esc save). Deviation: dismisses the edited text and the caller persists it (the original wrote to the store itself). |
| LinksScreen | 2026-07-08 | — | P4-B: tui/views/links.py; wired home `o` per app.py (0 links → toast, 1 → open directly, 2+ → list; enter opens and stays). `[kind]` label brackets escaped (the original emitted them as live markup). |
| AddScreen | 2026-07-08 | — | P4-B: tui/views/add.py; wired home `a`. Category Select → Cycler (left/right/space). Deviation: Enter submits from any field (FormModalView semantics; the original hopped name→desc first). |
| AddLinkScreen | 2026-07-08 | — | P4-B: tui/views/add_link.py; wired home `W`. Kind Cycler with the live kind-description line; focus starts on value as in the original. |
| BrainDumpScreen | 2026-07-08 | — | P4-B: tui/views/brain_dump.py + the full `b` chain in home (_do_brain port). Deviation: the preview's "launch" branch calls launch_claude_session directly (suspend-attach) — app.py opened the Detail view, which is P4-C. |
| SessionPickerScreen | 2026-07-08 | — | P4-B: tui/views/session_picker.py; wired into do_resume's pick_session — multi-match `r` now opens the picker instead of resuming the most recent. 10s liveness refresh in a thread with exclusive-group generation staleness (stale results dropped); missing titles generated once in a thread. |
| AutoModeStartScreen | 2026-07-08 | — | P4-B: tui/views/auto_mode_start.py (space/a/n/enter per original). Not reachable in the tui engine yet: its only caller is app.toggle_auto_mode via ctrl+y on a Claude session screen — lands with P5. |
| TrashScreen | 2026-07-08 | — | P4-B: tui/views/trash.py (fullscreen, ws-grouped session blocks); reachable via `:` palette "trash" (no home key exists, same as app.py). `?`/`:` in-screen keys wired. Deviation: `D` purge asks a ConfirmView first — the original purged permanently with no confirm. |
| CurrentSessionsScreen | — | — | P4-C |
| DetailScreen (lite) | — | — | P4-C: no embedded tig (suspend fallback) |
| ClaudeSessionScreen | — | — | P5 |
| DetailScreen (full) | — | — | P6 |

## Drift log

Changes made to old-UI files after 2026-07-08 that the port must pick up:

| Date | File | Change | Picked up in port? |
|---|---|---|---|
| (none yet) | | | |
