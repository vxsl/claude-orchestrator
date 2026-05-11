"""Tests for the auto-mode loop runner and its CLI hooks."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from auto_mode import (
    AutoMode,
    NUDGE_INTERVAL_S,
    build_coordinator_followup,
    build_coordinator_kickoff,
    build_coordinator_nudge,
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

    def test_origin_does_not_filter(self):
        # Origin is informational only; both manual and crystallized are eligible.
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="a", origin="manual"),
            TodoItem(text="b", origin="crystallized"),
        ]
        # First un-done un-skipped wins, regardless of origin.
        assert find_next_todo(ws).text == "a"

    def test_skip_ids_work_for_any_origin(self):
        ws = Workstream(name="x")
        ws.todos = [
            TodoItem(text="manual", origin="manual", id="man00001"),
            TodoItem(text="crystal", origin="crystallized", id="cry00002"),
        ]
        # Skip the manual → only crystallized eligible
        assert find_next_todo(ws, {"man00001"}).text == "crystal"
        # Skip the crystallized → only manual eligible
        assert find_next_todo(ws, {"cry00002"}).text == "manual"

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

    def test_followup_is_imperative_not_conversational(self):
        # The followup must NOT use language that lets the coordinator
        # respond conversationally and 'stand by' instead of acting.
        todo = TodoItem(text="task", origin="crystallized")
        out = build_coordinator_followup(todo, "report")
        # Hard guards — pin the imperative phrasing
        assert "MUST" in out or "must" in out or "exactly ONE" in out or "Take" in out
        assert "Do NOT" in out or "do not" in out or "Do not" in out
        assert "stand by" in out.lower() or "conversational" in out.lower()

    def test_followup_lists_pending_todos_with_ids_and_next_command(self):
        # When pending todos exist, the followup must surface them with
        # IDs and tell the coordinator to use `orch distill next`.
        todo = TodoItem(text="just finished", origin="crystallized", id="aaa11111")
        pending = [
            TodoItem(text="queued one", origin="crystallized", id="bbb22222"),
            TodoItem(text="queued two", origin="manual", id="ccc33333"),
        ]
        out = build_coordinator_followup(todo, "report text", pending_todos=pending)
        assert "bbb22222" in out
        assert "ccc33333" in out
        assert "queued one" in out
        assert "queued two" in out
        assert "distill next" in out

    def test_followup_omits_next_command_when_no_pending(self):
        todo = TodoItem(text="t", origin="crystallized", id="aaa11111")
        out = build_coordinator_followup(todo, "r", pending_todos=[])
        # No pending todos → don't tell the coordinator to use `next`.
        assert "distill next" not in out

    def test_nudge_is_short_and_imperative(self):
        out = build_coordinator_nudge()
        assert "extract-orch-todo" in out or "crystallize" in out
        assert "distill done" in out
        # Should be much shorter than the full followup
        assert len(out) < 400

    def test_nudge_mentions_next_when_pending_exist(self):
        pending = [TodoItem(text="t", origin="crystallized", id="aaa11111")]
        out = build_coordinator_nudge(pending)
        assert "distill next" in out

    def test_kickoff_mentions_extract(self):
        ws = Workstream(name="my-ws")
        out = build_coordinator_kickoff(ws)
        assert "my-ws" in out
        assert "extract-orch-todo" in out or "crystallize" in out

    def test_kickoff_with_pending_lists_them_and_gates(self):
        ws = Workstream(name="busy-ws")
        pending = [
            TodoItem(text="alpha", origin="crystallized", id="aaa11111"),
            TodoItem(text="beta", origin="manual", id="bbb22222"),
            TodoItem(text="gamma", origin="crystallized", id="ccc33333"),
        ]
        out = build_coordinator_kickoff(ws, pending_todos=pending)
        assert "busy-ws" in out
        assert "3" in out
        # Each pending todo must appear with its id so the coordinator
        # can pick by id.
        assert "aaa11111" in out
        assert "bbb22222" in out
        assert "ccc33333" in out
        assert "alpha" in out
        assert "beta" in out
        assert "gamma" in out
        # Must explicitly tell the coordinator the loop won't auto-dispatch.
        assert "distill next" in out
        # And must NOT promise auto-flow.
        assert "run them sequentially" not in out

    def test_kickoff_singular_when_one_pending(self):
        ws = Workstream(name="x")
        pending = [TodoItem(text="solo", origin="crystallized", id="aaa11111")]
        out = build_coordinator_kickoff(ws, pending_todos=pending)
        # "1 pending todo" not "1 pending todos"
        assert "1 pending todo " in out


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
            # On kickoff, coordinator picks the pre-existing todo via `orch distill next`.
            if "[auto-mode started]" in text:
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "todo1aaa"
                store.update(fresh)
            # After the followup is sent, coordinator declares done.
            elif "Implementer for todo" in text:
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
        assert any("implementer report text" in t for t in injected)
        # Loop must have cleared the next-todo signal after consuming it.
        assert store.get(ws.id).auto_next_todo_id == ""

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

    def test_nudge_fires_when_coordinator_silent(self, store, monkeypatch):
        """If the coordinator stalls past the nudge interval (doesn't pick,
        crystallize, or declare done), the loop re-injects a short prompt
        asking it to act. Pre-existing pending todos do NOT auto-advance
        the loop — that's exactly what creates the silence here."""
        # Tiny interval so the test runs fast
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 0.05)

        # A pre-existing pending todo is gated; the coordinator must
        # explicitly act. That gives us the silence the nudge needs.
        ws = _ws_with_todos(store, [
            TodoItem(text="task", origin="crystallized", id="aaa11111"),
        ])

        async def spawn(_todo, _brief):
            raise AssertionError("should not spawn — coordinator never picked")

        injected = []

        def coord(text):
            injected.append(text)
            # Once we've seen a nudge, declare done so the test ends.
            if "Still waiting" in text:
                fresh = store.get(ws.id)
                if not fresh.auto_done_reason:
                    fresh.auto_done_reason = "stop after nudge"
                    store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn,
            inject_coordinator=coord,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "stop after nudge"
        assert any("Still waiting" in t for t in injected), \
            f"expected a nudge after silence; injected={[t[:60] for t in injected]}"

    def test_attempted_todo_not_respawned_in_same_run(self, store):
        """If an implementer doesn't report (e.g. user detaches, impl
        crashes), the todo stays un-done on disk. The loop must not
        respawn the same todo on the next iteration — even if the
        coordinator tries to pick it again via `orch distill next`,
        the skip set should drop the pick."""
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
        attempted_repeat = []

        def coord(text):
            injected.append(text)
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            # On kickoff: pick first.
            if "[auto-mode started]" in text:
                fresh.auto_next_todo_id = "aaa11111"
                store.update(fresh)
                return
            # On first followup: try to pick aaa11111 AGAIN — skip set
            # must drop this. Then pick bbb22222 on the same followup
            # so the loop has something to advance to.
            if "Implementer for todo 'will not report'" in text:
                fresh.auto_next_todo_id = "aaa11111"
                store.update(fresh)
                attempted_repeat.append(True)
                # Then pick the second one.
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "bbb22222"
                store.update(fresh)
                return
            # On second followup: terminate.
            if "Implementer for todo 'second todo'" in text:
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
        # Coordinator did try to re-pick the failed todo.
        assert attempted_repeat, "test setup failed to attempt a re-pick"
        # Skip set should contain both attempted ids.
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
            if "[auto-mode started]" in text:
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "abc12345"
                store.update(fresh)
            elif "Implementer for todo" in text:
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

    def test_report_read_retries_on_transient_load_failure(self, store, monkeypatch):
        """models.Store.load swallows JSONDecodeError and silently sets
        workstreams=[] when a concurrent writer (other agent's crystallize,
        another impl's report, the TUI's desc refresher) leaves data.json
        partially written. Without retries, the post-spawn read in
        AutoMode.run() falsely emits 'no writeback' even though the
        implementer's report is sitting on disk. This pins the retry."""
        ws = _ws_with_todos(store, [
            TodoItem(text="task one", origin="crystallized", id="abc12345"),
        ])

        async def spawn(_todo, _brief):
            # Implementer writes the report normally.
            fresh = store.get(ws.id)
            fresh.todos[0].report = "real implementer report"
            fresh.todos[0].done = True
            store.update(fresh)
            # Now arrange for the next two store.load() calls to behave
            # like a partial-JSON read (workstreams cleared, no exception).
            real_load = store.load
            fails_left = [2]

            def flaky_load(*a, **kw):
                if fails_left[0] > 0:
                    fails_left[0] -= 1
                    store.workstreams = []
                    return
                real_load(*a, **kw)

            monkeypatch.setattr(store, "load", flaky_load)

        injected = []

        def inject(text):
            injected.append(text)
            if "[auto-mode started]" in text:
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "abc12345"
                store.update(fresh)
            elif "Implementer for todo" in text:
                fresh = store.get(ws.id)
                if fresh is not None:
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
        followup = next(
            (t for t in injected if "Implementer for todo" in t), None,
        )
        assert followup is not None, f"no followup injected; saw: {injected}"
        assert "real implementer report" in followup, (
            f"loop dropped the report on transient load miss; followup={followup!r}"
        )
        assert "no writeback" not in followup

    def test_runs_any_origin_when_selected(self, store):
        # Origin is informational; loop runs whatever the coordinator picks.
        # Manual todos are dispatchable via `orch distill next` just like
        # crystallized ones.
        ws = _ws_with_todos(store, [
            TodoItem(text="manual one", origin="manual", id="man00001"),
            TodoItem(text="crystal", origin="crystallized", id="cry00002"),
        ])
        spawned_briefs = []

        async def spawn(_todo, brief):
            spawned_briefs.append(brief)
            import re
            m = re.search(r"orch report --todo-id (\S+)", brief)
            if m:
                tid = m.group(1)
                fresh = store.get(ws.id)
                for t in fresh.todos:
                    if t.id == tid:
                        t.done = True
                        t.report = "done"
                store.update(fresh)

        injected = []

        def inject(text):
            injected.append(text)
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            # Drive the loop: first pick the manual todo, then the crystallized one,
            # then terminate.
            n_spawns = len(spawned_briefs)
            if "[auto-mode started]" in text:
                fresh.auto_next_todo_id = "man00001"
            elif "Implementer for todo" in text and n_spawns == 1:
                fresh.auto_next_todo_id = "cry00002"
            elif "Implementer for todo" in text and n_spawns == 2:
                fresh.auto_done_reason = "stop"
            store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "stop"
        # Both ran in the order the coordinator picked them.
        assert len(spawned_briefs) == 2
        assert "manual one" in spawned_briefs[0]
        assert "crystal" in spawned_briefs[1]

    def test_pre_existing_todos_not_auto_dispatched_at_kickoff(self, store, monkeypatch):
        """A workstream with pending todos at auto-mode start must NOT
        auto-flow. The coordinator has to read the kickoff, read the
        pending list, and explicitly pick one (or crystallize, or done).

        Regression for the bug where the loop instantly kicked off the
        first un-done todo without giving the coordinator a chance to
        decide based on context."""
        # Tight nudge interval just to keep the test fast; we don't rely on it.
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 0.05)
        ws = _ws_with_todos(store, [
            TodoItem(text="pre-existing", origin="crystallized", id="pre00001"),
        ])

        spawn_calls = []

        async def spawn(_todo, _brief):
            spawn_calls.append(_todo.id)

        injected = []

        def inject(text):
            injected.append(text)
            # Coordinator never picks — declare done after the kickoff.
            # This proves the loop didn't auto-grab the pending todo.
            if "[auto-mode started]" in text:
                fresh = store.get(ws.id)
                fresh.auto_done_reason = "coordinator chose to abort"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "coordinator chose to abort"
        # Critically: no implementer was spawned even though a pending todo existed.
        assert spawn_calls == [], \
            f"loop auto-dispatched a pre-existing todo; spawned={spawn_calls}"
        # Kickoff should have surfaced the pending todo's ID for the coordinator.
        assert any("pre00001" in t and "[auto-mode started]" in t for t in injected)

    def test_pre_existing_todos_not_auto_dispatched_after_report(self, store, monkeypatch):
        """After an implementer reports, any OTHER pending todos that were
        already in the queue must NOT auto-advance the loop. The
        coordinator reads the report and decides."""
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 0.05)
        # Two todos: coordinator will pick the first; the second is
        # pre-existing and must NOT be auto-dispatched after the first
        # implementer reports.
        ws = _ws_with_todos(store, [
            TodoItem(text="first", origin="crystallized", id="aaa00001"),
            TodoItem(text="second", origin="crystallized", id="bbb00002"),
        ])

        spawn_calls = []

        async def spawn(_todo, _brief):
            spawn_calls.append(_todo.id)
            fresh = store.get(ws.id)
            for t in fresh.todos:
                if t.id == _todo.id:
                    t.done = True
                    t.report = f"reported for {_todo.id}"
            store.update(fresh)

        injected = []

        def inject(text):
            injected.append(text)
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            if "[auto-mode started]" in text:
                fresh.auto_next_todo_id = "aaa00001"
                store.update(fresh)
            elif "Implementer for todo 'first'" in text:
                # Coordinator decides not to run the second pre-existing one;
                # declares done. The loop must NOT auto-dispatch bbb00002.
                fresh.auto_done_reason = "decided to stop after reading report"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "decided to stop after reading report"
        # Only the picked todo ran; the pre-existing second todo did not.
        assert spawn_calls == ["aaa00001"], \
            f"loop auto-advanced to second pre-existing todo; spawned={spawn_calls}"
        # The followup must have surfaced the second todo as a pending option.
        followup = next((t for t in injected if "Implementer for todo 'first'" in t), "")
        assert "bbb00002" in followup
        assert "second" in followup

    def test_fresh_crystallization_advances_loop(self, store, monkeypatch):
        """A todo crystallized AFTER the loop starts waiting (id not in
        the snapshot) advances the loop without needing
        `orch distill next`. This is the 'coordinator decided to plan
        a new step' path."""
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 5.0)  # don't fire mid-test
        ws = _ws_with_todos(store, [])  # empty — first iteration purely depends on a fresh crystallize

        spawn_briefs = []

        async def spawn(_todo, brief):
            spawn_briefs.append(_todo.id)
            fresh = store.get(ws.id)
            for t in fresh.todos:
                if t.id == _todo.id:
                    t.done = True
                    t.report = "shipped"
            store.update(fresh)

        injected = []

        def inject(text):
            injected.append(text)
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            if "[auto-mode started]" in text:
                # Coordinator crystallizes a fresh todo — no pre-existing snapshot.
                fresh.todos.append(TodoItem(text="freshly planned", origin="crystallized", id="fre00001"))
                store.update(fresh)
            elif "Implementer for todo 'freshly planned'" in text:
                fresh.auto_done_reason = "complete"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "complete"
        assert spawn_briefs == ["fre00001"]

    def test_invalid_auto_next_todo_id_is_dropped(self, store, monkeypatch):
        """If the coordinator sets auto_next_todo_id to something that
        doesn't match a pending todo (already done, archived, in skip
        set, or just bogus), the loop drops the signal and keeps
        waiting. This prevents a stale id from blocking the loop or
        causing a stuck pick."""
        monkeypatch.setattr("auto_mode.NUDGE_INTERVAL_S", 0.05)
        ws = _ws_with_todos(store, [
            TodoItem(text="done already", origin="crystallized", id="don00001", done=True),
            TodoItem(text="legit", origin="crystallized", id="leg00002"),
        ])
        spawn_calls = []

        async def spawn(_todo, _brief):
            spawn_calls.append(_todo.id)
            fresh = store.get(ws.id)
            for t in fresh.todos:
                if t.id == _todo.id:
                    t.done = True
                    t.report = "ok"
            store.update(fresh)

        injected = []
        bad_pick_seen = []

        def inject(text):
            injected.append(text)
            fresh = store.get(ws.id)
            if fresh.auto_done_reason:
                return
            if "[auto-mode started]" in text:
                # First, set a bogus id — should be dropped.
                fresh.auto_next_todo_id = "nope9999"
                store.update(fresh)
            elif "Still waiting" in text and not bad_pick_seen:
                # After the bogus pick was dropped and we got nudged, try
                # the already-done one — also should be dropped.
                bad_pick_seen.append(True)
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "don00001"
                store.update(fresh)
            elif "Still waiting" in text and bad_pick_seen:
                # Now pick the legitimate one.
                fresh = store.get(ws.id)
                fresh.auto_next_todo_id = "leg00002"
                store.update(fresh)
            elif "Implementer for todo 'legit'" in text:
                fresh.auto_done_reason = "done"
                store.update(fresh)

        mode = AutoMode(
            store=store, ws_id=ws.id,
            spawn_implementer=spawn, inject_coordinator=inject,
            poll_interval=0.01,
        )
        result = asyncio.run(mode.run())
        assert result == "done"
        assert spawn_calls == ["leg00002"], \
            f"bad picks should have been dropped; spawned={spawn_calls}"
        # Both bogus picks should have been cleared from the workstream.
        assert store.get(ws.id).auto_next_todo_id == ""


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
            todo_id = None

        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason  # non-empty default

    def test_refuses_when_pending_todos_exist(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="done-ws")
        ws.todos = [
            TodoItem(text="leftover task", origin="crystallized", id="abc12345"),
        ]
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
            reason = "I think we're done"
            todo_id = None
            force = False

        with pytest.raises(SystemExit):
            cmd_distill(Args())

        # auto_done_reason must NOT be set
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason == ""

        # Output should mention the pending todo and the recovery action
        out = capsys.readouterr().out
        assert "abc12345" in out
        assert "distill next" in out
        assert "--force" in out

    def test_skips_archived_and_done_when_counting_pending(self, tmp_path, monkeypatch):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="done-ws")
        ws.todos = [
            TodoItem(text="finished", origin="crystallized", done=True, id="don00001"),
            TodoItem(text="abandoned", origin="crystallized", archived=True, id="arc00001"),
        ]
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
            reason = "complete"
            todo_id = None
            force = False

        # Should succeed — no pending todos remain
        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason == "complete"

    def test_force_overrides_pending_guard(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="done-ws")
        ws.todos = [
            TodoItem(text="leftover", origin="crystallized", id="abc12345"),
        ]
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
            reason = "really done"
            todo_id = None
            force = True

        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_done_reason == "really done"
        out = capsys.readouterr().out
        assert "pending" in out.lower()  # warning about forced termination


