"""Tests for the headless auto-mode host.

The loop itself is covered by tests/test_auto_mode.py. What is tested
here is the thing a headless host adds: that every way a run can end
leaves a durable reason behind, that a quota park is not one of those
ends, and that a second host refuses to touch a workstream a first one
already owns.
"""

import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone

import pytest

import auto_runner
from auto_mode import ExitKind
from auto_runner import (
    EXIT_CRASHED,
    EXIT_GAVE_UP,
    EXIT_NO_START,
    EXIT_OK,
    EXIT_REFUSED,
    HeadlessRunner,
    InstanceLock,
    RunnerRecord,
    RunnerState,
    classify,
    exit_status_for,
    holder_pid,
    log_path,
    record_path,
    run_headless,
    state_dir,
)
from models import Store, TodoItem, Workstream


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep records, logs and locks out of the developer's real ~/.local."""
    monkeypatch.setenv("ORCH_AUTO_STATE_DIR", str(tmp_path / "auto"))


@pytest.fixture
def store(tmp_path):
    return Store(path=tmp_path / "data.json")


def _ws(store, todos=None):
    ws = Workstream(name="headless-ws")
    ws.todos = list(todos or [])
    store.add(ws)
    return ws


class FakeRunner(HeadlessRunner):
    """A HeadlessRunner with tmux and claude taken out.

    Everything above those two — the record, the log, the signal
    handling, the exit classification — is the real thing.
    """

    def __init__(self, *a, **kw):
        self.on_inject = kw.pop("on_inject", None)
        self.on_spawn = kw.pop("on_spawn", None)
        super().__init__(*a, **kw)
        self.injected: list[str] = []
        self.spawned: list[str] = []

    def inject_coordinator(self, text: str) -> None:
        self.injected.append(text)
        if self.on_inject:
            self.on_inject(self, text)

    async def spawn_implementer(self, todo, brief: str) -> None:
        self.spawned.append(todo.id)
        if self.on_spawn:
            self.on_spawn(self, todo, brief)


def _runner(store, ws, **kw):
    kw.setdefault("coord_sid", "coord-sid-0001")
    kw.setdefault("poll_interval", 0.01)
    return FakeRunner(store=store, ws_id=ws.id, **kw)


# ─── Every exit path records its reason ──────────────────────────────

