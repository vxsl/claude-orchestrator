"""Auto-mode loop runner.

Drives the coordinator/implementer cycle for a workstream:
- coordinator decides what to dispatch next (every iteration, including the first)
- loop spawns an implementer for the chosen todo
- implementer runs `orch report` to write a summary back
- loop injects that summary into the coordinator's PTY and waits
- coordinator picks again, or runs `orch distill done` to terminate

The loop never auto-advances through a pre-existing pending queue — every
iteration requires the coordinator to take one of three actions:
  (a) `orch distill crystallize` (or /user:extract-orch-todo) to queue a NEW todo
  (b) `orch distill next --todo-id <id>` to dispatch an EXISTING pending todo
  (c) `orch distill done --reason '...'` to terminate

If the coordinator goes silent after a followup (it generates text but
takes none of those three actions), the loop re-injects a short nudge
every NUDGE_INTERVAL_S seconds until something changes.

Pure logic — no Textual imports. The TUI wires three callables:
  spawn_implementer(todo, brief) -> awaitable[None]
      Resolves whichever is sooner: todo.report becomes non-empty, OR
      the implementer's screen dismisses. Either signal advances the loop.
      Implementer may continue running in the background after report —
      that's fine; the next iteration will push another screen on top.
  inject_coordinator(text) -> None              (typed into coordinator's PTY)
  notify(line) -> None                          (status surfacing)
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from models import Store, TodoItem, Workstream

NUDGE_INTERVAL_S = 180.0  # 3 minutes of coordinator silence → re-prompt


# Patterns indicating Claude has stalled on a usage-quota prompt and is
# blocked waiting for the user to choose how to proceed. Lowercased
# before matching. Conservative — only the most distinctive phrases so
# benign session content (e.g. someone discussing rate limits) doesn't
# trigger false positives.
QUOTA_STALL_PATTERNS: tuple[str, ...] = (
    "5-hour limit reached",
    "usage limit reached",
    "your usage limit",
    "wait until your limit resets",
    "limit will reset at",
    "rate limit reached",
)


def detect_quota_stall(pane_text: str) -> bool:
    """Return True if pane content suggests Claude is blocked on a
    quota / usage-limit interactive prompt.

    Caller is expected to have observed the same stall across two
    consecutive polls before acting, so brief mentions of these
    phrases in normal conversation don't trigger an unwanted Enter.
    """
    if not pane_text:
        return False
    lower = pane_text.lower()
    return any(p in lower for p in QUOTA_STALL_PATTERNS)


def find_next_todo(
    ws: Workstream,
    skip_ids: Optional[set] = None,
) -> Optional[TodoItem]:
    """Next un-done un-archived non-skipped todo, or None.

    Origin (manual vs crystallized) is informational only — the picker
    decides at start what's in scope. Inside the loop, any newly-added
    todo (manual or crystallized) is eligible to be picked up unless
    explicitly in skip_ids.

    skip_ids: todo IDs the loop should ignore (used both by 'start fresh'
    mode and as a defensive guard against re-attempting a todo whose
    implementer never reported).
    """
    skip = skip_ids or set()
    for todo in ws.todos:
        if todo.archived or todo.done:
            continue
        if todo.id in skip:
            continue
        return todo
    return None


def build_implementer_brief(todo: TodoItem) -> str:
    """Prompt for an implementer session: the todo's text + context, plus
    instructions to call `orch report` when done."""
    parts = [todo.text]
    if todo.context:
        parts.append("")
        parts.append(todo.context)
    parts.append("")
    parts.append("---")
    parts.append(
        f"[auto-mode] When finished, run this command to report back so the "
        f"coordinator can plan the next step:\n\n"
        f"  orch report --todo-id {todo.id} --text \"<one-paragraph summary "
        f"of what you did, anything notable, surprising, or unfinished>\"\n\n"
        f"Then exit with /exit."
    )
    return "\n".join(parts)


def _format_pending_list(todos: list) -> str:
    """One line per pending todo, with ID and truncated text — for prompts."""
    lines = []
    for t in todos:
        text = t.text.strip().replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        lines.append(f"  - {t.id}  {text}")
    return "\n".join(lines)


def build_coordinator_kickoff(ws: Workstream, pending_todos: Optional[list] = None) -> str:
    pending = pending_todos or []
    if pending:
        plural = "todo" if len(pending) == 1 else "todos"
        listing = _format_pending_list(pending)
        return (
            f"[auto-mode started] You are now the coordinator for workstream "
            f"'{ws.name}'. {len(pending)} pending {plural} already queued — but "
            f"the loop will NOT auto-dispatch them. You decide what runs next.\n\n"
            f"Pending {plural}:\n{listing}\n\n"
            f"⚠ AUTO-MODE PROTOCOL — take exactly ONE action now:\n"
            f"  (a) `orch distill next --todo-id <id>` to dispatch one of the pending todos above, OR\n"
            f"  (b) /user:extract-orch-todo (or `orch distill crystallize`) to queue a fresh task, OR\n"
            f"  (c) `orch distill done --reason '...'` to HARD-KILL auto-mode (rare).\n\n"
            f"NOTE: `distill done` is NOT an end-of-iteration signal. It exits the auto-mode "
            f"runner entirely — there is no \"next loop.\" Only use it when the workstream is "
            f"actually finished. While pending todos exist, dispatch one with (a) instead. "
            f"The CLI will refuse `distill done` while pending todos remain unless you pass --force."
        )
    return (
        f"[auto-mode started] You are now the coordinator for workstream "
        f"'{ws.name}'. Crystallize the first concrete task with "
        f"/user:extract-orch-todo (or `orch distill crystallize`). An "
        f"implementer will pick it up automatically. After each implementer "
        f"reports back, you'll be prompted again. Run "
        f"`orch distill done --reason '...'` only when the workstream is complete "
        f"and no pending todos remain — it HARD-KILLS the auto-mode runner."
    )


def build_coordinator_followup(
    todo: TodoItem,
    report: str,
    pending_todos: Optional[list] = None,
) -> str:
    pending = pending_todos or []
    parts = [
        f"[auto-mode] Implementer for todo '{todo.text}' has finished.",
        "",
        "Report:",
        report,
        "",
        "⚠ AUTO-MODE PROTOCOL — read the report above, then take exactly ONE action:",
    ]
    if pending:
        plural = "todo" if len(pending) == 1 else "todos"
        listing = _format_pending_list(pending)
        parts += [
            f"  (a) `orch distill next --todo-id <id>` to dispatch one of these pending {plural}:",
            listing,
            f"  (b) /user:extract-orch-todo (or `orch distill crystallize`) to queue a NEW implementer task, OR",
            f"  (c) `orch distill done --reason '...'` to HARD-KILL auto-mode (rare; refused while pending todos exist).",
        ]
    else:
        parts += [
            f"  (a) /user:extract-orch-todo (or `orch distill crystallize`) to queue the next implementer task, OR",
            f"  (b) `orch distill done --reason '...'` to HARD-KILL auto-mode (only if the workstream is complete).",
        ]
    parts += [
        "",
        "Do NOT respond conversationally, recap, or 'stand by' — the loop is "
        "blocked until you take one of those actions. Decide based on the "
        "report above whether to continue with an existing item, crystallize "
        "a new step, or terminate.",
        "",
        "REMINDER: `distill done` is NOT an end-of-iteration marker. It exits "
        "the auto-mode runner entirely — there is no \"next loop\" that re-fires. "
        "If pending todos remain, dispatch one with (a). Use `done` only when "
        "the workstream is actually finished.",
    ]
    return "\n".join(parts)


def build_coordinator_nudge(pending_todos: Optional[list] = None) -> str:
    pending = pending_todos or []
    lines = ["[auto-mode] Still waiting. Take ONE action now:"]
    if pending:
        lines.append("  (a) `orch distill next --todo-id <id>` to dispatch a pending todo, OR")
        lines.append("  (b) /user:extract-orch-todo to queue a new task, OR")
        lines.append("  (c) `orch distill done --reason '...'` to HARD-KILL auto-mode (refused while pending todos exist).")
    else:
        lines.append("  (a) /user:extract-orch-todo to queue next, OR")
        lines.append("  (b) `orch distill done --reason '...'` to HARD-KILL auto-mode (only if workstream is complete).")
    lines.append("No conversational reply — pick one and run it.")
    lines.append("`distill done` exits the auto-mode runner — there is no \"next loop.\"")
    return "\n".join(lines)


class AutoMode:
    """Sequential coordinator/implementer loop. Cooperates with cancel()."""

    def __init__(
        self,
        store: Store,
        ws_id: str,
        spawn_implementer: Callable[[TodoItem, str], Awaitable[None]],
        inject_coordinator: Callable[[str], None],
        notify: Optional[Callable[[str], None]] = None,
        poll_interval: float = 2.0,
        skip_todo_ids: Optional[set] = None,
    ):
        self.store = store
        self.ws_id = ws_id
        self.spawn_implementer = spawn_implementer
        self.inject_coordinator = inject_coordinator
        self.notify = notify or (lambda _: None)
        self.poll_interval = poll_interval
        self.skip_todo_ids: set = set(skip_todo_ids) if skip_todo_ids else set()

        self.canceled = False
        self.iteration = 0
        self.current_todo_id: Optional[str] = None
        self.last_report: str = ""
        self.final_status: str = ""

    def cancel(self) -> None:
        self.canceled = True

    async def _read_report(self, todo_id: str) -> str:
        """Read the implementer's writeback for `todo_id`, retrying briefly
        when the load returns no workstreams or no matching todo.

        Defends against the same race wait_for_report handles: a concurrent
        writer (coordinator's `orch distill crystallize`, another impl's
        `orch report`, the TUI's description refresher) can leave data.json
        partially written for a few ms. Store.load() catches the
        JSONDecodeError and silently sets workstreams=[] — a one-shot read
        at that instant looks identical to "todo missing, no report" and
        falsely emits the no-writeback fallback.

        A clear hit (workstream + todo both present, report empty) means
        the implementer truly didn't report — return '' immediately.
        """
        for _ in range(5):
            try:
                self.store.load(force=True)
            except Exception:
                await asyncio.sleep(0.3)
                continue
            ws = self.store.get(self.ws_id)
            if ws is None:
                await asyncio.sleep(0.3)
                continue
            cur = next((t for t in ws.todos if t.id == todo_id), None)
            if cur is None:
                await asyncio.sleep(0.3)
                continue
            return cur.report or ""
        return ""

    def _pending_todos(self, ws: Workstream) -> list[TodoItem]:
        """Pending un-archived un-done todos that haven't been attempted this run."""
        return [
            t for t in ws.todos
            if not t.done and not t.archived and t.id not in self.skip_todo_ids
        ]

    async def _wait_for_todo_or_done(
        self,
        existing_ids: set[str],
    ) -> tuple[Optional[TodoItem], str]:
        """Poll until the coordinator picks something or terminates.

        `existing_ids` is the snapshot of pending todo IDs at the moment
        the wait started. Those are gated — the loop will NOT dispatch
        them automatically. The coordinator must explicitly pick one
        (via `orch distill next --todo-id <id>`, which sets
        ws.auto_next_todo_id) or crystallize a fresh todo (a new id
        appears that wasn't in the snapshot).

        Returns (todo, terminate_reason). Terminating reasons:
          - ws.auto_done_reason set → that string
          - canceled → "canceled"
          - workstream missing → "workstream not found"

        If the coordinator goes silent past NUDGE_INTERVAL_S, re-inject
        a short reminder.
        """
        import time as _time
        last_nudge_at = _time.time()
        while not self.canceled:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if not ws:
                return None, "workstream not found"
            if ws.auto_done_reason:
                return None, ws.auto_done_reason

            # (1) Coordinator explicitly picked an existing pending todo.
            if ws.auto_next_todo_id:
                picked = next(
                    (t for t in ws.todos
                     if t.id == ws.auto_next_todo_id
                     and not t.done and not t.archived
                     and t.id not in self.skip_todo_ids),
                    None,
                )
                # Always clear the signal — invalid picks are dropped, not retried,
                # so the coordinator gets a chance to pick again on the next nudge.
                ws.auto_next_todo_id = ""
                self.store.update(ws)
                if picked is not None:
                    return picked, ""

            # (2) A fresh todo was crystallized (id not in pre-wait snapshot).
            for t in self._pending_todos(ws):
                if t.id not in existing_ids:
                    return t, ""

            # (3) Silent — nudge.
            if _time.time() - last_nudge_at > NUDGE_INTERVAL_S:
                self.notify("coordinator silent — sending nudge")
                pending = [t for t in self._pending_todos(ws) if t.id in existing_ids]
                self.inject_coordinator(build_coordinator_nudge(pending))
                last_nudge_at = _time.time()
            await asyncio.sleep(self.poll_interval)
        return None, "canceled"

    async def run(self) -> str:
        """Run the loop to completion. Returns the terminating reason.

        Every iteration — including the first — waits for the coordinator
        to explicitly pick a todo (via `orch distill next` or a fresh
        crystallization) or terminate. Pre-existing pending todos do not
        auto-flow.
        """
        self.store.load(force=True)
        ws = self.store.get(self.ws_id)
        if not ws:
            self.final_status = "workstream not found"
            return self.final_status

        # Clear stale signals from a previous run.
        dirty = False
        if ws.auto_done_reason:
            ws.auto_done_reason = ""
            dirty = True
        if ws.auto_next_todo_id:
            ws.auto_next_todo_id = ""
            dirty = True
        if dirty:
            self.store.update(ws)

        pending = self._pending_todos(ws)
        existing_ids = {t.id for t in pending}
        self.inject_coordinator(build_coordinator_kickoff(ws, pending_todos=pending))
        self.notify(f"started ({len(pending)} todos queued, awaiting coordinator pick)")

        todo, reason = await self._wait_for_todo_or_done(existing_ids)
        if reason or todo is None:
            self.final_status = reason or "canceled"
            return self.final_status

        while not self.canceled:
            self.iteration += 1
            self.current_todo_id = todo.id
            brief = build_implementer_brief(todo)
            self.notify(f"iter {self.iteration}: spawning implementer for '{todo.text[:60]}'")

            await self.spawn_implementer(todo, brief)
            if self.canceled:
                self.final_status = "canceled"
                return self.final_status

            # Never re-attempt the same todo within one run, even if its
            # implementer didn't report. Without this, an impl that
            # fails to report could be respawned in a tight loop.
            self.skip_todo_ids.add(todo.id)

            report_text = await self._read_report(todo.id)
            report = report_text or "(implementer did not run `orch report` — no writeback)"
            self.last_report = report

            # Snapshot pending todos AS OF NOW — anything created after
            # this point counts as a fresh coordinator decision and will
            # advance the loop. Pre-existing pending items are gated and
            # require explicit `orch distill next --todo-id <id>`.
            self.store.load(force=True)
            ws_snap = self.store.get(self.ws_id)
            if not ws_snap:
                self.final_status = "workstream not found"
                return self.final_status
            pending_snap = self._pending_todos(ws_snap)
            existing_ids = {t.id for t in pending_snap}

            self.inject_coordinator(
                build_coordinator_followup(todo, report, pending_todos=pending_snap)
            )
            self.notify(f"iter {self.iteration}: report received, awaiting coordinator pick")

            next_todo, reason = await self._wait_for_todo_or_done(existing_ids)
            if reason:
                self.final_status = reason
                return self.final_status
            if next_todo is None:
                self.final_status = "canceled"
                return self.final_status
            todo = next_todo

        self.final_status = "canceled"
        return self.final_status
