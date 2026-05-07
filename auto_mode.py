"""Auto-mode loop runner.

Drives the coordinator/implementer cycle for a workstream:
- coordinator decides what to crystallize next
- loop spawns an implementer for each crystallized todo
- implementer runs `orch report` to write a summary back
- loop injects that summary into the coordinator's PTY and waits
- coordinator runs `orch distill done` to terminate the loop

Pure logic — no Textual imports. The TUI wires three callables:
  spawn_implementer(brief) -> awaitable[None]   (resolves on implementer dismiss)
  inject_coordinator(text) -> None              (typed into coordinator's PTY)
  notify(line) -> None                          (status surfacing)
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from models import Store, TodoItem, Workstream


def find_next_todo(ws: Workstream) -> Optional[TodoItem]:
    """Next un-done crystallized todo, or None.

    Manual todos are intentionally skipped — auto mode only consumes
    crystallized briefs (implementers need rich context, not bare text).
    """
    for todo in ws.todos:
        if todo.archived or todo.done:
            continue
        if todo.origin != "crystallized":
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


def build_coordinator_kickoff(ws: Workstream) -> str:
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
        f"Decide what's next: crystallize another todo with "
        f"/user:extract-orch-todo (or `orch distill crystallize`), or run "
        f"`orch distill done --reason '...'` if the workstream is complete."
    )


class AutoMode:
    """Sequential coordinator/implementer loop. Cooperates with cancel()."""

    def __init__(
        self,
        store: Store,
        ws_id: str,
        spawn_implementer: Callable[[str], Awaitable[None]],
        inject_coordinator: Callable[[str], None],
        notify: Optional[Callable[[str], None]] = None,
        poll_interval: float = 2.0,
    ):
        self.store = store
        self.ws_id = ws_id
        self.spawn_implementer = spawn_implementer
        self.inject_coordinator = inject_coordinator
        self.notify = notify or (lambda _: None)
        self.poll_interval = poll_interval

        self.canceled = False
        self.iteration = 0
        self.current_todo_id: Optional[str] = None
        self.last_report: str = ""
        self.final_status: str = ""

    def cancel(self) -> None:
        self.canceled = True

    async def _wait_for_todo_or_done(self) -> tuple[Optional[TodoItem], str]:
        """Poll until an un-done crystallized todo appears, auto_done_reason
        is set, or we're canceled. Returns (todo, terminate_reason)."""
        while not self.canceled:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if not ws:
                return None, "workstream not found"
            if ws.auto_done_reason:
                return None, ws.auto_done_reason
            todo = find_next_todo(ws)
            if todo is not None:
                return todo, ""
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

        todo = find_next_todo(ws)
        if todo is None:
            self.inject_coordinator(build_coordinator_kickoff(ws))
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

            await self.spawn_implementer(brief)
            if self.canceled:
                self.final_status = "canceled"
                return self.final_status

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