class TestExitReasons:
    def test_done_records_the_coordinators_reason(self, store):
        ws = _ws(store)

        def on_inject(r, text):
            fresh = store.get(ws.id)
            fresh.auto_done_reason = "build order is shipped"
            store.update(fresh)

        r = _runner(store, ws, on_inject=on_inject)
        assert asyncio.run(r.run()) == EXIT_OK

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.DONE
        assert rec.exit_reason == "build order is shipped"
        assert rec.exit_at
        assert classify(rec) == (RunnerState.STOPPED, "build order is shipped")

    def test_cancel_flag_records_that_it_was_a_cancel(self, store, monkeypatch):
        """`orch auto cancel` sets a flag in another process. The headless
        loop must notice it and say so — this is the one stop button a
        user has on a loop with no UI."""
        monkeypatch.setattr("auto_mode.CANCEL_POLL_INTERVAL_S", 0.02)
        ws = _ws(store)

        def on_inject(r, text):
            fresh = store.get(ws.id)
            fresh.auto_cancel_requested = True   # what `orch auto cancel` writes
            store.update(fresh)

        r = _runner(store, ws, on_inject=on_inject)
        assert asyncio.run(r.run()) == EXIT_OK

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.CANCELED
        assert "orch auto cancel" in rec.exit_reason

    def test_signal_records_which_signal(self, store):
        ws = _ws(store)

        def on_inject(r, text):
            r._on_signal("SIGTERM")

        r = _runner(store, ws, on_inject=on_inject)
        assert asyncio.run(r.run()) == EXIT_OK

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.SIGNALED
        assert "SIGTERM" in rec.exit_reason

    def test_crash_records_the_traceback(self, store):
        ws = _ws(store)

        def on_inject(r, text):
            raise ZeroDivisionError("the coordinator's pane went away")

        r = _runner(store, ws, on_inject=on_inject)
        assert asyncio.run(r.run()) == EXIT_CRASHED

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.CRASHED
        assert "ZeroDivisionError" in rec.exit_reason
        # The traceback, not just the status — a crash you cannot locate
        # is barely better than a crash you never heard about.
        assert "Traceback" in rec.exit_detail
        assert "the coordinator's pane went away" in rec.exit_detail
        assert classify(rec)[0] == RunnerState.CRASHED
        assert "Traceback" in log_path(ws.id).read_text()

    def test_silent_coordinator_gives_up_and_says_so(self, store, monkeypatch):
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 0.01)
        ws = _ws(store)

        r = _runner(store, ws, max_nudges=2)   # never answers
        assert asyncio.run(r.run()) == EXIT_GAVE_UP

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.COORDINATOR_SILENT
        assert "2 nudges" in rec.exit_reason
        # It really did try before giving up: kickoff plus two nudges.
        assert len(r.injected) == 3

    def test_implementer_that_never_reports_ends_the_run(self, store):
        todo = TodoItem(text="a task", origin="crystallized", id="silent01")
        ws = _ws(store, [todo])

        def on_inject(r, text):
            if "[auto-mode started]" in text:
                fresh = store.get(ws.id)
                fresh.auto_next_todo_ids = ["silent01"]
                store.update(fresh)

        # on_spawn does nothing: the implementer dies without `orch report`.
        r = _runner(store, ws, on_inject=on_inject, max_silent_iterations=1)
        assert asyncio.run(r.run()) == EXIT_GAVE_UP

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.IMPLEMENTER_SILENT
        assert "no implementer writeback" in rec.exit_reason
        assert r.spawned == ["silent01"]

    def test_missing_workstream_is_recorded_not_raised(self, store):
        assert run_headless("nosuchws", store=store) == EXIT_NO_START
        rec = RunnerRecord.load("nosuchws")
        assert rec.exit_kind == ExitKind.NO_WORKSTREAM
        assert "nosuchws" in rec.exit_reason

    def test_no_coordinator_is_recorded_not_raised(self, store, monkeypatch):
        """There is no loop worth starting without a session to talk to,
        but 'there is no loop, and here is why' is as much a thing you
        need to find in the morning as a loop that stopped."""
        ws = _ws(store)

        def boom(*a, **kw):
            raise RuntimeError("tmux session cafe0000 is not alive")

        monkeypatch.setattr(auto_runner, "resolve_coordinator", boom)
        assert run_headless(ws.id, store=store) == EXIT_NO_START

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.SPAWN_FAILED
        assert "not alive" in rec.exit_reason
        assert "Traceback" in rec.exit_detail

    def test_every_kind_maps_to_a_process_status(self):
        # A systemd unit reasons about the loop through this number, so
        # an unmapped kind must not quietly become "success".
        for kind in (ExitKind.DONE, ExitKind.CANCELED, ExitKind.SIGNALED):
            assert exit_status_for(kind) == EXIT_OK
        for kind in (ExitKind.COORDINATOR_SILENT, ExitKind.IMPLEMENTER_SILENT):
            assert exit_status_for(kind) == EXIT_GAVE_UP
        for kind in (ExitKind.NO_WORKSTREAM, ExitKind.SPAWN_FAILED):
            assert exit_status_for(kind) == EXIT_NO_START
        assert exit_status_for(ExitKind.CRASHED) == EXIT_CRASHED
        assert exit_status_for("something-invented-later") == EXIT_CRASHED


# ─── A park is not an exit ───────────────────────────────────────────

class TestParkIsNotAnExit:
    def test_parked_record_reads_as_parked_not_stopped(self):
        rec = RunnerRecord(
            ws_id="parked01", pid=os.getpid(),
            host=auto_runner.socket.gethostname(),
            parked=True, park_reason="5h session at 100%",
            park_until="2026-08-30T14:00:00",
        )
        state, why = classify(rec)
        assert state == RunnerState.PARKED
        assert "asleep, not stopped" in why
        assert not rec.exit_kind   # nothing has ended

    def test_quota_park_holds_the_loop_and_records_no_exit(
            self, store, monkeypatch):
        """The gate parks and wakes itself. The host's only job is to
        still be here when it does — and to be describable as parked
        while it waits, because a park that reads as a stop is the
        report that sends someone to restart a loop that was fine."""
        from usage import QuotaLimit, QuotaSnapshot

        monkeypatch.setenv("ORCH_AUTO_QUOTA_PAUSE", "1")
        monkeypatch.setattr("auto_mode.QUOTA_MIN_SLEEP_S", 0.01)
        monkeypatch.setattr("auto_mode.QUOTA_MAX_SLEEP_S", 0.05)
        monkeypatch.setattr(auto_runner, "HEARTBEAT_S", 0.02)

        spent = {"now": True}
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        limit = QuotaLimit(kind="session", group="session", percent=100.0,
                           resets_at=future)
        full = QuotaSnapshot(limits=(limit,), fetched_at=datetime.now(timezone.utc))
        clear = QuotaSnapshot(
            limits=(QuotaLimit(kind="session", group="session", percent=12.0,
                               resets_at=future),),
            fetched_at=datetime.now(timezone.utc))

        ws = _ws(store)

        def on_inject(r, text):
            fresh = store.get(ws.id)
            fresh.auto_done_reason = "resumed and finished"
            store.update(fresh)

        r = _runner(store, ws, on_inject=on_inject,
                    check_quota=lambda: full if spent["now"] else clear)

        async def go():
            task = asyncio.create_task(r.run())
            # Wait for the park to be visible in the record — that is what
            # `orch auto status` reads.
            for _ in range(400):
                await asyncio.sleep(0.01)
                rec = RunnerRecord.load(ws.id)
                if rec is not None and rec.parked:
                    break
            rec = RunnerRecord.load(ws.id)
            assert rec is not None and rec.parked, "park never reached the record"
            assert classify(rec)[0] == RunnerState.PARKED
            assert not rec.exit_kind, "a park recorded itself as an exit"
            assert r.injected == [], "a parked loop typed at the coordinator"
            assert r.mode.quota_paused
            # Release the window; the same process must pick the loop back up.
            spent["now"] = False
            return await asyncio.wait_for(task, timeout=20)

        assert asyncio.run(go()) == EXIT_OK
        assert r.injected, "the loop never resumed after the reset"

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ExitKind.DONE
        assert rec.exit_reason == "resumed and finished"
        assert not rec.parked