class TestCoordinatorPromptWording:
    """Prompts must steer the coordinator away from terminating with pending todos."""

    def test_kickoff_with_pending_warns_about_done_semantics(self):
        ws = Workstream(name="x")
        pending = [TodoItem(text="t", origin="crystallized", id="abc12345")]
        text = build_coordinator_kickoff(ws, pending_todos=pending)
        assert "HARD-KILL" in text
        assert "next loop" in text  # phrase warning that there is no "next loop"

    def test_followup_with_pending_warns_about_done_semantics(self):
        todo = TodoItem(text="ran", origin="crystallized", id="ran00001")
        pending = [TodoItem(text="t", origin="crystallized", id="abc12345")]
        text = build_coordinator_followup(todo, "did it", pending_todos=pending)
        assert "HARD-KILL" in text
        assert "next loop" in text  # phrase warning that there is no "next loop"

    def test_followup_without_pending_still_marks_done_as_hard_kill(self):
        todo = TodoItem(text="ran", origin="crystallized", id="ran00001")
        text = build_coordinator_followup(todo, "did it", pending_todos=[])
        assert "HARD-KILL" in text

    def test_nudge_with_pending_warns_about_done_semantics(self):
        pending = [TodoItem(text="t", origin="crystallized", id="abc12345")]
        text = build_coordinator_nudge(pending_todos=pending)
        assert "HARD-KILL" in text
        assert "next loop" in text  # phrase warning that there is no "next loop"


