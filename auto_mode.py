"""Auto-mode loop runner.

Drives the coordinator/implementer cycle for a workstream:
- coordinator decides what to dispatch next (every iteration, including the first)
- loop spawns implementer(s) for the chosen todo(s) — concurrently when the
  coordinator picks more than one (`orch distill next --todo-id a --todo-id b`)
- each implementer runs `orch report` to write a summary back
- loop injects the report(s) into the coordinator's PTY and waits
- coordinator picks again, or runs `orch distill done` to terminate

The loop never auto-advances through a pre-existing pending queue — every
iteration requires the coordinator to take one of three actions:
  (a) `orch distill crystallize` (or /user:extract-orch-todo) to queue a NEW todo
  (b) `orch distill next --todo-id <id>` (repeat flag for batch) to dispatch
      one or more EXISTING pending todos
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
import os
from datetime import datetime
from typing import Awaitable, Callable, Optional

from models import Store, TodoItem, Workstream

CANCEL_POLL_INTERVAL_S = 3.0  # how often to poll auto_cancel_requested

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
            f"  (a) `orch distill next --todo-id <id>` to dispatch one pending todo above.\n"
            f"      Pass `--todo-id` MULTIPLE times to dispatch a CONCURRENT batch — the\n"
            f"      loop spawns parallel implementers and waits for ALL of them before\n"
            f"      re-engaging you. Only batch when the work is genuinely independent\n"
            f"      (e.g. parallel research, separate-file edits with no shared state).\n"
            f"      When in doubt, dispatch ONE — sequential is safer.\n"
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
        f"reports back, you'll be prompted again — at that point you can also "
        f"dispatch a CONCURRENT batch by passing `--todo-id` multiple times to "
        f"`orch distill next` when the work is genuinely independent (e.g. parallel "
        f"research). Run `orch distill done --reason '...'` only when the workstream "
        f"is complete and no pending todos remain — it HARD-KILLS the auto-mode runner."
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
        "First, briefly state your read on the report and what you think "
        "should happen next — keep it proportional to what the report "
        "warrants (a sentence for routine completions, a short paragraph "
        "if there's something to untangle). Then take exactly ONE action:",
    ]
    if pending:
        plural = "todo" if len(pending) == 1 else "todos"
        listing = _format_pending_list(pending)
        batch_hint = ""
        if len(pending) >= 2:
            batch_hint = (
                "      Pass `--todo-id` MULTIPLE times to dispatch a CONCURRENT batch — "
                "only when the work is genuinely independent (e.g. parallel research, "
                "separate-file edits). When in doubt, dispatch ONE."
            )
        parts += [
            f"  (a) `orch distill next --todo-id <id>` to dispatch one of these pending {plural}:",
            listing,
        ]
        if batch_hint:
            parts.append(batch_hint)
        parts += [
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
        "Brief reasoning is welcome; do not stand by waiting for further "
        "input or write extended recaps. The loop is blocked until you take "
        "one of those actions, and the report above should drive the choice "
        "— continue with an existing item, crystallize a new step, or "
        "terminate.",
        "",
        "REMINDER: `distill done` is NOT an end-of-iteration marker. It exits "
        "the auto-mode runner entirely — there is no \"next loop\" that re-fires. "
        "If pending todos remain, dispatch one with (a). Use `done` only when "
        "the workstream is actually finished.",
    ]
    return "\n".join(parts)


def build_coordinator_followup_multi(
    items: list,
    pending_todos: Optional[list] = None,
) -> str:
    """Followup for a concurrent batch: multiple (TodoItem, report) tuples.

    Falls back to the single-todo wording when len(items) == 1 so that
    existing prompt-checking tests keep matching.
    """
    if len(items) <= 1:
        if not items:
            return ""
        t, r = items[0]
        return build_coordinator_followup(t, r, pending_todos=pending_todos)

    pending = pending_todos or []
    parts = [
        f"[auto-mode] {len(items)} implementers (concurrent batch) have finished.",
        "",
        "Reports:",
    ]
    for idx, (t, r) in enumerate(items, start=1):
        parts += [
            "",
            f"[{idx}] todo '{t.text}' ({t.id}):",
            r,
        ]
    parts += [
        "",
        "First, briefly state your read on the reports and what you think "
        "should happen next — keep it proportional to what the reports "
        "warrant (a sentence each for routine completions, a short paragraph "
        "where there's something to untangle). Then take exactly ONE action:",
    ]
    if pending:
        plural = "todo" if len(pending) == 1 else "todos"
        listing = _format_pending_list(pending)
        parts += [
            f"  (a) `orch distill next --todo-id <id>` to dispatch from these pending {plural}:",
            listing,
        ]
        if len(pending) >= 2:
            parts.append(
                "      Pass `--todo-id` MULTIPLE times for another CONCURRENT batch — "
                "only when the work is genuinely independent. When in doubt, dispatch ONE."
            )
        parts += [
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
        "Brief reasoning is welcome; do not stand by waiting for further "
        "input or write extended recaps. The loop is blocked until you take "
        "one of those actions.",
        "",
        "REMINDER: `distill done` is NOT an end-of-iteration marker. It exits "
        "the auto-mode runner entirely — there is no \"next loop\" that re-fires. "
        "If pending todos remain, dispatch one (or several) with (a). Use "
        "`done` only when the workstream is actually finished.",
    ]
    return "\n".join(parts)


def build_coordinator_nudge(pending_todos: Optional[list] = None) -> str:
    pending = pending_todos or []
    lines = [
        "[auto-mode] Still waiting on you. If you're mid-thought, take your "
        "time — this is just a reminder of your options:"
    ]
    if pending:
        lines.append("  (a) `orch distill next --todo-id <id>` to dispatch a pending todo, OR")
        lines.append("  (b) /user:extract-orch-todo to queue a new task, OR")
        lines.append("  (c) `orch distill done --reason '...'` to HARD-KILL auto-mode (refused while pending todos exist).")
    else:
        lines.append("  (a) /user:extract-orch-todo to queue next, OR")
        lines.append("  (b) `orch distill done --reason '...'` to HARD-KILL auto-mode (only if workstream is complete).")
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
        coord_sid: str = "",
    ):
        self.store = store
        self.ws_id = ws_id
        self.spawn_implementer = spawn_implementer
        self.inject_coordinator = inject_coordinator
        self.notify = notify or (lambda _: None)
        self.poll_interval = poll_interval
        self.skip_todo_ids: set = set(skip_todo_ids) if skip_todo_ids else set()
        self.coord_sid = coord_sid

        self.canceled = False
        self.iteration = 0
        self.current_todo_id: Optional[str] = None
        self.last_report: str = ""
        self.final_status: str = ""
        # Set by cancel(); awaitable so callers blocked on long polls (e.g.
        # waiting for an implementer's report) can race against it and exit
        # immediately instead of waiting for the next checkpoint.
        self.cancel_event = asyncio.Event()

    def cancel(self) -> None:
        self.canceled = True
        self.cancel_event.set()

    # ── Persisted-state writes ────────────────────────────────────
    # The owning orch process is the only writer for everything except
    # auto_cancel_requested; other processes set THAT, and the loop's
    # watchdog picks it up.

    def _mark_running(self) -> None:
        """Write the loop's start state to data.json so other processes
        can observe and signal it (cancel, status). Clears stale flags
        from a previous run."""
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None:
                return
            ws.auto_running = True
            ws.auto_pid = os.getpid()
            ws.auto_started_at = datetime.now().isoformat()
            ws.auto_iteration = 0
            ws.auto_current_todo_id = ""
            ws.auto_coord_sid = self.coord_sid
            ws.auto_impl_sids = []
            ws.auto_cancel_requested = False
            ws.auto_dispatched_todo_ids = sorted(self.skip_todo_ids)
            self.store.update(ws)
        except Exception:
            pass  # Best-effort observability; never fail the loop on a store write.

    def _persist_iteration(self) -> None:
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None:
                return
            ws.auto_iteration = self.iteration
            ws.auto_current_todo_id = self.current_todo_id or ""
            self.store.update(ws)
        except Exception:
            pass

    def _persist_dispatched_todo_ids(self) -> None:
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None:
                return
            ws.auto_dispatched_todo_ids = sorted(self.skip_todo_ids)
            self.store.update(ws)
        except Exception:
            pass

    def _mark_stopped(self) -> None:
        """Clear runtime flags so other processes know the loop isn't
        running anymore. Iteration / coord_sid / impl_sids are left as
        post-mortem data until the next start clears them."""
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None:
                return
            ws.auto_running = False
            ws.auto_pid = 0
            ws.auto_cancel_requested = False
            ws.auto_dispatched_todo_ids = []
            self.store.update(ws)
        except Exception:
            pass

    async def _watch_cancel_requested(self) -> None:
        """Poll the persisted auto_cancel_requested flag. If another
        process sets it, trigger self.cancel() — which sets cancel_event
        and unblocks every existing race in the loop. Exits cleanly when
        cancel_event is already set (loop wrapping up)."""
        while not self.canceled:
            try:
                await asyncio.wait_for(
                    self.cancel_event.wait(), timeout=CANCEL_POLL_INTERVAL_S,
                )
                return  # cancel happened locally; nothing more to do
            except asyncio.TimeoutError:
                pass
            try:
                self.store.load(force=True)
                ws = self.store.get(self.ws_id)
                if ws is not None and ws.auto_cancel_requested:
                    self.notify("cancel requested via persisted flag — exiting")
                    self.cancel()
                    return
            except Exception:
                pass

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
    ) -> tuple[list[TodoItem], str]:
        """Poll until the coordinator picks something or terminates.

        `existing_ids` is the snapshot of pending todo IDs at the moment
        the wait started. Those are gated — the loop will NOT dispatch
        them automatically. The coordinator must explicitly pick (via
        `orch distill next --todo-id <id>`, which sets ws.auto_next_todo_ids
        — possibly multiple IDs for a concurrent batch) or crystallize a
        fresh todo (a new id appears that wasn't in the snapshot).

        Returns (todos, terminate_reason). On dispatch: todos is a non-empty
        list of items to spawn (concurrently if len>1); reason is "".
        On terminate: todos is [] and reason explains why:
          - ws.auto_done_reason set → that string
          - canceled → "canceled"
          - workstream missing → "workstream not found"

        Fresh crystallizations are returned ONE at a time even if multiple
        appear in a single poll — the coordinator opts into concurrency
        explicitly via `distill next`, not implicitly by crystallizing fast.

        If the coordinator goes silent past NUDGE_INTERVAL_S, re-inject
        a short reminder.
        """
        import time as _time
        last_nudge_at = _time.time()
        while not self.canceled:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if not ws:
                return [], "workstream not found"
            if ws.auto_done_reason:
                return [], ws.auto_done_reason

            # (1) Coordinator explicitly picked one or more pending todos.
            if ws.auto_next_todo_ids:
                requested = list(ws.auto_next_todo_ids)
                picked: list[TodoItem] = []
                picked_ids: set[str] = set()
                for tid in requested:
                    if tid in picked_ids:
                        continue
                    match = next(
                        (t for t in ws.todos
                         if t.id == tid
                         and not t.done and not t.archived
                         and t.id not in self.skip_todo_ids),
                        None,
                    )
                    if match is not None:
                        picked.append(match)
                        picked_ids.add(match.id)
                # Always clear the signal — invalid picks are dropped, not
                # retried, so the coordinator gets a chance to pick again
                # on the next nudge.
                ws.auto_next_todo_ids = []
                self.store.update(ws)
                if picked:
                    return picked, ""

            # (2) A fresh todo was crystallized (id not in pre-wait snapshot).
            for t in self._pending_todos(ws):
                if t.id not in existing_ids:
                    return [t], ""

            # (3) Silent — nudge.
            if _time.time() - last_nudge_at > NUDGE_INTERVAL_S:
                self.notify("coordinator silent — sending nudge")
                pending = [t for t in self._pending_todos(ws) if t.id in existing_ids]
                self.inject_coordinator(build_coordinator_nudge(pending))
                last_nudge_at = _time.time()
            try:
                await asyncio.wait_for(
                    self.cancel_event.wait(), timeout=self.poll_interval,
                )
            except asyncio.TimeoutError:
                pass
        return [], "canceled"

    async def run(self) -> str:
        """Run the loop to completion. Returns the terminating reason.

        Every iteration — including the first — waits for the coordinator
        to explicitly pick one or more todos (via `orch distill next`, or
        a fresh crystallization) or terminate. Pre-existing pending todos
        do not auto-flow. When the coordinator picks multiple todos in
        one `distill next` call, they are dispatched as concurrent
        implementers and the loop waits for ALL reports before re-engaging
        the coordinator.
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
        if ws.auto_next_todo_ids:
            ws.auto_next_todo_ids = []
            dirty = True
        if ws.auto_dispatched_todo_ids:
            ws.auto_dispatched_todo_ids = []
            dirty = True
        if dirty:
            self.store.update(ws)

        # Mark this loop as the active owner BEFORE spawning the watchdog —
        # the watchdog polls auto_cancel_requested and would mis-fire on
        # leftover True from a previous run. _mark_running clears it.
        self._mark_running()
        cancel_watcher = asyncio.create_task(self._watch_cancel_requested())

        try:
            return await self._run_inner()
        finally:
            cancel_watcher.cancel()
            try:
                await cancel_watcher
            except (asyncio.CancelledError, Exception):
                pass
            self._mark_stopped()

    async def _run_inner(self) -> str:
        ws = self.store.get(self.ws_id)
        if not ws:
            self.final_status = "workstream not found"
            return self.final_status

        pending = self._pending_todos(ws)
        existing_ids = {t.id for t in pending}
        self.inject_coordinator(build_coordinator_kickoff(ws, pending_todos=pending))
        self.notify(f"started ({len(pending)} todos queued, awaiting coordinator pick)")

        batch, reason = await self._wait_for_todo_or_done(existing_ids)
        if reason or not batch:
            self.final_status = reason or "canceled"
            return self.final_status

        while not self.canceled:
            self.iteration += 1
            self.current_todo_id = batch[0].id  # informational; first of batch
            self._persist_iteration()
            if len(batch) == 1:
                t = batch[0]
                self.notify(f"iter {self.iteration}: spawning implementer for '{t.text[:60]}'")
            else:
                self.notify(
                    f"iter {self.iteration}: spawning {len(batch)} concurrent implementers "
                    f"({', '.join(t.id for t in batch)})"
                )

            # Spawn all implementers in parallel. Each spawn_implementer
            # call resolves when its own todo's report lands OR its claude
            # process exits cleanly — independently. asyncio.gather waits
            # for every dispatch to settle before re-engaging the coordinator.
            briefs = [(t, build_implementer_brief(t)) for t in batch]
            # Mark every dispatched todo as already-attempted BEFORE awaiting,
            # so a coordinator re-pick during the wait can't queue a duplicate.
            for t in batch:
                self.skip_todo_ids.add(t.id)
            # Persist the skip set so `orch distill next` (running in a
            # separate process) can refuse re-dispatch requests instead of
            # the loop silently filtering them — the silent path stranded
            # the coordinator in a nudge loop with false "✓ dispatched"
            # confirmations from the CLI.
            self._persist_dispatched_todo_ids()
            await asyncio.gather(
                *[self.spawn_implementer(t, brief) for t, brief in briefs]
            )
            if self.canceled:
                self.final_status = "canceled"
                return self.final_status

            # Read each report (with the same retry semantics as before).
            items: list[tuple[TodoItem, str]] = []
            for t in batch:
                report_text = await self._read_report(t.id)
                report = report_text or "(implementer did not run `orch report` — no writeback)"
                items.append((t, report))
            self.last_report = items[-1][1] if items else ""

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
                build_coordinator_followup_multi(items, pending_todos=pending_snap)
            )
            self.notify(
                f"iter {self.iteration}: {len(items)} report(s) received, "
                f"awaiting coordinator pick"
            )

            batch, reason = await self._wait_for_todo_or_done(existing_ids)
            if reason:
                self.final_status = reason
                return self.final_status
            if not batch:
                self.final_status = "canceled"
                return self.final_status

        self.final_status = "canceled"
        return self.final_status
