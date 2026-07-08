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
- Entry gate: `ORCH_ENGINE=tui` env var selects the new engine once P3
  lands; default stays `textual` until cutover.

## Screen port ledger

| Screen | Ported | Parity-verified | Notes |
|---|---|---|---|
| Home (app.py) | — | — | P3 |
| ConfirmScreen | — | — | P4-A |
| _TodoEditScreen | — | — | P4-A |
| BrainPreviewScreen | — | — | P4-A |
| LinkSessionScreen | — | — | P4-A |
| RepoPickerScreen | — | — | P4-A |
| WorkstreamPickerScreen | — | — | P4-A |
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
