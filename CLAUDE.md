# Claude Code Instructions for this Repository

## Concurrent Agents

Multiple Claude agents frequently work in this repo at the same time. Follow these rules strictly:

### Always Commit Your Work

- **Commit early and often.** Do not leave large amounts of work uncommitted. Make a commit as soon as you have a coherent, working change — even if the task isn't fully done yet.
- Before starting work, run `git status` to understand the current state. If there are uncommitted changes from other agents, **do not touch or discard them**.
- When you finish a task or are about to stop, commit your changes immediately.

### Never Run Destructive Commands

- **NEVER** run `git checkout -- <file>`, `git restore <file>`, `git checkout .`, or `git restore .` to discard changes. Other agents may have uncommitted work in those files.
- **NEVER** run `git reset --hard`, `git clean -f`, or any command that destroys uncommitted work.
- **NEVER** run `git stash` unless you are certain no other agent has uncommitted changes.
- If you need to undo YOUR changes to a specific file, use `git diff -- <file>` to review first, and only revert lines you changed. Prefer `git checkout HEAD -- <file>` only if you are certain the file has no other agents' work in it.
- If you encounter merge conflicts or unexpected state, **ask the user** rather than forcing a resolution.

### Branch Etiquette

- Work on `master` unless told otherwise.
- Do not force-push.
- Do not rewrite history (rebase, amend) on shared branches.

## Testing

- Run `python -m pytest tests/ -x -q` to run the test suite.
- `tests/test_app.py` may have flaky tests due to async Textual testing — if a test fails in batch but passes alone, note it but don't block on it.

## Project Structure

- `app.py` — Main TUI application (Textual). This is the largest and most actively edited file.
- `sessions.py` — Claude session discovery and JSONL parsing.
- `threads.py` — Thread clustering and activity detection.
- `models.py` — Workstream data model and persistence.
- `workstream_synthesizer.py` — AI-driven thread-to-workstream grouping.
- `thread_namer.py` — AI-driven thread naming.
- `orch`, `orch-claude`, `orch-header` — Shell scripts for tmux integration.
- `cli.py` — CLI interface (`orch` command).
