"""Headless host for the auto-mode loop.

Auto mode's logic has never needed a UI — `auto_mode.AutoMode` takes three
callables and drives the coordinator/implementer cycle through them. What
it needed was a *process*, and until now the only process on offer was the
TUI. So closing the terminal, an X session ending, or a Textual traceback
took the loop with it, and the queue simply stopped. It did that once,
overnight, with nothing said. This module is the second host: the same
three callables, wired to tmux and a log file instead of to a screen.

The three callables, headless:

  inject_coordinator  tmux send-keys into the coordinator's session, with
                      bracketed-paste markers so embedded newlines arrive
                      as paste content and Enter submits. Identical to the
                      TUI's, and deliberately so — it writes to the tmux
                      session, not to an attached pane, which is why it
                      works with nobody watching.
  spawn_implementer   spawn_implementer_session + a race between "the todo's
                      report field was written", "the tmux session died" and
                      "auto-mode was canceled".
  notify              a timestamped line in the run log.

WHY IT STOPPED
--------------
A headless runner that dies quietly is strictly worse than a TUI that dies
visibly: a closed terminal is at least something you can see. So every exit
path lands in a durable record (`RunnerRecord`) alongside the log, and the
three states a reader actually needs to tell apart are:

  PARKED    the quota is spent and the loop is asleep waiting for the
            reset. This is NOT an exit. The loop parks itself and wakes
            itself; the host's only job is to still be here when it does.
  STOPPED   it ended, and `exit_kind`/`exit_reason` say how.
  CRASHED   it ended on an exception, and `exit_detail` holds the traceback.

plus one nobody writes:

  VANISHED  the record says running and the pid says otherwise. That is
            the shape of SIGKILL and of the machine going down mid-run —
            the two ends where the dying process gets no say. Reporting it
            as "running" is the lie that started all this.

The record lives under ~/.local/state (XDG's home for state that must
survive a restart) rather than ~/.cache with the rest of orch's scratch:
the whole point of the exit reason is that it is still there tomorrow
morning, and ~/.cache is a directory whose contract says it may not be.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from auto_mode import ExitKind

# ── Where the durable record lives ───────────────────────────────────

def state_dir() -> Path:
    """Directory holding per-workstream runner records, logs and locks.

    Resolved per call, not at import, so a test (or a second machine
    sharing a home directory) can point ORCH_AUTO_STATE_DIR elsewhere
    without the import order deciding for it.
    """
    override = os.environ.get("ORCH_AUTO_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-orchestrator" / "auto"


def record_path(ws_id: str) -> Path:
    return state_dir() / f"{ws_id}.json"


def log_path(ws_id: str) -> Path:
    return state_dir() / f"{ws_id}.log"


def lock_path(ws_id: str) -> Path:
    return state_dir() / f"{ws_id}.lock"


# ── States a reader can be in ────────────────────────────────────────

class RunnerState:
    IDLE = "idle"          # no headless run has ever been recorded here
    RUNNING = "running"
    PARKED = "parked"      # quota spent; asleep, not stopped
    STOPPED = "stopped"
    CRASHED = "crashed"
    VANISHED = "vanished"  # claimed running, pid is gone, no exit written
    FOREIGN = "foreign"    # recorded on another host; we cannot check its pid


# Exit statuses of the runner *process*, for a systemd unit to reason about.
EXIT_OK = 0             # done / canceled / signaled — the design working
EXIT_CRASHED = 1        # unhandled exception
EXIT_GAVE_UP = 3        # coordinator or implementers went silent
EXIT_REFUSED = 4        # another loop already owns this workstream
EXIT_NO_START = 5       # never got far enough to have a loop

_EXIT_STATUS_BY_KIND = {
    ExitKind.DONE: EXIT_OK,
    ExitKind.CANCELED: EXIT_OK,
    ExitKind.SIGNALED: EXIT_OK,
    ExitKind.COORDINATOR_SILENT: EXIT_GAVE_UP,
    ExitKind.COORDINATOR_GONE: EXIT_GAVE_UP,
    ExitKind.IMPLEMENTER_SILENT: EXIT_GAVE_UP,
    ExitKind.NO_WORKSTREAM: EXIT_NO_START,
    ExitKind.SPAWN_FAILED: EXIT_NO_START,
    ExitKind.CRASHED: EXIT_CRASHED,
}


def exit_status_for(kind: str) -> int:
    return _EXIT_STATUS_BY_KIND.get(kind, EXIT_CRASHED)


# ── The record ───────────────────────────────────────────────────────

@dataclass
class RunnerRecord:
    """One headless run, from start to whatever ended it.

    Written by the host at start, on every heartbeat, and once more on the
    way out. Deliberately a sidecar rather than more `auto_*` columns on
    the Workstream: data.json is a shared document with merge semantics
    tuned for todos and three other writers, and a heartbeat every 20
    seconds does not belong in it.
    """

    ws_id: str = ""
    ws_name: str = ""
    pid: int = 0
    host: str = ""
    coord_sid: str = ""
    started_at: str = ""
    heartbeat_at: str = ""
    iteration: int = 0
    current_todo_id: str = ""
    parked: bool = False
    park_reason: str = ""
    park_until: str = ""
    last_note: str = ""
    last_note_at: str = ""
    # Empty until something ends the run. Their presence IS the signal
    # that this record describes a finished run, which is why nothing
    # sets exit_kind speculatively.
    exit_kind: str = ""
    exit_reason: str = ""
    exit_detail: str = ""
    exit_at: str = ""

    def save(self) -> None:
        """Atomically replace the record. Best-effort, like every other
        observability write in auto mode — the run matters more than the
        note about the run."""
        try:
            path = record_path(self.ws_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2))
            tmp.replace(path)
        except Exception:
            pass

    @classmethod
    def load(cls, ws_id: str) -> Optional["RunnerRecord"]:
        """The last recorded run for `ws_id`, or None if there isn't one.

        A record that is unreadable is treated as absent rather than
        raising: `orch auto status` on a truncated file should still tell
        you about the other workstreams.
        """
        try:
            raw = json.loads(record_path(ws_id).read_text())
        except Exception:
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def finish(self, kind: str, reason: str, detail: str = "") -> None:
        self.exit_kind = kind
        self.exit_reason = reason
        self.exit_detail = detail
        self.exit_at = datetime.now().isoformat(timespec="seconds")
        self.parked = False
        self.save()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except Exception:
        return False


def classify(rec: Optional[RunnerRecord]) -> tuple[str, str]:
    """(state, one-line explanation) for a record.

    The one function that decides parked-vs-stopped-vs-crashed, so
    `orch auto status`, the tests and any future reader all agree. Note
    that VANISHED is a conclusion, never a stored value: it is what an
    unfinished record plus a dead pid means, and it is the only honest
    thing to say about a run that was killed outright.
    """
    if rec is None:
        return RunnerState.IDLE, "no headless run recorded"
    if rec.exit_kind:
        state = (RunnerState.CRASHED if rec.exit_kind == ExitKind.CRASHED
                 else RunnerState.STOPPED)
        return state, rec.exit_reason or rec.exit_kind
    if rec.host and rec.host != socket.gethostname():
        return RunnerState.FOREIGN, (
            f"recorded on {rec.host}; pid {rec.pid} cannot be checked from here"
        )
    if not _pid_alive(rec.pid):
        return RunnerState.VANISHED, (
            f"pid {rec.pid} is gone and wrote no exit reason — killed outright "
            f"(SIGKILL) or the machine went down; last heartbeat "
            f"{rec.heartbeat_at or rec.started_at or 'unknown'}"
        )
    if rec.parked:
        until = f" until {rec.park_until}" if rec.park_until else ""
        return RunnerState.PARKED, (
            f"{rec.park_reason or 'quota spent'}{until} — asleep, not stopped"
        )
    return RunnerState.RUNNING, rec.last_note or "started"


# ── One loop per workstream ──────────────────────────────────────────

class InstanceLock:
    """An exclusive, self-releasing claim on a workstream's loop.

    flock rather than a pid file because the kernel drops it when the
    holder dies however it dies — a crash must not wedge every future
    start, and the whole point of this runner is that crashes happen
    where nobody is looking.

    Not a substitute for the `auto_running` check: a TUI-hosted loop
    takes no lock, so a headless start also has to look at data.json.
    Two cheap checks, and refusing twice is the correct failure here —
    two hosts driving one coordinator is exactly the collision that put
    two implementers in one worktree.
    """

    def __init__(self, ws_id: str):
        self.path = lock_path(ws_id)
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.close(self.fd)   # closing drops the flock
        except OSError:
            pass
        self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def holder_pid(ws_id: str) -> int:
    """PID currently holding the loop lock for `ws_id`, or 0.

    Probes by trying to take the lock: if we get it, nobody had it, and
    we drop it again immediately. The pid written inside the file is only
    a label for the message — the lock itself is the truth.
    """
    import fcntl
    path = lock_path(ws_id)
    if not path.exists():
        return 0
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return 0
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return int((path.read_text().strip() or "0"))
            except Exception:
                return -1  # held, by someone who did not write a readable pid
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0
    finally:
        os.close(fd)


# ── tmux plumbing ────────────────────────────────────────────────────

def _tmux_socket() -> str:
    from term_host import TerminalHost
    return TerminalHost.TMUX_SOCKET


def capture_pane(sid: str) -> str:
    """Visible text of a tmux session's active pane, or "" on any failure."""
    try:
        out = subprocess.run(
            ["tmux", "-L", _tmux_socket(), "capture-pane", "-p", "-t", sid],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


# What a Claude session that is ready for input looks like, taken from a
# real spawn rather than guessed at. The startup banner is the marker
# that matters: it is drawn only AFTER the workspace-trust question is
# settled, so it separates "ready" from "sitting on a modal dialog whose
# default answer is to exit" — which is what the first draft of this
# matched on the prompt glyph and got wrong, because the trust dialog
# draws that glyph too, on the word "No, exit".
READY_MARKERS = ("Claude Code v", "? for shortcuts")


def wait_for_claude_ready(sid: str, timeout: float = 60.0,
                          poll: float = 1.0) -> bool:
    """Block until a freshly-spawned Claude session shows its input box.

    Returns False on timeout, and the caller injects anyway: a kickoff
    typed into a session that is not listening yet is lost, but it is not
    fatal — `_wait_for_todo_or_done` re-sends it as a nudge. Refusing to
    start because a boot took 61 seconds would be the worse trade.

    Raises RuntimeError if the session is gone, which is a different
    thing entirely and must not be waited out. Claude's first run in a
    directory asks whether you trust it and *exits* on the default
    answer, so a session that dies during startup is the single most
    likely way this goes wrong.
    """
    from term_host import TerminalHost

    deadline = time.time() + timeout
    while time.time() < deadline:
        text = capture_pane(sid)
        if any(m in text for m in READY_MARKERS):
            return True
        if not TerminalHost.tmux_session_alive(sid):
            raise RuntimeError(
                f"coordinator session {sid[:8]} exited during startup. "
                f"Claude asks 'is this a project you trust?' on its first "
                f"run in a directory and exits on the default answer — "
                f"start a session there by hand once, or `orch trust add "
                f"<dir>`, then try again"
            )
        time.sleep(poll)
    return False


# ── The host ─────────────────────────────────────────────────────────

HEARTBEAT_S = 20.0
# A wall-clock jump this much larger than the elapsed monotonic time means
# the machine was suspended. Worth a log line: "the loop did nothing for
# six hours" and "the laptop was shut" look identical in a timestamp
# column otherwise.
SLEEP_SKEW_S = 120.0

# Headless give-up limits (see AutoMode.max_nudges). 10 nudges at the
# 180s nudge interval is half an hour of a coordinator saying nothing at
# all — past that it is not thinking, and something is wrong that another
# nudge will not fix.
DEFAULT_MAX_NUDGES = 10
DEFAULT_MAX_SILENT_ITERATIONS = 3


class HeadlessRunner:
    """Owns one AutoMode loop, its log, and its record."""

    def __init__(
        self,
        store,
        ws_id: str,
        coord_sid: str,
        skip_todo_ids: Optional[set] = None,
        max_nudges: Optional[int] = DEFAULT_MAX_NUDGES,
        max_silent_iterations: Optional[int] = DEFAULT_MAX_SILENT_ITERATIONS,
        poll_interval: float = 2.0,
        check_quota=None,
    ):
        self.store = store
        self.ws_id = ws_id
        self.coord_sid = coord_sid
        self.skip_todo_ids = set(skip_todo_ids or ())
        self.max_nudges = max_nudges
        self.max_silent_iterations = max_silent_iterations
        self.poll_interval = poll_interval
        # Passed straight through to AutoMode, and injectable for the same
        # reason it is there: a test of the park must not call the usage
        # endpoint to find out whether it is time to park.
        self.check_quota = check_quota
        self.mode = None
        self.record = RunnerRecord(
            ws_id=ws_id,
            pid=os.getpid(),
            host=socket.gethostname(),
            coord_sid=coord_sid,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        ws = store.get(ws_id)
        if ws is not None:
            self.record.ws_name = ws.name

    # ── log ──────────────────────────────────────────────────────────

    def log(self, line: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            path = log_path(self.ws_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(f"{stamp}  {line}\n")
        except Exception:
            pass

    def note(self, line: str) -> None:
        """A loop notification: to the log, and to the record's last_note
        so `orch auto status` can say what it was doing without opening
        the log."""
        self.log(line)
        self.record.last_note = line
        self.record.last_note_at = datetime.now().isoformat(timespec="seconds")

    # ── the three callables ──────────────────────────────────────────

    def inject_coordinator(self, text: str) -> None:
        paste = f"\x1b[200~{text}\x1b[201~"
        sock = _tmux_socket()
        try:
            subprocess.run(
                ["tmux", "-L", sock, "send-keys", "-t", self.coord_sid,
                 "-l", paste],
                timeout=10, capture_output=True, check=True,
            )
            subprocess.run(
                ["tmux", "-L", sock, "send-keys", "-t", self.coord_sid, "Enter"],
                timeout=10, capture_output=True, check=True,
            )
        except Exception as e:
            # Not fatal on its own. A coordinator session that has actually
            # died shows up as silence, and the nudge limit is what turns
            # that silence into a recorded reason.
            self.log(f"inject failed: {e}")

    async def spawn_implementer(self, todo, brief: str) -> None:
        """Spawn one implementer and wait for it to report, exit, or be
        canceled — the headless twin of the TUI's callable, including the
        per-todo stamp that survives this process."""
        from auto_mode import record_todo_implementer
        from session_launch import (
            auto_link_session, log_session_exit, spawn_implementer_session,
        )
        from term_host import TerminalHost

        ws = self.store.get(self.ws_id)
        if ws is None:
            self.log("implementer spawn skipped: workstream vanished")
            return

        start_time = time.time()
        try:
            sid, _jsonl = await asyncio.to_thread(
                spawn_implementer_session, ws, self.store, brief)
        except Exception as e:
            self.log(f"implementer spawn failed for {todo.id}: {e}")
            return
        self.log(f"implementer {sid[:8]} spawned for todo {todo.id}")

        try:
            self.store.load(force=True)
            cur = self.store.get(self.ws_id)
            if cur is not None and sid not in cur.auto_impl_sids:
                cur.auto_impl_sids.append(sid)
                self.store.update(cur)
        except Exception:
            pass
        # The stamp b82a302 added. It is the ONLY thing that survives this
        # process to tell a later run that this todo is taken — and a new
        # host process is exactly the situation that regressed it — so the
        # headless path writes it for the same reason the TUI does.
        record_todo_implementer(self.store, self.ws_id, todo.id, sid)

        async def wait_for_report():
            while True:
                try:
                    self.store.load(force=True)
                except Exception:
                    await asyncio.sleep(2)
                    continue
                cur_ws = self.store.get(self.ws_id)
                if cur_ws is None:
                    await asyncio.sleep(2)
                    continue
                t = next((x for x in cur_ws.todos if x.id == todo.id), None)
                if t is None:
                    await asyncio.sleep(2)
                    continue
                if t.report:
                    return
                await asyncio.sleep(2)

        async def wait_for_tmux_exit():
            while True:
                alive = await asyncio.to_thread(
                    TerminalHost.tmux_session_alive, sid)
                if not alive:
                    return
                await asyncio.sleep(3)

        report_task = asyncio.create_task(wait_for_report())
        exit_task = asyncio.create_task(wait_for_tmux_exit())
        cancel_task = asyncio.create_task(self.mode.cancel_event.wait())
        try:
            await asyncio.wait(
                [report_task, exit_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task.done():
                self.log(f"implementer {sid[:8]}: wait released by cancel")
                return
            if exit_task.done() and not report_task.done():
                auto_link_session(self.store, self.ws_id, sid)
                log_session_exit(sid, ws.name, start_time, exit_type="headless")
                self.log(f"implementer {sid[:8]} exited without reporting")
            elif report_task.done():
                self.log(f"implementer {sid[:8]} reported on todo {todo.id}")
        finally:
            report_task.cancel()
            exit_task.cancel()
            cancel_task.cancel()

    # ── heartbeat ────────────────────────────────────────────────────

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        wall = time.time()
        mono = time.monotonic()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
                return
            except asyncio.TimeoutError:
                pass
            now_wall, now_mono = time.time(), time.monotonic()
            skew = (now_wall - wall) - (now_mono - mono)
            if skew > SLEEP_SKEW_S:
                self.log(
                    f"wall clock jumped {skew / 60:.0f} min past elapsed time "
                    f"— the machine was probably suspended; the loop survived it"
                )
            wall, mono = now_wall, now_mono

            m = self.mode
            if m is not None:
                self.record.iteration = m.iteration
                self.record.current_todo_id = m.current_todo_id or ""
                self.record.parked = bool(m.quota_paused)
                if m.quota_paused:
                    ws = self.store.get(self.ws_id)
                    self.record.park_reason = (
                        getattr(ws, "auto_pause_reason", "") or "quota spent")
                    self.record.park_until = (
                        m.quota_resume_at.isoformat(timespec="seconds")
                        if m.quota_resume_at else "")
                else:
                    self.record.park_reason = ""
                    self.record.park_until = ""
            self.record.heartbeat_at = datetime.now().isoformat(timespec="seconds")
            self.record.save()

    # ── run ──────────────────────────────────────────────────────────

    async def run(self) -> int:
        """Run to completion. Returns the process exit status.

        Wrapped so that *every* way out of here writes a reason, including
        the ones that arrive as exceptions. The only end this cannot
        record is the one where the process is not given a chance to run
        code — SIGKILL, or the power going — and `classify` names that
        VANISHED from the outside.
        """
        from auto_mode import AutoMode

        self.record.save()
        self.log(
            f"headless auto-mode starting: ws={self.ws_id[:8]} "
            f"({self.record.ws_name}) coord={self.coord_sid[:8]} pid={os.getpid()}"
        )

        self.mode = AutoMode(
            store=self.store,
            ws_id=self.ws_id,
            spawn_implementer=self.spawn_implementer,
            inject_coordinator=self.inject_coordinator,
            notify=self.note,
            skip_todo_ids=self.skip_todo_ids,
            coord_sid=self.coord_sid,
            max_nudges=self.max_nudges,
            max_silent_iterations=self.max_silent_iterations,
            poll_interval=self.poll_interval,
            check_quota=self.check_quota,
        )

        self._install_signal_handlers()
        stop = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(stop))
        try:
            reason = await self.mode.run()
            kind = self.mode.final_kind or ExitKind.DONE
            if kind == ExitKind.CANCELED and self.mode.cancel_source.startswith("SIG"):
                kind = ExitKind.SIGNALED
                reason = f"received {self.mode.cancel_source}"
            elif kind == ExitKind.CANCELED and self.mode.cancel_source == "flag":
                reason = "canceled via `orch auto cancel`"
            self.record.finish(kind, reason)
            self.log(f"loop ended [{kind}]: {reason}")
            return exit_status_for(kind)
        except asyncio.CancelledError:
            self.record.finish(ExitKind.SIGNALED, "host task canceled")
            self.log("loop ended [signaled]: host task canceled")
            raise
        except BaseException as e:
            detail = traceback.format_exc()
            self.record.finish(
                ExitKind.CRASHED, f"{type(e).__name__}: {e}", detail)
            self.log(f"loop CRASHED: {type(e).__name__}: {e}\n{detail}")
            return EXIT_CRASHED
        finally:
            stop.set()
            beat.cancel()
            try:
                await beat
            except (asyncio.CancelledError, Exception):
                pass

    def _install_signal_handlers(self) -> None:
        """Turn SIGTERM/SIGINT into a cancel that records which signal.

        `systemctl --user stop` and a hand `kill` both arrive this way,
        and both are legitimate ends — but "stopped by SIGTERM at 03:14"
        and "the coordinator declared it done" should not read the same
        in the morning.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, self._on_signal, signal.Signals(sig).name)
            except (NotImplementedError, ValueError, RuntimeError):
                pass  # no event loop signal support (Windows, nested loop)

    def _on_signal(self, name: str) -> None:
        self.log(f"received {name} — canceling loop")
        if self.mode is not None:
            self.mode.cancel(source=name)


# ── Entry points ─────────────────────────────────────────────────────

def resolve_coordinator(store, ws, coord_sid: str = "",
                        logger=None) -> tuple[str, bool]:
    """Pick the tmux session to drive as coordinator.

    Order: an explicit --coord-sid, then whatever the last run recorded
    if it is still alive, then a fresh one. Returns (sid, spawned_fresh).
    Raises RuntimeError if a fresh spawn fails — there is no loop worth
    starting without a coordinator to talk to.
    """
    from session_launch import spawn_coordinator_session
    from term_host import TerminalHost

    say = logger or (lambda _: None)
    if coord_sid:
        if not TerminalHost.tmux_session_alive(coord_sid):
            raise RuntimeError(
                f"coordinator session {coord_sid} is not alive on the orch "
                f"tmux socket")
        say(f"using given coordinator session {coord_sid[:8]}")
        return coord_sid, False

    prior = getattr(ws, "auto_coord_sid", "")
    if prior and TerminalHost.tmux_session_alive(prior):
        say(f"reusing coordinator session {prior[:8]} from the last run")
        return prior, False

    sid, _jsonl = spawn_coordinator_session(ws, store)
    say(f"spawned coordinator session {sid[:8]}; waiting for its input box")
    ready = wait_for_claude_ready(sid)
    say("coordinator ready" if ready else
        "coordinator did not show an input box in time — "
        "injecting anyway; the nudge loop re-sends if this one is lost")
    return sid, True


def run_headless(
    ws_id: str,
    coord_sid: str = "",
    skip_todo_ids: Optional[set] = None,
    max_nudges: Optional[int] = DEFAULT_MAX_NUDGES,
    max_silent_iterations: Optional[int] = DEFAULT_MAX_SILENT_ITERATIONS,
    store=None,
) -> int:
    """Take the lock, get a coordinator, run the loop. Returns exit status.

    This is what `orch auto start --foreground` and the systemd unit both
    call. Refusals and start-up failures get a record of their own —
    "there is no loop and here is why" is as much a thing you need to
    find in the morning as "the loop stopped and here is why".
    """
    from models import Store

    store = store or Store()
    ws = store.get(ws_id)
    if ws is None:
        rec = RunnerRecord(ws_id=ws_id, pid=os.getpid(),
                           host=socket.gethostname(),
                           started_at=datetime.now().isoformat(timespec="seconds"))
        rec.finish(ExitKind.NO_WORKSTREAM, f"no workstream with id {ws_id}")
        return EXIT_NO_START

    lock = InstanceLock(ws_id)
    if not lock.acquire():
        pid = holder_pid(ws_id)
        # Do NOT touch the record: it belongs to the loop that is running,
        # and overwriting its exit reason with our refusal is precisely
        # the kind of lie this module exists to prevent.
        try:
            with log_path(ws_id).open("a") as fh:
                fh.write(
                    f"{datetime.now():%Y-%m-%d %H:%M:%S}  refused to start: "
                    f"pid {pid} already holds the loop for {ws.name}\n")
        except Exception:
            pass
        return EXIT_REFUSED

    with lock:
        runner = HeadlessRunner(
            store=store,
            ws_id=ws_id,
            coord_sid="",
            skip_todo_ids=skip_todo_ids,
            max_nudges=max_nudges,
            max_silent_iterations=max_silent_iterations,
        )
        # Publish before resolving the coordinator, not after. Spawning one
        # and waiting for its input box can take the better part of a
        # minute, and `orch auto start` polls for this file to decide
        # whether the child it just launched is alive — an unpublished
        # record for 45 seconds reads as a failed start.
        runner.record.save()
        try:
            sid, _fresh = resolve_coordinator(
                store, ws, coord_sid, logger=runner.log)
        except Exception as e:
            runner.record.finish(
                ExitKind.SPAWN_FAILED, f"no coordinator session: {e}",
                traceback.format_exc())
            runner.log(f"refusing to run: {e}")
            return EXIT_NO_START
        runner.coord_sid = sid
        runner.record.coord_sid = sid
        return asyncio.run(runner.run())
