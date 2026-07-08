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
| Home (app.py) | 2026-07-08 | — | P3: tui/views/home.py + tui/orch_app.py. No embedded tig panes yet (P5/P6 — lists own the whole body). `c` spawn + `r` resume work; resume = suspend-attach fallback. `/` filters ws names only (global session content search still deferred; per-ws content search lives in Detail). Pollers ported: tmux, git-status, worktrees, sessions, liveness (+bridge/watcher, rate limiters), idle cleanup. P4-A wired: `C` repo-spawn, `d` delete + `t` trust (ConfirmView), `u` archive/unarchive (no confirm, as in app.py). P4-B wired: `a` add, `n` quick note, `e` todos, `W` add link, `o` open links (1 link → direct open), `b` brain-dump chain, `?` help, `:` command palette, multi-match `r` opens SessionPickerView. P4-C wired tabs: enter/l opens DetailView, ctrl+b/ctrl+x cycle Home↔Sessions↔Detail via app-held cached views (TabManager stays the source of truth; replace_top between surfaces, dismiss pops to home leaving the tab open, as app.py), `x` closes the tab (evicts the cached view; falls back per TabManager to the previous tab). The palette moved to OrchApp and is detail-aware (open/spawn/resume delegate to the active DetailView, `rename` shows the "use E" hint, `close` really closes the tab). ctrl+b/ctrl+x/x also fire app-level via on_unhandled_key (the Textual app intercepted them in App.on_key); other app-global bindings (e.g. `q` falling through a screen) stay surface-local. Remaining stubs: `E` rename inline input, dev-workflow actions (P/T/B/rr + palette equivalents). Deferred: AI naming/titling/description refresh in the session poller, global search worker, tab-switch session auto-resume (_tab_active_session — needs the P5 ClaudeSession screen). |
| ConfirmScreen | 2026-07-08 | — | P4-A: tui/views/confirm.py. Wired at home: `d` delete confirm, `t` trust confirm. |
| _TodoEditScreen | 2026-07-08 | — | P4-A: tui/views/todo_edit.py (FormModalView). Its caller (TodoScreen) is P4-B. |
| BrainPreviewScreen | 2026-07-08 | — | P4-A: tui/views/brain_preview.py. Its caller (BrainDumpScreen flow) is P4-B. |
| LinkSessionScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py. Callers (CurrentSessions/Detail) are P4-C. |
| RepoPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py; wired to `C` repo-spawn. Deviation (all FuzzyModalView pickers): ctrl+h/\x08 deletes instead of dismissing (engine FuzzyList semantics); physical backspace on an empty query still cancels. |
| WorkstreamPickerScreen | 2026-07-08 | — | P4-A: tui/views/pickers.py (`__new__` sentinel preserved); wired into the `C` flow. |
| QuickNoteScreen | 2026-07-08 | — | P4-B: tui/views/quick_note.py; wired home `n` and TodoView `a`. Deviation (all P4 TextEdit/Form modals): ctrl+h cancels instead of deleting a char — the wave-A FormModalView "^H back" semantics. |
| HelpScreen | 2026-07-08 | — | P4-B: tui/views/help.py; wired home `?` (+ `?` in Todo/Trash/Detail/CurrentSessions). Contexts ported: home, todo, trash, detail (P4-C, with the `t` tig + `\` titles additions and without the not-yet-ported ctrl+z zoom), sessions (P4-C); "session" lands with P5 and falls back to home meanwhile. |
| TodoScreen | 2026-07-08 | — | P4-B: tui/views/todo.py (fullscreen ListModal + BlockList rows, header stats, context preview). Keys per original (a/e/E/d/space/enter/c/J/K/?); `l` suppressed (unbound in the original); `q` now really goes back (the original's footer promised it but only the app-level quit was bound). Space-toggle keeps the highlight by todo id, not index. |
| _TodoContextScreen | 2026-07-08 | — | P4-B: TodoContextView in tui/views/todo.py. Save-on-close preserved (^H/Esc save). Deviation: dismisses the edited text and the caller persists it (the original wrote to the store itself). |
| LinksScreen | 2026-07-08 | — | P4-B: tui/views/links.py; wired home `o` per app.py (0 links → toast, 1 → open directly, 2+ → list; enter opens and stays). `[kind]` label brackets escaped (the original emitted them as live markup). |
| AddScreen | 2026-07-08 | — | P4-B: tui/views/add.py; wired home `a`. Category Select → Cycler (left/right/space). Deviation: Enter submits from any field (FormModalView semantics; the original hopped name→desc first). |
| AddLinkScreen | 2026-07-08 | — | P4-B: tui/views/add_link.py; wired home `W`. Kind Cycler with the live kind-description line; focus starts on value as in the original. |
| BrainDumpScreen | 2026-07-08 | — | P4-B: tui/views/brain_dump.py + the full `b` chain in home (_do_brain port). P4-C resolved the interim deviation: the preview's "launch" branch now opens the first created workstream's DetailView tab, exactly as app.py. |
| SessionPickerScreen | 2026-07-08 | — | P4-B: tui/views/session_picker.py; wired into do_resume's pick_session — multi-match `r` now opens the picker instead of resuming the most recent. 10s liveness refresh in a thread with exclusive-group generation staleness (stale results dropped); missing titles generated once in a thread. |
| AutoModeStartScreen | 2026-07-08 | — | P4-B: tui/views/auto_mode_start.py (space/a/n/enter per original). Not reachable in the tui engine yet: its only caller is app.toggle_auto_mode via ctrl+y on a Claude session screen — lands with P5. |
| TrashScreen | 2026-07-08 | — | P4-B: tui/views/trash.py (fullscreen, ws-grouped session blocks); reachable via `:` palette "trash" (no home key exists, same as app.py). `?`/`:` in-screen keys wired. Deviation: `D` purge asks a ConfirmView first — the original purged permanently with no confirm. |
| CurrentSessionsScreen | 2026-07-08 | — | P4-C: tui/views/current_sessions.py. Permanent "Sessions" tab (index 1), reached by tab cycling from home, as app.py. Grouped ws-header rows, 5s reload moved to a thread worker on the "current_sessions" exclusive group with generation-stale drops (the original reloaded on the UI thread), 0.3s throbber re-renders THINKING blocks in place via id-keyed update_row (a shape mismatch rebuilds — the ids-match check of screens.py:3922). enter/l/r resume = suspend-attach until P5; space archive; ctrl+space archive+back; `:`/`?` wired. Toasts render only on home's footer for now (fullscreen surfaces don't paint them; Detail shows them in its help bar). |
| DetailScreen (lite) | 2026-07-08 | — | P4-C: tui/views/detail.py (Detail-lite — Kyle's most-used screen). Full port of layout (tab bar / title+meta / desc / sessions 2fr + archived 1fr with load-more / lower panel / help bar), notified→elevated→quiet(today/thinking/earlier/shelved) grouping with separators (group_detail_sessions et al. extracted to state.py with tests; ws meta/body/peek markup to rendering.py), ctrl+j/k pane ring (sessions/archived/body) with focused-pane border, the whole binding walk (enter/l/^L/r, c, n, e, W, o, f, u, p, y, z, d/D, A, X, T, space, x, `/`, `\`, `:`, `?`, h/^H/esc), back cascade peek→search→dismiss, 30s liveness thread worker (per-ws exclusive group + generation-stale drop + options-fingerprint rebuild gate), 0.3s animating-rows-only throbber, 0.12s discovery spinner, peek (extract_session_content into the same list) and content search (bg cache warm; title fallback while cold; `\` title-only fuzzy with highlight positions). Deviations: **no embedded tig panes (P6)** — the body panel always renders, plus a "t: tig (fullscreen) — embedded panes land later" hint, and a new `t` key runs tig over suspend() (shadowing the app-level `t` trust-toggle while in Detail); **session select/resume = suspend-attach** via launch_claude_session (embedded ClaudeSession screen is P5); peek reads the focused pane's highlighted session (the original always peeked the sessions list); rows crop at very narrow pane widths where the Textual OptionList soft-wrapped (session rows have a ~40-col floor); toasts surface in the help-bar line; auto_role badges inert until auto-mode state lands (P5); ctrl+z panel zoom not ported (Textual-specific). |
| ClaudeSessionScreen | — | — | P5 |
| DetailScreen (full) | — | — | P6 |

## Drift log

Changes made to old-UI files after 2026-07-08 that the port must pick up:

| Date | File | Change | Picked up in port? |
|---|---|---|---|
| (none yet) | | | |