class TestDistillNextCLI:
    """`orch distill next --todo-id <id>` — coordinator dispatches an
    existing pending todo without re-crystallizing."""

    def _setup(self, tmp_path, monkeypatch, todos):
        target = tmp_path / "dev" / "claude-orchestrator" / "data.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path=target)
        ws = Workstream(name="next-ws")
        ws.todos = todos
        store.add(ws)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ORCH_WS_ID", ws.id)
        return target, ws

    def test_sets_auto_next_todo_id_for_pending(self, tmp_path, monkeypatch):
        target, ws = self._setup(tmp_path, monkeypatch, [
            TodoItem(text="task", origin="crystallized", id="abc12345"),
        ])

        from cli import cmd_distill

        class Args:
            distill_mode = "next"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None
            todo_id = "abc12345"

        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_next_todo_id == "abc12345"

    def test_accepts_id_prefix(self, tmp_path, monkeypatch):
        target, ws = self._setup(tmp_path, monkeypatch, [
            TodoItem(text="task", origin="crystallized", id="abc12345"),
        ])

        from cli import cmd_distill

        class Args:
            distill_mode = "next"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None
            todo_id = "abc123"  # prefix

        cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_next_todo_id == "abc12345"

    def test_rejects_missing_todo_id(self, tmp_path, monkeypatch):
        target, ws = self._setup(tmp_path, monkeypatch, [])

        from cli import cmd_distill

        class Args:
            distill_mode = "next"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None
            todo_id = None

        with pytest.raises(SystemExit):
            cmd_distill(Args())

    def test_rejects_unknown_todo(self, tmp_path, monkeypatch):
        target, ws = self._setup(tmp_path, monkeypatch, [
            TodoItem(text="task", origin="crystallized", id="abc12345"),
        ])

        from cli import cmd_distill

        class Args:
            distill_mode = "next"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None
            todo_id = "zzzz9999"

        with pytest.raises(SystemExit):
            cmd_distill(Args())
        # Field must remain unset on rejection.
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_next_todo_id == ""

    def test_rejects_done_todo(self, tmp_path, monkeypatch):
        target, ws = self._setup(tmp_path, monkeypatch, [
            TodoItem(text="finished", origin="crystallized", id="don00001", done=True),
        ])

        from cli import cmd_distill

        class Args:
            distill_mode = "next"
            ws_id = None
            text = None
            context = None
            summary = None
            reason = None
            todo_id = "don00001"

        with pytest.raises(SystemExit):
            cmd_distill(Args())
        s2 = Store(path=target)
        assert s2.workstreams[0].auto_next_todo_id == ""