# ─── Only one loop per workstream ────────────────────────────────────

class TestSingleInstance:
    def test_second_start_refuses(self, store):
        ws = _ws(store)
        first = InstanceLock(ws.id)
        assert first.acquire()
        try:
            assert holder_pid(ws.id) == os.getpid()
            assert run_headless(ws.id, store=store) == EXIT_REFUSED
        finally:
            first.release()
        # And once the first lets go, a start is allowed again.
        assert holder_pid(ws.id) == 0

    def test_refusal_does_not_overwrite_the_running_loops_record(self, store):
        """The record belongs to the loop that is running. Stamping a
        refusal onto it would replace 'still going' with an exit reason
        that never happened — the exact lie this module exists to stop."""
        ws = _ws(store)
        live = RunnerRecord(ws_id=ws.id, pid=os.getpid(),
                            host=auto_runner.socket.gethostname(),
                            iteration=4, last_note="iter 4: awaiting pick")
        live.save()

        lock = InstanceLock(ws.id)
        assert lock.acquire()
        try:
            assert run_headless(ws.id, store=store) == EXIT_REFUSED
        finally:
            lock.release()

        rec = RunnerRecord.load(ws.id)
        assert rec.exit_kind == ""
        assert rec.iteration == 4
        assert rec.last_note == "iter 4: awaiting pick"
        assert "refused to start" in log_path(ws.id).read_text()

    def test_lock_is_released_when_the_holder_dies(self, store, tmp_path):
        """flock, not a pid file: a crash must not wedge every later start,
        and crashes are precisely what this runner exists for."""
        import subprocess
        import sys

        ws = _ws(store)
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import auto_runner\n"
            "lock = auto_runner.InstanceLock(%r)\n"
            "assert lock.acquire()\n"
            "raise SystemExit(7)\n"
            % (str(auto_runner.Path(auto_runner.__file__).parent), ws.id)
        )
        env = dict(os.environ, ORCH_AUTO_STATE_DIR=str(state_dir()))
        p = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True)
        assert p.returncode == 7, p.stderr
        assert holder_pid(ws.id) == 0   # the kernel took it back


# ─── Classification of a run nobody got to close out ─────────────────

class TestClassify:
    def test_no_record_is_idle(self):
        assert classify(None)[0] == RunnerState.IDLE

    def test_unfinished_record_with_a_dead_pid_is_vanished(self):
        # The shape of SIGKILL, and of the machine going down mid-run.
        rec = RunnerRecord(ws_id="gone0001", pid=2, host=auto_runner.socket.gethostname(),
                           started_at="2026-08-29T23:00:00",
                           heartbeat_at="2026-08-30T02:14:00")
        # pid 2 is the kernel's kthreadd; stand in for "not our process".
        rec.pid = 999_999_999
        state, why = classify(rec)
        assert state == RunnerState.VANISHED
        assert "wrote no exit reason" in why
        assert "02:14" in why   # how far it got before it went

    def test_record_from_another_host_is_not_guessed_at(self):
        rec = RunnerRecord(ws_id="far00001", pid=1234, host="some-other-box")
        state, why = classify(rec)
        assert state == RunnerState.FOREIGN
        assert "some-other-box" in why

    def test_live_unfinished_record_is_running(self):
        rec = RunnerRecord(ws_id="live0001", pid=os.getpid(),
                           host=auto_runner.socket.gethostname(),
                           last_note="iter 2: awaiting coordinator pick")
        assert classify(rec) == (
            RunnerState.RUNNING, "iter 2: awaiting coordinator pick")

    def test_unreadable_record_reads_as_absent(self, tmp_path):
        state_dir().mkdir(parents=True, exist_ok=True)
        record_path("junk0001").write_text("{not json")
        assert RunnerRecord.load("junk0001") is None

    def test_record_survives_a_field_it_has_never_seen(self):
        """A record written by a later version must still read back — the
        whole value of this file is that it is still legible tomorrow."""
        state_dir().mkdir(parents=True, exist_ok=True)
        record_path("fwd00001").write_text(
            '{"ws_id": "fwd00001", "exit_kind": "done", '
            '"exit_reason": "fine", "invented_later": 42}')
        rec = RunnerRecord.load("fwd00001")
        assert rec.exit_kind == "done"
        assert classify(rec)[0] == RunnerState.STOPPED


