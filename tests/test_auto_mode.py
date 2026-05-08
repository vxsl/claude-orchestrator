"""Tests for the auto-mode loop runner and its CLI hooks."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from auto_mode import (
    AutoMode,
    build_coordinator_followup,
    build_coordinator_kickoff,
    build_implementer_brief,
    detect_quota_stall,
    find_next_todo,
)
from models import Store, TodoItem, Workstream


# ─── find_next_todo ──────────────────────────────────────────────────

class TestFindNextTodo:
    def test_skips_done(self):
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="a", origin="crystallized", done=True),
            TodoItem(text="b", origin="crystallized"),
        ]
        assert find_next_todo(ws).text == "b"

    def test_skips_archived(self):
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="a", origin="crystallized", archived=True),
            TodoItem(text="b", origin="crystallized"),
        ]
        assert find_next_todo(ws).text == "b"

    def test_skips_manual(self):
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="a", origin="manual"),
            TodoItem(text="b", origin="crystallized"),
        ]
        assert find_next_todo(ws).text == "b"

    def test_returns_none_when_empty(self):
        assert find_next_todo(Workstream(name="x")) is None

    def test_returns_none_when_all_done(self):
        ws = Workstream(name="x")
        ws.todos = [TodoItem(text="a", origin="crystallized", done=True)]
        assert find_next_todo(ws) is None

    def test_skip_ids_are_filtered(self):
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="a", origin="crystallized", id="aaa00001"),
            TodoItem(text="b", origin="crystallized", id="bbb00002"),
        ]
        # With no skip, picks first
        assert find_next_todo(ws).text == "a"
        # With skip on first, picks second
        assert find_next_todo(ws, {"aaa00001"}).text == "b"
        # With skip on both, returns None
        assert find_next_todo(ws, {"aaa00001", "bbb00002"}) is None


# ─── prompt builders ─────────────────────────────────────────────────

class TestPromptBuilders:
    def test_brief_includes_text_context_and_report_command(self):
        todo = TodoItem(id="abc12345", text="task one", context="ctx", origin="crystallized")
        brief = build_implementer_brief(todo)
        assert "task one" in brief
        assert "ctx" in brief
        assert "orch report --todo-id abc12345" in brief
        assert "/exit" in brief

    def test_brief_works_without_context(self):
        todo = TodoItem(id="abc12345", text="task one", origin="crystallized")
        brief = build_implementer_brief(todo)
        assert "task one" in brief
        assert "orch report --todo-id abc12345" in brief

    def test_followup_includes_report_and_done_hint(self):
        todo = TodoItem(text="task", origin="crystallized")
        out = build_coordinator_followup(todo, "I shipped it.")
        assert "I shipped it." in out
        assert "task" in out
        assert "distill done" in out

    def test_kickoff_mentions_extract(self):
        ws = Workstream(name="my-ws")
        out = build_coordinator_kickoff(ws)
        assert "my-ws" in out
        assert "extract-orch-todo" in out or "crystallize" in out

    def test_kickoff_with_pending_count(self):
        ws = Workstream(name="busy-ws")
        out = build_coordinator_kickoff(ws, pending_count=3)
        assert "busy-ws" in out
        assert "3" in out
        assert "queued" in out or "todos" in out

    def test_kickoff_singular_when_one_pending(self):
        ws = Workstream(name="x")
        out = build_coordinator_kickoff(ws, pending_count=1)
        # "1 crystallized todo" not "1 crystallized todos"
        assert "1 crystallized todo " in out


# ─── quota stall detection ───────────────────────────────────────────

class TestDetectQuotaStall:
    def test_empty_no_match(self):
        assert detect_quota_stall("") is False
        assert detect_quota_stall(None or "") is False

    def test_normal_content_no_match(self):
        assert detect_quota_stall("Working on the auth refactor. Tests pass.") is False

    def test_5_hour_limit_matches(self):
        pane = "Claude TUI ...\n5-hour limit reached.\n  a) wait until reset\n  b) ...\n"
        assert detect_quota_stall(pane) is True

    def test_usage_limit_matches(self):
        assert detect_quota_stall("Your usage limit reached.") is True
        assert detect_quota_stall("USAGE LIMIT REACHED — please wait.") is True

    def test_wait_until_reset_matches(self):
        assert detect_quota_stall(
            "Please wait until your limit resets at 14:00 UTC."
        ) is True

    def test_rate_limit_matches(self):
        assert detect_quota_stall("Error: rate limit reached") is True

    def test_partial_word_no_match(self):
        # "limit" alone shouldn't trigger — too generic
        assert detect_quota_stall("The limit on this list is 100 items") is False

    def test_case_insensitive(self):
        assert detect_quota_stall("USAGE LIMIT REACHED") is True
        assert detect_quota_stall("usage limit reached") is True
        assert detect_quota_stall("Usage Limit Reached") is True


# ─── AutoMode loop ──────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return Store(path=tmp_path / "data.json")


def _ws_with_todos(store, todos):
    ws = Workstream(name="auto-ws")
    ws.todos = todos
    store.add(ws)
    return ws


class TestAutoModeLoop:
    def test_exits_immediately_when_done_flag_set(self, store):
        ws = _ws_with_todos(store, [TodoItem(text="t", origin="crystallized")])
        ws.auto_done_reason = ""  # cleared at run start, then immediately re-set by us
        store.update(ws)

        injected = []
        spawn_calls = []

        async def spawn(_todo, _brief):
            spawn_calls.append(_brief)
            # If reached, simulate immediate dismiss
            return

        def inject(text):
            injected.append(text)
            # Simulate the coordinator running `orch distill done` after kickoff
            fresh = store.get(ws.id)
            fresh.auto_done_reason = "test-done"
            store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn,
            inject_coordinator=inject,
            poll_interval=0.01,
        )
        # Pre-mark the only todo done so the loop kickoffs and waits
        ws.todos[0].done = True
        store.update(ws)

        result = asyncio.run(mode.run())
        assert result == "test-done"
        assert spawn_calls == []  # never spawned an implementer

    def test_runs_one_iteration_then_done(self, store):
        ws = _ws_with_todos(store, [
            TodoItem(text="task one", context="ctx", origin="crystallized", id="todo1aaa"),
        ])
        spawn_briefs = []
        injected = []

        async def spawn(_todo, brief):
            spawn_briefs.append(brief)
            # Simulate the implementer running `orch report` before exiting
            fresh = store.get(ws.id)
            fresh.todos[0].report = "implementer report text"
            fresh.todos[0].done = True
            store.update(fresh)

        def inject(text):
            injected.append(text)
            # After the followup is sent, coordinator declares done
            if "Implementer for todo" in text:
                fresh = store.get(ws.id)
                fresh.auto_done_reason = "complete"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn,
            inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "complete"
        assert mode.iteration == 1
        assert len(spawn_briefs) == 1
        assert "task one" in spawn_briefs[0]
        assert "orch report --todo-id todo1aaa" in spawn_briefs[0]
        # First inject is the followup (kickoff skipped because todo was already present)
        assert any("implementer report text" in t for t in injected)

    def test_cancel_during_wait_exits_cleanly(self, store):
        ws = _ws_with_todos(store, [])  # no todos → loop will kickoff and wait
        injected = []

        async def spawn(_todo, _brief):
            raise AssertionError("should not spawn")

        def inject(text):
            injected.append(text)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn,
            inject_coordinator=inject,
            poll_interval=0.01,
        )

        async def cancel_after_a_tick():
            await asyncio.sleep(0.05)
            mode.cancel()

        async def runner():
            return await asyncio.gather(mode.run(), cancel_after_a_tick())

        results = asyncio.run(runner())
        assert results[0] == "canceled"

    def test_attempted_todo_not_respawned_in_same_run(self, store):
        """If an implementer doesn't report (e.g. user detaches, impl
        crashes), the todo stays un-done on disk. The loop should NOT
        respawn the same todo on the next iteration — that creates
        the duplicate-implementer-spam pathology."""
        ws = _ws_with_todos(store, [
            TodoItem(text="will not report", origin="crystallized", id="aaa11111"),
            TodoItem(text="second todo", origin="crystallized", id="bbb22222"),
        ])
        spawned_ids = []

        async def spawn_no_report(_todo, brief):
            # Extract todo id from the brief footer (mirrors what `orch report` would do)
            import re
            m = re.search(r"orch report --todo-id (\S+)", brief)
            if m:
                spawned_ids.append(m.group(1))
            # Do NOT write a report — simulating user detach or impl crash.

        injected = []

        def coord(text):
            injected.append(text)
            # After the second iteration's followup, declare done so loop exits.
            # Both todos un-done, neither reported — loop must move on after each.
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            # Once both are in skip_ids, declare done.
            if len(injected) >= 3:  # kickoff + followup #1 + followup #2
                fresh.auto_done_reason = "stop after both attempted"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn_no_report,
            inject_coordinator=coord,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        # Loop should run BOTH todos exactly once each — not respawn the first.
        assert len(spawned_ids) == 2, f"expected 2 spawns, got {spawned_ids}"
        assert spawned_ids[0] == "aaa11111"
        assert spawned_ids[1] == "bbb22222"
        # Skip set should contain both attempted ids
        assert "aaa11111" in mode.skip_todo_ids
        assert "bbb22222" in mode.skip_todo_ids

    def test_caller_decides_when_to_resolve_spawn(self, store):
        """The loop hands off to spawn_implementer; whatever the caller's
        contract is for resolution (screen dismiss vs report-written, or
        a race between them) is handled in the TUI wiring. The loop just
        awaits the awaitable. This test pins the contract: as long as
        spawn_implementer resolves and the report is on the todo, the
        loop reads it correctly even if the implementer is still
        notionally 'running'."""
        ws = _ws_with_todos(store, [
            TodoItem(text="task one", origin="crystallized", id="abc12345"),
        ])
        spawn_briefs = []

        async def spawn_resolves_on_report(_todo, brief):
            spawn_briefs.append(brief)
            # Simulate the TUI behavior: report arrives BEFORE the
            # implementer screen dismisses. spawn_implementer returns
            # at this point; the screen is still notionally up.
            fresh = store.get(ws.id)
            for t in fresh.todos:
                if t.id == "abc12345":
                    t.report = "report written but impl still running"
            store.update(fresh)
            # Return without setting any 'dismiss' signal — the loop should still advance.

        injected = []
        coord_done = []

        def inject(text):
            injected.append(text)
            if "Implementer for todo" in text:
                fresh = store.get(ws.id)
                fresh.auto_done_reason = "advanced on report"
                store.update(fresh)
                coord_done.append(True)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn_resolves_on_report,
            inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "advanced on report"
        assert mode.iteration == 1
        # The followup contained the report, even though impl never marked done itself.
        assert any("report written but impl still running" in t for t in injected)
        # Note: in real flow the orch report CLI sets done=True. Here we only set
        # report and not done — but the loop still proceeds because
        # find_next_todo would skip this todo on next iteration only if done=True.
        # Coordinator's distill done short-circuited before that mattered.

    def test_skips_manual_todos(self, store):
        ws = _ws_with_todos(store, [
            TodoItem(text="manual one", origin="manual"),
            TodoItem(text="real", origin="crystallized", id="real0001"),
        ])
        spawned = []

        async def spawn(_todo, brief):
            spawned.append(brief)
            fresh = store.get(ws.id)
            for t in fresh.todos:
                if t.id == "real0001":
                    t.done = True
                    t.report = "done"
            fresh.auto_done_reason = "stop"
            store.update(fresh)

        def inject(_):
            pass

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "stop"
        assert len(spawned) == 1
        assert "real" in spawned[0]
        assert "manual one" not in spawned[0]


# ─── CLI: orch report and orch distill done ─────────────────────────

ORCH_DIR = Path(__file__).parent.parent


def _run_cli(args, env_extra=None, cwd=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ORCH_DIR / "cli.py")] + args,
        capture_output=True, text=True, env=env, cwd=cwd or str(ORCH_DIR),
    )


class TestReportCLI:
    def test_writes_report_and_marks_done(self, tmp_path, monkeypatch):
        # Point Store at a temp data file
        data_path = tmp_path / "data.json"
        monkeypatch.setenv("HOME", str(tmp_path))  # default Store path is under HOME
        store = Store(path=data_path)
        ws = Workstream(name="rep-ws")
        ws.todos = [TodoItem(text="task", origin="crystallized", id="rep00001")]
        store.add(ws)

        # Patch Store.__init__ via env-free path: directly run cmd_report inline
        from cli import cmd_report

        class Args:
            todo_id = "rep00001"
            text = "all done"

        # cmd_report constructs its own Store() which reads from default HOME path.
        # We monkeypatched HOME above, so it lands in our tmp.
        # But Store default is HOME/dev/claude-orchestrator/data.json
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data_path.read_text())

        cmd_report(Args())

        # Reload to see the persisted change
        s2 = Store(path=target)
        cur = s2.workstreams[0].todos[0]
        assert cur.report == "all done"
        assert cur.done is True

    def test_missing_todo_exits_nonzero(self, tmp_path, monkeypatch):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"workstreams": []}\n')
        monkeypatch.setenv("HOME", str(tmp_path))

        from cli import cmd_report

        class Args:
            todo_id = "nope0000"
            text = "x"

        with pytest.raises(SystemExit):
            cmd_report(Args())


class TestDistillDoneCLI:
    def test_sets_auto_done_reason(self, tmp_path, monkeypatch):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="done-ws")
        store.add(ws)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ORCH_WS_ID", ws.id)

        from cli import cmd_distill

        class Args:
            distill_mode = "done"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = "we're complete"

        cmd_distill(Args())

        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason == "we're complete"

    def test_default_reason_when_none(self, tmp_path, monkeypatch):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="done-ws")
        store.add(ws)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ORCH_WS_ID", ws.id)

        from cli import cmd_distill

        class Args:
            distill_mode = "done"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None

        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason  # non-empty default
