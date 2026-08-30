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

Quota gate: before each step that would burn tokens (kickoff, spawning
implementers, injecting a followup) the loop checks the account's Claude
subscription limits. When one is spent it parks — no dispatch, no
nudges — and wakes itself when the window resets. See `usage.py`.

Pure logic — no Textual imports. The TUI wires three callables:
  spawn_implementer(todo, brief) -> awaitable[None]
      Resolves whichever is sooner: todo.report becomes non-empty, OR
      the implementer's screen dismisses. Either signal advances the loop.
      Implementer may continue running in the background after report —
      that's fine; the next iteration will push another screen on top.
  inject_coordinator(text) -> None              (typed into coordinator's PTY)
  notify(line) -> None                          (status surfacing)

Every terminating path sets BOTH `final_status` (prose, and what run()
returns) and `final_kind` (one of `ExitKind`, for machines). A host that
outlives its terminal — `auto_runner` — persists the pair, because a
headless loop that dies quietly is strictly worse than a TUI that dies
visibly: at least a closed terminal is something you can see.

Two of those paths only exist when a host asks for them. `max_nudges`
and `max_silent_iterations` are None by default, which is right for the
TUI (a human is sitting there and can tell a thinking coordinator from a
dead one). A headless host has no such human, so it sets both, and a
coordinator that stops answering ends the run with a reason instead of
nudging an empty tmux session until morning.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from models import Store, TodoItem, Workstream

CANCEL_POLL_INTERVAL_S = 3.0  # how often to poll auto_cancel_requested

NUDGE_INTERVAL_S = 180.0  # 3 minutes of coordinator silence → re-prompt


class ExitKind:
    """Why a loop is no longer running.

    A string enum rather than an Enum so it round-trips through JSON and
    an old record still reads back as itself. The set is deliberately
    exhaustive over *observed* ends; VANISHED is the one nobody writes —
    it is what a reader concludes when the record says "running" and the
    pid says otherwise, which is the shape of SIGKILL and of the machine
    going down mid-run.
    """

    DONE = "done"                              # `orch distill done`
    CANCELED = "canceled"                      # `orch auto cancel`, or a local cancel()
    SIGNALED = "signaled"                      # SIGTERM/SIGINT (systemctl stop, kill)
    NO_WORKSTREAM = "no_workstream"            # ws_id stopped resolving mid-run
    COORDINATOR_SILENT = "coordinator_silent"  # nudge limit hit
    IMPLEMENTER_SILENT = "implementer_silent"  # implementers stopped reporting
    SPAWN_FAILED = "spawn_failed"              # never got a coordinator to talk to
    CRASHED = "crashed"                        # unhandled exception; detail holds the traceback
    VANISHED = "vanished"                      # inferred, never written: see above

    #: Ends that are the design working as designed.
    CLEAN = frozenset({DONE, CANCELED, SIGNALED})

# ── Quota gate tuning ────────────────────────────────────────────────
# While parked, the loop sleeps in bounded hops and re-reads the quota on
# each one, so it recovers if the reset lands early, the user tops up, or
# the first read was stale. MIN keeps a just-past-reset retry from
# hot-looping; MAX keeps a 5-hour park from sleeping through a change.
QUOTA_MIN_SLEEP_S = 5.0
QUOTA_MAX_SLEEP_S = 300.0
QUOTA_POLL_INTERVAL_S = 60.0   # used when no reset timestamp is available
QUOTA_RESET_BUFFER_S = 20.0    # wake a beat *after* the stated reset


def quota_wait_seconds(resume_at, now=None) -> float:
    """How long to sleep before re-reading the quota.

    Aims at `resume_at` plus a small buffer, clamped into
    [QUOTA_MIN_SLEEP_S, QUOTA_MAX_SLEEP_S]. With no reset timestamp,
    falls back to a plain poll interval.
    """
    if resume_at is None:
        return QUOTA_POLL_INTERVAL_S
    ref = now or datetime.now(timezone.utc)
    secs = (resume_at - ref).total_seconds() + QUOTA_RESET_BUFFER_S
    return max(QUOTA_MIN_SLEEP_S, min(QUOTA_MAX_SLEEP_S, secs))


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


def todos_with_live_implementer(todos, live_sids) -> list:
    """Todos whose recorded implementer session is still running.

    An implementer outlives the loop that spawned it — the cancel path
    says so out loud ("in-flight implementers keep running") — and
    nothing on the way out clears `impl_sid`. So a later run can ask
    tmux whether that session is still there.

    This is the only signal that survives the loop that created it:
    `auto_impl_sids` is wiped by _mark_running and
    `auto_dispatched_todo_ids` by _mark_stopped, which is why the
    in-run re-dispatch guard could not see across a restart. A cancel
    plus a restart put two implementers in one worktree on one todo.

    live_sids: live tmux session names (TerminalHost.list_tmux_sessions()).
    """
    live = set(live_sids or ())
    return [
        t for t in todos
        if getattr(t, "impl_sid", "") and t.impl_sid in live
        and not t.done and not t.archived
    ]


def record_todo_implementer(store, ws_id: str, todo_id: str, sid: str) -> None:
    """Stamp `todo_id` with the session now implementing it, and when.

    Written by whichever engine spawned the session; read back by a
    later run's `todos_with_live_implementer` and by the status line.
    Best-effort — never fail a spawn on a store write.
    """
    if not (ws_id and todo_id and sid):
        return
    try:
        store.load(force=True)
        ws = store.get(ws_id)
        if ws is None:
            return
        todo = next((t for t in ws.todos if t.id == todo_id), None)
        if todo is None:
            return
        todo.impl_sid = sid
        todo.impl_started_at = datetime.now().isoformat()
        store.update(ws)
    except Exception:
        pass


def clear_todo_implementers(store, ws_id: str, todo_ids) -> None:
    """Drop the implementer stamp once the loop's wait for it resolved.

    Leaving it set would make a finished todo look permanently in-flight
    to the next run's hold check and to the status line.
    """
    ids = {t for t in (todo_ids or ()) if t}
    if not (ws_id and ids):
        return
    try:
        store.load(force=True)
        ws = store.get(ws_id)
        if ws is None:
            return
        dirty = False
        for t in ws.todos:
            if t.id in ids and getattr(t, "impl_sid", ""):
                t.impl_sid = ""
                dirty = True
        if dirty:
            store.update(ws)
    except Exception:
        pass


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
        quota_gate: Optional[bool] = None,
        quota_threshold: Optional[float] = None,
        quota_kinds: Optional[tuple] = None,
        check_quota: Optional[Callable[[], object]] = None,
        max_nudges: Optional[int] = None,
        max_silent_iterations: Optional[int] = None,
    ):
        self.store = store
        self.ws_id = ws_id
        self.spawn_implementer = spawn_implementer
        self.inject_coordinator = inject_coordinator
        self.notify = notify or (lambda _: None)
        self.poll_interval = poll_interval
        self.skip_todo_ids: set = set(skip_todo_ids) if skip_todo_ids else set()
        self.coord_sid = coord_sid

        # Quota gate. Settings resolve from config.toml / env unless the
        # caller overrides them, so neither engine has to plumb anything
        # through to get the behaviour (see config.auto_quota_config).
        # check_quota is injectable purely so tests don't touch the network.
        from config import auto_quota_config
        qcfg = auto_quota_config()
        self.quota_gate = qcfg["enabled"] if quota_gate is None else bool(quota_gate)
        self.quota_threshold = (
            qcfg["percent"] if quota_threshold is None else float(quota_threshold)
        )
        self.quota_kinds = tuple(
            qcfg["kinds"] if quota_kinds is None else quota_kinds
        )
        self.check_quota = check_quota
        self.quota_paused = False
        self.quota_resume_at: Optional[datetime] = None

        # Give-up limits. None means "never give up", which is the right
        # default for the TUI: a human is watching, and a coordinator that
        # takes twenty minutes to think is not a coordinator that died.
        # A headless host sets both, because nobody is watching and an
        # unbounded nudge loop into a dead tmux session is the failure this
        # whole runner exists to make visible.
        self.max_nudges = max_nudges
        self.max_silent_iterations = max_silent_iterations
        self.silent_iterations = 0

        self.canceled = False
        # Who pulled the cord. "" until something does; "flag" for
        # `orch auto cancel`, whatever a host passes for its own reasons
        # (a headless host passes the signal name). A cancel is a clean
        # end either way, but "someone typed cancel" and "systemd stopped
        # the unit" are different answers to "why did it stop last night".
        self.cancel_source: str = ""
        self.iteration = 0
        self.current_todo_id: Optional[str] = None
        self.last_report: str = ""
        self.final_status: str = ""
        # Machine-readable twin of final_status. See ExitKind.
        self.final_kind: str = ""
        # Set by cancel(); awaitable so callers blocked on long polls (e.g.
        # waiting for an implementer's report) can race against it and exit
        # immediately instead of waiting for the next checkpoint.
        self.cancel_event = asyncio.Event()

    def cancel(self, source: str = "") -> None:
        self.canceled = True
        if source and not self.cancel_source:
            self.cancel_source = source
        self.cancel_event.set()

    def _finish(self, kind: str, reason: str) -> str:
        """Record the terminating reason in both forms and return the prose.

        Every `return` out of the loop goes through here so the two can
        never drift — a status without a kind is a status a headless host
        cannot classify, and classifying it by string-matching prose is
        how "parked" gets mistaken for "stopped".
        """
        self.final_kind = kind
        self.final_status = reason
        return reason

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
            ws.auto_paused = False
            ws.auto_pause_reason = ""
            ws.auto_resume_at = ""
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
            ws.auto_paused = False
            ws.auto_pause_reason = ""
            ws.auto_resume_at = ""
            self.store.update(ws)
        except Exception:
            pass

    def _persist_quota_pause(self, reason: str, resume_at) -> None:
        """Publish the park to data.json so other orch instances and
        `orch auto status` can tell 'waiting on quota until 9am' apart
        from 'wedged'."""
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None:
                return
            ws.auto_paused = True
            ws.auto_pause_reason = reason
            ws.auto_resume_at = resume_at.isoformat() if resume_at else ""
            self.store.update(ws)
        except Exception:
            pass  # Best-effort observability; never fail the loop on a store write.

    def _clear_quota_pause(self) -> None:
        try:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if ws is None or not ws.auto_paused:
                return
            ws.auto_paused = False
            ws.auto_pause_reason = ""
            ws.auto_resume_at = ""
            self.store.update(ws)
        except Exception:
            pass

    # ── Quota gate ────────────────────────────────────────────────
    # Fails OPEN throughout: an unreadable quota (no OAuth token, network
    # down, API shape changed) lets the loop run. Getting rate-limited is
    # recoverable — the in-session watchdog parks Claude until reset —
    # whereas a gate that fails closed silently kills every loop the
    # moment the endpoint hiccups.

    async def _blocking_limits(self) -> list:
        """Watched quota limits that are spent right now.

        Empty when the gate is disabled or the quota can't be read.
        """
        if not self.quota_gate:
            return []
        probe = self.check_quota
        if probe is None:
            from usage import get_usage
            probe = get_usage
        try:
            snap = await asyncio.to_thread(probe)
        except Exception:
            return []
        if snap is None:
            return []
        try:
            return list(snap.blocking(self.quota_threshold, self.quota_kinds))
        except Exception:
            return []

    async def _await_quota(self, context: str) -> bool:
        """Hold the loop while a Claude subscription limit is spent.

        Returns True when it's safe to burn tokens, False if the loop was
        canceled while parked. Sleeps in bounded hops (see
        `quota_wait_seconds`), re-reading the quota each time, so an early
        reset or a topped-up balance is picked up without waiting out the
        full estimate.
        """
        from usage import describe_limits, format_eta, soonest_reset

        announced = False
        while not self.canceled:
            blocking = await self._blocking_limits()
            if not blocking:
                if announced:
                    self.notify("quota reset — resuming")
                    self.quota_paused = False
                    self.quota_resume_at = None
                    self._clear_quota_pause()
                return True

            resume_at = soonest_reset(blocking)
            reason = describe_limits(blocking)
            self.quota_paused = True
            self.quota_resume_at = resume_at
            if not announced:
                eta = format_eta(resume_at)
                self.notify(
                    f"paused before {context}: {reason}"
                    + (f" — resumes {eta}" if eta else "")
                )
                announced = True
            self._persist_quota_pause(reason, resume_at)

            try:
                await asyncio.wait_for(
                    self.cancel_event.wait(),
                    timeout=quota_wait_seconds(resume_at),
                )
                break  # canceled
            except asyncio.TimeoutError:
                pass  # slept a hop; re-read the quota

        self.quota_paused = False
        self.quota_resume_at = None
        self._clear_quota_pause()
        return False

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
                    self.cancel(source="flag")
                    return
            except Exception:
                pass

    async def _live_tmux_sessions(self) -> set:
        """Live session names on the orch tmux socket.

        Empty on any failure — the hold check fails OPEN, matching the
        quota gate: an unreadable tmux must not stop the loop dispatching.
        """
        try:
            from term_host import TerminalHost
            names = await asyncio.to_thread(TerminalHost.list_tmux_sessions)
            return set(names or ())
        except Exception:
            return set()

    async def _hold_todos_with_live_implementer(self, ws) -> list:
        """Add every todo with a still-running implementer to the skip set.

        Called once at start, before _mark_running publishes the skip set
        as auto_dispatched_todo_ids — which is what makes `orch distill
        next` refuse them too, in the coordinator's own process.
        """
        held = todos_with_live_implementer(
            ws.todos, await self._live_tmux_sessions())
        for t in held:
            self.skip_todo_ids.add(t.id)
        if held:
            self.notify(
                f"holding {len(held)} todo(s) — implementer still running: "
                + ", ".join(f"{t.id} ({t.impl_sid[:8]})" for t in held)
            )
        return held

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
    ) -> tuple[list[TodoItem], str, str]:
        """Poll until the coordinator picks something or terminates.

        `existing_ids` is the snapshot of pending todo IDs at the moment
        the wait started. Those are gated — the loop will NOT dispatch
        them automatically. The coordinator must explicitly pick (via
        `orch distill next --todo-id <id>`, which sets ws.auto_next_todo_ids
        — possibly multiple IDs for a concurrent batch) or crystallize a
        fresh todo (a new id appears that wasn't in the snapshot).

        Returns (todos, terminate_reason, exit_kind). On dispatch: todos is
        a non-empty list of items to spawn (concurrently if len>1); reason
        and kind are "". On terminate: todos is [] and reason explains why:
          - ws.auto_done_reason set → that string        (ExitKind.DONE)
          - canceled → "canceled"                        (ExitKind.CANCELED)
          - workstream missing → "workstream not found"  (ExitKind.NO_WORKSTREAM)
          - max_nudges exhausted                         (ExitKind.COORDINATOR_SILENT)

        Fresh crystallizations are returned ONE at a time even if multiple
        appear in a single poll — the coordinator opts into concurrency
        explicitly via `distill next`, not implicitly by crystallizing fast.

        If the coordinator goes silent past NUDGE_INTERVAL_S, re-inject
        a short reminder — up to `max_nudges` times, after which the
        silence is the answer and the loop stops saying so.
        """
        import time as _time
        last_nudge_at = _time.time()
        nudges_sent = 0
        while not self.canceled:
            self.store.load(force=True)
            ws = self.store.get(self.ws_id)
            if not ws:
                return [], "workstream not found", ExitKind.NO_WORKSTREAM
            if ws.auto_done_reason:
                return [], ws.auto_done_reason, ExitKind.DONE

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
                    return picked, "", ""

            # (2) A fresh todo was crystallized (id not in pre-wait snapshot).
            for t in self._pending_todos(ws):
                if t.id not in existing_ids:
                    return [t], "", ""

            # (3) Silent — nudge. Unless the quota is spent, in which case
            # the coordinator's own Claude is parked too and nudges would
            # just pile up in an input box nobody is reading.
            if _time.time() - last_nudge_at > NUDGE_INTERVAL_S:
                if await self._blocking_limits():
                    # A parked coordinator is not a silent one — its Claude
                    # is waiting on the same reset we are. Holding the nudge
                    # AND not counting it is what keeps a long quota park
                    # from being retold later as "the coordinator died".
                    self.notify("quota spent — holding nudges until reset")
                else:
                    nudges_sent += 1
                    if (self.max_nudges is not None
                            and nudges_sent > self.max_nudges):
                        silent_for = int(self.max_nudges * NUDGE_INTERVAL_S / 60)
                        return [], (
                            f"coordinator silent through {self.max_nudges} "
                            f"nudges (~{silent_for} min)"
                        ), ExitKind.COORDINATOR_SILENT
                    self.notify(
                        f"coordinator silent — sending nudge {nudges_sent}"
                        + (f"/{self.max_nudges}" if self.max_nudges else "")
                    )
                    pending = [t for t in self._pending_todos(ws) if t.id in existing_ids]
                    self.inject_coordinator(build_coordinator_nudge(pending))
                last_nudge_at = _time.time()
            try:
                await asyncio.wait_for(
                    self.cancel_event.wait(), timeout=self.poll_interval,
                )
            except asyncio.TimeoutError:
                pass
        return [], "canceled", ExitKind.CANCELED

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
            return self._finish(ExitKind.NO_WORKSTREAM, "workstream not found")

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

        # An implementer outlives the loop that spawned it (cancel says so:
        # "in-flight implementers keep running"). Dispatching its todo again
        # puts two Claude sessions in one worktree on one task. Hold anything
        # whose implementer is still alive in tmux — the only cross-run
        # signal, since the in-run guard's state is wiped at start/stop.
        await self._hold_todos_with_live_implementer(ws)

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
            return self._finish(ExitKind.NO_WORKSTREAM, "workstream not found")

        if not await self._await_quota("kickoff"):
            return self._finish(ExitKind.CANCELED, "canceled")

        pending = self._pending_todos(ws)
        existing_ids = {t.id for t in pending}
        self.inject_coordinator(build_coordinator_kickoff(ws, pending_todos=pending))
        self.notify(f"started ({len(pending)} todos queued, awaiting coordinator pick)")

        batch, reason, kind = await self._wait_for_todo_or_done(existing_ids)
        if reason or not batch:
            return self._finish(
                kind or ExitKind.CANCELED, reason or "canceled")

        while not self.canceled:
            if not await self._await_quota("dispatch"):
                return self._finish(ExitKind.CANCELED, "canceled")

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
                # Do NOT clear the implementer stamp here. A cancel unblocks
                # the wait without stopping the implementer — that stamp is
                # precisely what tells the next run this todo is taken.
                return self._finish(ExitKind.CANCELED, "canceled")

            # The wait resolved on its own terms (report written, or the
            # session exited), so nothing is in flight — drop the stamp
            # before the next run reads it as live.
            clear_todo_implementers(
                self.store, self.ws_id, [t.id for t in batch])

            # Read each report (with the same retry semantics as before).
            items: list[tuple[TodoItem, str]] = []
            wrote_back = 0
            for t in batch:
                report_text = await self._read_report(t.id)
                if report_text:
                    wrote_back += 1
                report = report_text or "(implementer did not run `orch report` — no writeback)"
                items.append((t, report))
            self.last_report = items[-1][1] if items else ""

            # A whole batch with no writeback means the implementers are
            # dying before they report — a worktree that won't open, a
            # command that isn't there, a prompt they can't run. One such
            # iteration is worth telling the coordinator about; a run of
            # them is a loop burning quota to produce nothing, and the
            # headless host stops rather than doing that all night.
            self.silent_iterations = 0 if wrote_back else self.silent_iterations + 1
            if (self.max_silent_iterations is not None
                    and self.silent_iterations >= self.max_silent_iterations):
                return self._finish(
                    ExitKind.IMPLEMENTER_SILENT,
                    f"{self.silent_iterations} consecutive iterations with no "
                    f"implementer writeback",
                )

            # The batch that just finished may have spent the last of the
            # quota. Hold the followup rather than typing it into a
            # coordinator that's parked on its own limit prompt.
            if not await self._await_quota("coordinator followup"):
                return self._finish(ExitKind.CANCELED, "canceled")

            # Snapshot pending todos AS OF NOW — anything created after
            # this point counts as a fresh coordinator decision and will
            # advance the loop. Pre-existing pending items are gated and
            # require explicit `orch distill next --todo-id <id>`.
            self.store.load(force=True)
            ws_snap = self.store.get(self.ws_id)
            if not ws_snap:
                return self._finish(
                    ExitKind.NO_WORKSTREAM, "workstream not found")
            pending_snap = self._pending_todos(ws_snap)
            existing_ids = {t.id for t in pending_snap}

            self.inject_coordinator(
                build_coordinator_followup_multi(items, pending_todos=pending_snap)
            )
            self.notify(
                f"iter {self.iteration}: {len(items)} report(s) received, "
                f"awaiting coordinator pick"
            )

            batch, reason, kind = await self._wait_for_todo_or_done(existing_ids)
            if reason:
                return self._finish(kind or ExitKind.DONE, reason)
            if not batch:
                return self._finish(ExitKind.CANCELED, "canceled")

        return self._finish(ExitKind.CANCELED, "canceled")