# ─── The restart that handed one todo to two implementers ────────────

class TestRestartDoesNotRedispatch:
    """b82a302 fixed this for the TUI. A new host process is exactly the
    situation that regressed it, so the headless path gets its own test.
    """

    def test_a_todo_with_a_live_implementer_is_held(self, store, monkeypatch):
        taken = TodoItem(text="already being written", origin="crystallized",
                         id="taken001", impl_sid="live-impl-session")
        free = TodoItem(text="not started", origin="crystallized", id="free0001")
        ws = _ws(store, [taken, free])

        import term_host
        monkeypatch.setattr(term_host.TerminalHost, "list_tmux_sessions",
                            classmethod(lambda cls: ["live-impl-session"]))

        held_at_kickoff = []

        def on_inject(r, text):
            # The coordinator asks for BOTH — including the taken one.
            fresh = store.get(ws.id)
            if "[auto-mode started]" in text:
                held_at_kickoff.append(list(fresh.auto_dispatched_todo_ids))
                fresh.auto_next_todo_ids = ["taken001", "free0001"]
                store.update(fresh)
            else:
                fresh.auto_done_reason = "done"
                store.update(fresh)

        def on_spawn(r, todo, brief):
            fresh = store.get(ws.id)
            t = next(x for x in fresh.todos if x.id == todo.id)
            t.report = "did it"
            t.done = True
            store.update(fresh)

        r = _runner(store, ws, on_inject=on_inject, on_spawn=on_spawn)
        asyncio.run(r.run())

        # Only the free one was dispatched. A second implementer in the
        # same worktree on the same file is the failure being prevented.
        assert r.spawned == ["free0001"]
        # The hold was published to data.json while the loop ran, which is
        # what makes `orch distill next` refuse it in the coordinator's own
        # process rather than the loop silently dropping the pick and
        # leaving the CLI to print a false "✓ dispatched".
        assert held_at_kickoff == [["taken001"]]
        # And the taken todo was never offered in the kickoff listing.
        assert "taken001" not in r.injected[0]
        assert "free0001" in r.injected[0]

    def test_headless_spawn_stamps_the_todo_with_its_session(
            self, store, monkeypatch):
        """The stamp is the only signal that outlives this process. If the
        headless spawn skipped it, the hold above would have nothing to
        read after a restart."""
        import session_launch
        import term_host

        todo = TodoItem(text="t", origin="crystallized", id="stamp001")
        ws = _ws(store, [todo])

        monkeypatch.setattr(
            session_launch, "spawn_implementer_session",
            lambda w, s, p, cwd=None: ("spawned-sid", auto_runner.Path("/dev/null")))
        monkeypatch.setattr(session_launch, "log_session_exit",
                            lambda *a, **kw: None)
        monkeypatch.setattr(session_launch, "auto_link_session",
                            lambda *a, **kw: None)
        monkeypatch.setattr(term_host.TerminalHost, "tmux_session_alive",
                            classmethod(lambda cls, sid: False))

        runner = HeadlessRunner(store=store, ws_id=ws.id, coord_sid="c")

        class _Mode:
            cancel_event = asyncio.Event()
        runner.mode = _Mode()

        async def go():
            await runner.spawn_implementer(todo, "brief")

        asyncio.run(go())

        fresh = store.get(ws.id)
        assert fresh.todos[0].impl_sid == "spawned-sid"
        assert "spawned-sid" in fresh.auto_impl_sids


# ─── The log ─────────────────────────────────────────────────────────

class TestLog:
    def test_every_loop_notification_lands_in_the_log(self, store):
        ws = _ws(store)

        def on_inject(r, text):
            fresh = store.get(ws.id)
            fresh.auto_done_reason = "fine"
            store.update(fresh)

        r = _runner(store, ws)
        r.on_inject = on_inject
        asyncio.run(r.run())

        text = log_path(ws.id).read_text()
        assert "headless auto-mode starting" in text
        assert "awaiting coordinator pick" in text   # AutoMode's own notify
        assert "loop ended [done]: fine" in text
