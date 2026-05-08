"""Auto-mode loop runner.

Drives the coordinator/implementer cycle for a workstream:
- coordinator decides what to crystallize next
- loop spawns an implementer for each crystallized todo
- implementer runs `orch report` to write a summary back
- loop injects that summary into the coordinator's PTY and waits
- coordinator runs `orch distill done` to terminate the loop

If the coordinator goes silent after a followup (it generates text but
takes neither of the two protocol actions), the loop re-injects a
short nudge every NUDGE_INTERVAL_S seconds until something changes.

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
    """Next un-done crystallized todo, or None.

    Manual todos are intentionally skipped — auto mode only consumes
    crystallized briefs (implementers need rich context, not bare text).

    skip_ids: todo IDs the loop should ignore (used by 'start fresh' mode
    to leave existing backlog untouched while still processing todos
    crystallized DURING the run).
    """
    skip = skip_ids or set()
    for todo in ws.todos:
        if todo.archived or todo.done:
            continue
        if todo.origin != "crystallized":
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


def build_coordinator_kickoff(ws: Workstream, pending_count: int = 0) -> str:
    if pending_count > 0:
        plural = "todo" if pending_count == 1 else "todos"
        return (
            f"[auto-mode started] You are now the coordinator for workstream "
            f"'{ws.name}'. {pending_count} crystallized {plural} already queued — "
            f"the loop will run them sequentially. After each one finishes, you'll "
            f"receive the implementer's report and a prompt to crystallize more or "
            f"run `orch distill done --reason '...'` to terminate. To stop early, "
            f"run `orch distill done` at any point."
        )
    return (
        f"[auto-mode started] You are now the coordinator for workstream "
        f"'{ws.name}'. Crystallize the first concrete task with "
        f"/user:extract-orch-todo (or `orch distill crystallize`). An "
        f"implementer will pick it up automatically. When the implementer "
        f"reports back, you'll be prompted again. Run "
        f"`orch distill done --reason '...'` when the workstream is complete."
    )


def build_coordinator_followup(todo: TodoItem, report: str) -> str:
    return (
        f"[auto-mode] Implementer for todo '{todo.text}' has finished.\n\n"
        f"Report:\n{report}\n\n"
        f"⚠ AUTO-MODE PROTOCOL — take exactly ONE action right now:\n"
        f"  (a) /user:extract-orch-todo (or `orch distill crystallize`) "
        f"to queue the next implementer task, OR\n"
        f"  (b) `orch distill done --reason '...'` to terminate the loop.\n\n"
        f"Do NOT respond conversationally, recap, or 'stand by' — the loop "
        f"is blocked until you take one of those two actions. If unsure, "
        f"crystallize the next concrete step from the brief or recent "
        f"discussion."
    )


def build_coordinator_nudge() -> str:
    return (
        f"[auto-mode] Still waiting. Take ONE action now:\n"
        f"  (a) /user:extract-orch-todo to queue next, OR\n"
        f"  (b) `orch distill done --reason '...'` to terminate.\n"
        f"No conversational reply — pick one and run it."
    )


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

    async def _wait_for_todo_or_done(self) -> tuple[Optional[TodoItem], str]:
        """Poll until an un-done crystallized todo appears, auto_done_reason
        is set, or we're canceled. Returns (todo, terminate_reason).

        If the coordinator goes silent (no new todo, no done flag) for
        NUDGE_INTERVAL_S seconds, re-inject a short prompt asking it
        to take one of the two protocol actions.
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
            todo = find_next_todo(ws, self.skip_todo_ids)
            if todo is not None:
                return todo, ""
            if _time.time() - last_nudge_at > NUDGE_INTERVAL_S:
                self.notify("coordinator silent — sending nudge")
                self.inject_coordinator(build_coordinator_nudge())
                last_nudge_at = _time.time()
            await asyncio.sleep(self.poll_interval)
        return None, "canceled"

    async def run(self) -> str:
        """Run the loop to completion. Returns the terminating reason."""
        self.store.load(force=True)
        ws = self.store.get(self.ws_id)
        if not ws:
            self.final_status = "workstream not found"
            return self.final_status

        # Clear stale done flag from a previous run
        if ws.auto_done_reason:
            ws.auto_done_reason = ""
            self.store.update(ws)

        # Count pending un-skipped crystallized todos for the kickoff message.
        pending = [
            t for t in ws.todos
            if t.origin == "crystallized" and not t.done and not t.archived
            and t.id not in self.skip_todo_ids
        ]
        self.inject_coordinator(build_coordinator_kickoff(ws, pending_count=len(pending)))
        self.notify(f"started ({len(pending)} todos queued)")

        todo = find_next_todo(ws, self.skip_todo_ids)
        if todo is None:
            self.notify("waiting for coordinator to crystallize first todo")
            todo, reason = await self._wait_for_todo_or_done()
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

            # Belt-and-suspenders: never re-attempt the same todo within
            # one run, even if the implementer didn't write a report (so
            # find_next_todo would still see it as un-done). Without this,
            # an impl that fails to report could be respawned in a tight
            # loop on the next iteration.
            self.skip_todo_ids.add(todo.id)

            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            cur = next((t for t in ws.todos if t.id == todo.id), None) if ws else None
            report = (cur.report if cur and cur.report else
                      "(implementer did not run `orch report` — no writeback)")
            self.last_report = report

            self.inject_coordinator(build_coordinator_followup(cur or todo, report))
            self.notify(f"iter {self.iteration}: report received, awaiting coordinator")

            next_todo, reason = await self._wait_for_todo_or_done()
            if reason:
                self.final_status = reason
                return self.final_status
            if next_todo is None:
                self.final_status = "canceled"
                return self.final_status
            todo = next_todo

        self.final_status = "canceled"
        return self.final_status
