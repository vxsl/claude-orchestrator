"""ClaudeSessionView tests (P5): layout, key routing (action keys vs raw
PTY passthrough), the ctrl+r jump overlay, detach/finish dismissal
contracts, session switching order, zoom, the header worker's
generation staleness, and OrchApp's launch/dismiss/tab-resume wiring.
Panes are FakePane (TerminalPane with the PTY lifecycle stubbed) — no
real claude, no tmux, no orch-sessions socket (tmux_session_alive is
patched in the wired fixture). HOME is redirected so spawn-args /
slash-command syncing / diag logs never touch real user state;
thread_namer AI calls are mocked (autouse).
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import thread_namer
import threads
from session_launch import claude_jsonl_path
from sessions import ClaudeSession
from term_host import TerminalHost
from tui.orch_app import OrchApp
from tui.termpane import TerminalPane
from tui.testing import Headless, make_key_event
from tui.views.claude_session import (
    _AUTO_MODE_KEYS, _PASSTHROUGH_KEYS, ClaudeSessionView, WsSessionList,
)

AUTO_KEY = _AUTO_MODE_KEYS.split(",")[0]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(threads, "LAST_SEEN_FILE", tmp_path / "last-seen.json")
    monkeypatch.setattr(thread_namer, "get_session_title", lambda s: "")
    monkeypatch.setattr(thread_namer, "title_sessions", lambda sessions: {})


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


SID = "cafe0001-0000-0000-0000-000000000001"
SID_B = "cafe0002-0000-0000-0000-000000000002"
SID_C = "cafe0003-0000-0000-0000-000000000003"


def make_session(sid: str, **kw) -> ClaudeSession:
    defaults = dict(
        session_id=sid,
        project_dir="-home-u-dev-proj",
        project_path="/home/u/dev/proj",
        title=f"Session {sid[:8]}",
        started_at=_iso(90),
        last_activity=_iso(1),
        message_count=4,
        assistant_message_count=2,
        model="claude-sonnet-4",
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


def thinking_session(sid: str) -> ClaudeSession:
    ts = _iso(1)
    return make_session(sid, is_live=True, last_message_role="user",
                        turn_complete=False, last_user_message_at=ts,
                        last_human_turn_at=ts, last_activity=ts,
                        last_assistant_message_text="working on it")


def awaiting_session(sid: str) -> ClaudeSession:
    return make_session(sid, is_live=True, turn_complete=True,
                        last_message_role="assistant",
                        last_assistant_message_text="done, your turn")


class FakePane(TerminalPane):
    """TerminalPane with the PTY/tmux lifecycle stubbed out. Input routing
    (passthrough set, \\x08-as-ctrl+h, raw-bytes fidelity) is the real
    handle_key; writes/lifecycle land in inspectable lists."""

    def __init__(self, command="bash", *, env=None, cwd=None,
                 passthrough_keys=None, log=None):
        super().__init__(command, env=env, cwd=cwd,
                         passthrough_keys=passthrough_keys)
        self._pid = 4242  # "alive" so handle_key routes to the PTY
        self.writes: list[bytes] = []
        self.lifecycle: list = [] if log is None else log
        self.searches: list[str] = []

    def start(self):
        self.lifecycle.append((self._command, "start"))

    def start_persistent(self, name):
        self._persistent_session = name
        self.lifecycle.append((self._command, "start_persistent", name))

    def attach_persistent(self, name):
        self._persistent_session = name
        self.lifecycle.append((self._command, "attach_persistent", name))

    def detach_persistent(self):
        self.lifecycle.append((self._command, "detach_persistent"))

    def stop(self):
        self.lifecycle.append((self._command, "stop"))

    def _write_to_pty(self, data):
        self.writes.append(data.encode())

    def _write_bytes(self, data):
        self.writes.append(data)

    def search_backward(self, pattern):
        self.searches.append(pattern)
        return True

    def _tmux_copy_mode_nav(self, action=None):
        self.lifecycle.append((self._command, "copy_mode", action))


@pytest.fixture
def cs_app(populated_store, monkeypatch, tmp_path):
    """OrchApp + fake-pane factory + a ws with two live sibling sessions."""
    app = OrchApp(store_path=populated_store.path, pollers=False)
    ws = app.state.store.active[0]
    current = awaiting_session(SID)
    sib_thinking = thinking_session(SID_B)
    sib_awaiting = awaiting_session(SID_C)
    sessions = [current, sib_thinking, sib_awaiting]
    app.state.sessions = sessions
    app.state.sessions_loaded = True
    app.state.sessions_for_ws = (
        lambda w, include_archived_sessions=False: list(sessions))
    app.state.notifications_for_ws = lambda w: []
    lifecycle: list = []
    monkeypatch.setattr(
        ClaudeSessionView, "_make_pane",
        staticmethod(lambda command, *, env, cwd: FakePane(
            command, env=env, cwd=cwd, passthrough_keys=_PASSTHROUGH_KEYS,
            log=lifecycle)),
    )
    launched: list = []
    app.launch_claude_session = (
        lambda w, **kw: lifecycle.append(("launch", kw.get("session_id"))) or
        launched.append((w, kw)))
    app.launched = launched
    app.lifecycle = lifecycle
    app._ws = ws
    app._cwd = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    return app


def make_view(app, session_id=SID, **kw):
    return ClaudeSessionView(app.state, app.tabs, app._ws,
                             session_id=session_id, cwd=app._cwd, **kw)


async def push_view(h, view=None, **kw):
    view = view or make_view(h.app, **kw)
    results = []
    h.app.push(view, on_result=results.append)
    await h.pause()
    return view, results


# ─── layout ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLayout:
    async def test_header_footer_sidebar_render(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            text = h.screen_text()
            assert cs_app._ws.name in text          # header line 2
            assert "ORCH" in text
            assert SID[:8] in text                  # header + footer
            assert "C-e" in text and "extract" in text   # footer hints
            assert f"claude --resume {SID}" in text      # footer right side
            assert "Workstreams" in text            # tab bar
            # sidebar sessions block lists the siblings
            assert "you" in text                    # current-session chip

    async def test_new_session_footer_flag(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h, session_id=None)
            assert f"claude --session-id {view.session_id}" in h.screen_text()

    async def test_panes_start_and_register(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            started = [e for e in cs_app.lifecycle if e[1].startswith("start")]
            assert (view._claude_command, "start_persistent", SID) in started
            assert ("tig status", "start") in started
            assert ("tig", "start") in started
            assert view.claude_pane in h.app._panes  # 20fps ticker contract
            # panes were sized before spawn (not the 80x24 default)
            assert view.claude_pane._ncol > 80

    async def test_reattach_uses_attach_persistent(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h, reattach_tmux=True)
            assert (view._claude_command, "attach_persistent", SID) in cs_app.lifecycle

    async def test_orch_no_sidebar(self, cs_app, monkeypatch):
        monkeypatch.setenv("ORCH_NO_SIDEBAR", "1")
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            assert view.tig_status is None and view.tig_log is None
            assert view._panel_ids() == ["claude"]
            assert "tig" not in [e[0] for e in cs_app.lifecycle if e[1] == "start"]


# ─── key routing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestKeyRouting:
    async def test_ctrl_e_writes_slash_command(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.press("ctrl+e")
            assert b"/user:extract-orch-todo\r" in view.claude_pane.writes

    async def test_plain_keys_forward_raw_bytes(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.feed_bytes(b"hi")
            assert view.claude_pane.writes[-2:] == [b"h", b"i"]
            await h.feed_bytes(b"\x1b[A")  # arrow key: raw bytes verbatim
            assert view.claude_pane.writes[-1] == b"\x1b[A"

    async def test_physical_backspace_forwards(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            await h.feed_bytes(b"\x7f")
            assert view.claude_pane.writes[-1] == b"\x7f"
            assert not results  # did NOT detach

    async def test_ctrl_h_via_x08_detaches(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            await h.feed_bytes(b"\x08")
            assert results and results[0].get("detached") is True

    async def test_detach_dict_shape(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            await h.press("ctrl+h")
            (result,) = results
            # the exact contract app.py:2437's consumer relies on
            assert set(result) == {"detached", "session_id", "ws",
                                   "start_time", "jsonl"}
            assert result["session_id"] == SID
            assert result["ws"] is cs_app._ws
            assert result["jsonl"] == view._jsonl
            assert isinstance(result["start_time"], float)
            # claude detached (still running in tmux), tig children stopped
            assert (view._claude_command, "detach_persistent") in cs_app.lifecycle
            assert ("tig", "stop") in cs_app.lifecycle
            assert ("tig status", "stop") in cs_app.lifecycle
            assert view.claude_pane not in h.app._panes

    async def test_ctrl_backslash_detaches(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            _, results = await push_view(h)
            await h.press("ctrl+backslash")
            assert results and results[0]["detached"] is True

    async def test_ctrl_space_archives_and_detaches(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            _, results = await push_view(h)
            await h.feed_bytes(b"\x00")  # ctrl+space → ctrl+@
            assert results and results[0]["detached"] is True
            ws = cs_app.state.store.get(cs_app._ws.id)
            assert SID in ws.archived_sessions

    async def test_ctrl_j_cycles_panels(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            assert view._active_panel == "claude"
            await h.press("ctrl+j")
            assert view._active_panel == "tig_status"
            await h.press("ctrl+j")
            assert view._active_panel == "tig_log"
            await h.press("ctrl+j")  # sidebar list has rows → 4th panel
            assert view._active_panel == "sessions"
            await h.press("ctrl+j")
            assert view._active_panel == "claude"
            await h.press("ctrl+k")
            assert view._active_panel == "sessions"

    async def test_keys_route_to_focused_tig_pane(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.press("ctrl+j")  # focus tig status
            await h.feed_bytes(b"j")
            assert b"j" in view.tig_status.writes
            assert b"j" not in view.claude_pane.writes

    async def test_tab_keys_fall_through(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            for key in ("ctrl+b", "ctrl+x"):
                assert view.on_key(make_key_event(key)) is False
                assert not view.claude_pane.writes

    async def test_auto_key_delegates_to_app(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            calls = []
            cs_app.toggle_auto_mode = lambda ws_id, sid: calls.append((ws_id, sid))
            await h.press(AUTO_KEY)
            assert calls == [(cs_app._ws.id, SID)]

    async def test_ctrl_y_is_not_bound(self, cs_app):
        """ctrl+y used to be auto mode; it now reaches claude as a yank."""
        assert "ctrl+y" not in _PASSTHROUGH_KEYS
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            calls = []
            cs_app.toggle_auto_mode = lambda ws_id, sid: calls.append((ws_id, sid))
            await h.press("ctrl+y")
            assert calls == []


# ─── ctrl+r jump overlay ─────────────────────────────────────────────


def write_prompts_jsonl(view, tmp_path, texts):
    path = tmp_path / "prompts.jsonl"
    lines = [json.dumps({
        "type": "user",
        "message": {"role": "user", "content": t},
        "timestamp": f"2026-07-08T10:{i:02d}:00Z",
    }) for i, t in enumerate(texts)]
    path.write_text("\n".join(lines) + "\n")
    view._jsonl = str(path)
    return path


@pytest.mark.asyncio
class TestJumpOverlay:
    async def test_overlay_lists_and_searches(self, cs_app, tmp_path):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            write_prompts_jsonl(view, tmp_path,
                                ["fix the login bug", "now refactor the parser"])
            await h.press("ctrl+r")
            assert view._picker_active
            text = h.screen_text()
            assert "fix the login bug" in text
            assert "refactor the parser" in text
            # newest message highlighted by default; enter searches for it
            await h.press("enter")
            assert not view._picker_active
            assert view.claude_pane.searches == ["now refactor the parser"]

    async def test_overlay_filters(self, cs_app, tmp_path):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            write_prompts_jsonl(view, tmp_path,
                                ["fix the login bug", "now refactor the parser"])
            await h.press("ctrl+r")
            await h.press("l", "o", "g", "i", "n")
            assert len(view.picker.list.rows) == 1
            await h.press("enter")
            assert view.claude_pane.searches == ["fix the login bug"]

    async def test_clearing_filter_rehighlights_newest(self, cs_app, tmp_path):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            write_prompts_jsonl(view, tmp_path, ["alpha one", "beta two"])
            await h.press("ctrl+r")
            await h.press("a", "l")
            await h.press("backspace", "backspace")
            assert view.picker.list.highlighted == len(view.picker.list.rows) - 1

    async def test_escape_closes_without_search(self, cs_app, tmp_path):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            write_prompts_jsonl(view, tmp_path, ["alpha"])
            await h.press("ctrl+r")
            await h.press("escape")
            assert not view._picker_active
            assert view.claude_pane.searches == []
            # keys route to the PTY again
            await h.feed_bytes(b"x")
            assert view.claude_pane.writes[-1] == b"x"

    async def test_overlay_swallows_action_keys(self, cs_app, tmp_path):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            write_prompts_jsonl(view, tmp_path, ["alpha"])
            await h.press("ctrl+r")
            await h.press("ctrl+h")  # must not detach while the overlay is up
            assert not results
            assert view._picker_active


# ─── finish / teardown ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestFinish:
    async def test_natural_exit_dismisses_with_parsed_session(
            self, cs_app, tmp_path, monkeypatch):
        parsed = make_session(SID)
        import tui.views.claude_session as mod
        monkeypatch.setattr(mod, "parse_session", lambda jp: parsed)
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            (tmp_path / "done.jsonl").write_text("{}\n")
            view._jsonl = str(tmp_path / "done.jsonl")
            view.claude_pane.on_finished()  # PTY EOF fires this
            await h.pause()
            assert results == [parsed]
            assert (view._claude_command, "stop") in cs_app.lifecycle
            assert ("tig", "stop") in cs_app.lifecycle
            # auto-linked (ws has no dir links in the fixture store)
            ws = cs_app.state.store.get(cs_app._ws.id)
            assert any(l.kind == "claude-session" and l.value == SID
                       for l in ws.links)

    async def test_emergency_close_is_idempotent(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            view.emergency_close()
            view.emergency_close()
            detaches = [e for e in cs_app.lifecycle
                        if e == (view._claude_command, "detach_persistent")]
            assert len(detaches) == 1
            assert ("tig", "stop") in cs_app.lifecycle
            assert not results  # no dismissal — the app is exiting

    async def test_finished_after_detach_is_ignored(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            await h.press("ctrl+h")
            view.claude_pane.on_finished()
            await h.pause()
            assert len(results) == 1  # only the detached dict


# ─── session switching ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestSwitching:
    async def test_ctrl_shift_j_detaches_then_launches(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            await h.press("ctrl+shift+j")
            assert results and results[0]["detached"] is True
            # order: detach current BEFORE launching the sibling
            log = cs_app.lifecycle
            detach_i = log.index((view._claude_command, "detach_persistent"))
            launch_i = next(i for i, e in enumerate(log) if e[0] == "launch")
            assert detach_i < launch_i
            (_, kw), = cs_app.launched
            assert kw["session_id"] in (SID_B, SID_C)

    async def test_sidebar_enter_switches_to_selected(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            while view._active_panel != "sessions":
                await h.press("ctrl+j")
            # move off the current session if it's selected
            for _ in range(4):
                if view.sessions_list.selected_sid != SID:
                    break
                await h.press("j")
            target = view.sessions_list.selected_sid
            await h.press("enter")
            assert results and results[0]["detached"] is True
            (_, kw), = cs_app.launched
            assert kw["session_id"] == target

    async def test_switch_to_current_session_is_noop(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, results = await push_view(h)
            view._switch_to_session(SID)
            assert not results and not cs_app.launched


# ─── zoom ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestZoom:
    async def test_ctrl_z_toggles_sidebar_off(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.press("ctrl+z")
            assert view._zoomed_panel == "claude"
            lay = view._layout(view._rect)
            assert lay["tig_status"] is None and lay["tig_log"] is None
            assert lay["claude"].w == view._rect.w  # full width
            await h.press("ctrl+z")
            assert view._zoomed_panel is None
            lay = view._layout(view._rect)
            assert lay["tig_status"] is not None

    async def test_zoomed_sidebar_panel_takes_body(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.press("ctrl+j")  # tig_status
            await h.press("ctrl+z")
            lay = view._layout(view._rect)
            assert lay["claude"] is None and lay["header"] is None
            assert lay["tig_status"].w == view._rect.w


# ─── header worker ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHeaderWorker:
    async def test_fresh_result_applies(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            view.header.lines = ["sentinel"]
            await view._header_runner(h.app.gen(view._header_group))
            assert view.header.lines != ["sentinel"]

    async def test_stale_result_dropped(self, cs_app):
        async with Headless(cs_app, size=(140, 40)) as h:
            view, _ = await push_view(h)
            await h.pause(0.01)  # let the on_show refresh land
            view.header.lines = ["sentinel"]
            stale = h.app.gen(view._header_group) - 1
            await view._header_runner(stale)
            assert view.header.lines == ["sentinel"]

    async def test_header_reads_jsonl_stats(self, cs_app, tmp_path):
        view = make_view(cs_app)
        parsed = make_session(SID, total_input_tokens=1000,
                              total_output_tokens=500,
                              last_user_messages=["do the thing"])
        import tui.views.claude_session as mod
        orig = mod.parse_session
        try:
            mod.parse_session = lambda jp: parsed
            (tmp_path / "x.jsonl").write_text("{}\n")
            view.header._jsonl_path = str(tmp_path / "x.jsonl")
            lines = view.header.refresh_blocking()
        finally:
            mod.parse_session = orig
        joined = "\n".join(lines)
        assert "4↑2↓" in joined            # msg counts
        assert "you said:" in joined       # last-user-message lines
        assert "do the thing" in joined


# ─── sidebar list unit ───────────────────────────────────────────────


class TestWsSessionList:
    def _state(self, cs_app):
        return cs_app.state

    def test_refresh_orders_thinking_first(self, cs_app):
        lst = WsSessionList(cs_app._ws.id, SID)
        assert lst.refresh(cs_app.state) is True
        sids = [r[0] for r in lst.rows]
        assert sids[0] == SID_B  # THINKING outranks AWAITING_INPUT
        assert SID in sids       # current session always shown

    def test_selection_stable_across_refresh(self, cs_app):
        lst = WsSessionList(cs_app._ws.id, SID)
        lst.refresh(cs_app.state)
        lst.handle_key(make_key_event("j"))
        picked = lst.selected_sid
        assert lst.refresh(cs_app.state) is False  # unchanged data
        assert lst.selected_sid == picked

    def test_cycle_target_wraps(self, cs_app):
        lst = WsSessionList(cs_app._ws.id, SID)
        lst.refresh(cs_app.state)
        sids = [r[0] for r in lst.rows]
        i = sids.index(SID)
        assert lst.cycle_target(SID, 1) == sids[(i + 1) % len(sids)]
        assert lst.cycle_target(SID, -1) == sids[(i - 1) % len(sids)]
        assert lst.cycle_target("missing", 1) == sids[0]

    def test_items_changed_fires_on_empty_transition(self, cs_app):
        lst = WsSessionList(cs_app._ws.id, SID)
        events = []
        lst.on_items_changed = events.append
        lst.refresh(cs_app.state)
        assert events == [True]
        cs_app.state.sessions_for_ws = (
            lambda w, include_archived_sessions=False: [])
        lst.refresh(cs_app.state)
        assert events == [True, False]

    def test_render_marks_current_as_you(self, cs_app):
        lst = WsSessionList(cs_app._ws.id, SID)
        lst.refresh(cs_app.state)
        joined = "\n".join(lst.render_lines(32, focused=False))
        assert "you" in joined


# ─── OrchApp wiring (launch / dismiss / tabs / exit) ─────────────────


@pytest.fixture
def wired_app(populated_store, monkeypatch, tmp_path):
    """OrchApp with the REAL launch/dismiss machinery: fake panes, and
    tmux_session_alive patched to a controllable flag (app.alive)."""
    app = OrchApp(store_path=populated_store.path, pollers=False)
    ws = app.state.store.active[0]
    current = awaiting_session(SID)
    sessions = [current]
    app.state.sessions = sessions
    app.state.sessions_loaded = True
    app.state.sessions_for_ws = (
        lambda w, include_archived_sessions=False: list(sessions))
    app.state.notifications_for_ws = lambda w: []
    lifecycle: list = []
    monkeypatch.setattr(
        ClaudeSessionView, "_make_pane",
        staticmethod(lambda command, *, env, cwd: FakePane(
            command, env=env, cwd=cwd, passthrough_keys=_PASSTHROUGH_KEYS,
            log=lifecycle)),
    )
    alive = {"value": False}
    monkeypatch.setattr(TerminalHost, "tmux_session_alive",
                        classmethod(lambda cls, name: alive["value"]))
    app.alive = alive
    app.lifecycle = lifecycle
    app._ws = ws
    proj = tmp_path / "proj"
    proj.mkdir()
    app._cwd = str(proj)
    return app


async def wait_for_session_view(h, timeout: float = 2.0) -> ClaudeSessionView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if isinstance(h.app.top, ClaudeSessionView):
            await h.pause()
            return h.app.top
        await asyncio.sleep(0.01)
    raise AssertionError("ClaudeSessionView never appeared")


@pytest.mark.asyncio
class TestOrchAppLaunch:
    async def test_launch_resume_reattaches_when_tmux_alive(self, wired_app):
        wired_app.alive["value"] = True
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(wired_app._ws, session_id=SID,
                                            cwd=wired_app._cwd)
            view = await wait_for_session_view(h)
            assert view.session_id == SID and not view._is_new
            assert (view._claude_command, "attach_persistent", SID) in wired_app.lifecycle

    async def test_launch_dead_session_resumes_fresh_tmux(self, wired_app):
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(wired_app._ws, session_id=SID,
                                            cwd=wired_app._cwd)
            view = await wait_for_session_view(h)
            assert (view._claude_command, "start_persistent", SID) in wired_app.lifecycle
            assert "--resume" in view._claude_command

    async def test_reuse_pending_only_when_jsonl_exists(self, wired_app):
        ws = wired_app._ws
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, cwd=wired_app._cwd)
            view1 = await wait_for_session_view(h)
            sid1 = view1.session_id
            assert view1._is_new
            assert wired_app._ws_pending_session[ws.id] == sid1
            await h.press("ctrl+h")
            # pending has no JSONL → dropped; a fresh id is spawned
            wired_app.launch_claude_session(ws, cwd=wired_app._cwd)
            view2 = await wait_for_session_view(h)
            assert view2.session_id != sid1 and view2._is_new
            await h.press("ctrl+h")
            # JSONL exists now → pending adopted as a resume
            jp = claude_jsonl_path(wired_app._cwd, view2.session_id)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text("{}\n")
            wired_app.launch_claude_session(ws, cwd=wired_app._cwd)
            view3 = await wait_for_session_view(h)
            assert view3.session_id == view2.session_id
            assert not view3._is_new

    async def test_detach_stashes_and_injects_live_session(
            self, wired_app, monkeypatch):
        ws = wired_app._ws
        injected = make_session(SID, message_count=3)
        import tui.orch_app as oa
        monkeypatch.setattr(oa, "parse_session", lambda p: injected)
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            view = await wait_for_session_view(h)
            highlights = []
            wired_app._detail_cache[ws.id] = type(
                "Spy", (), {"request_session_highlight":
                            lambda self, sid: highlights.append(sid),
                            "cancel_timers": lambda self: None})()
            await h.press("ctrl+h")
            stash = wired_app._detached_sessions[SID]
            assert stash["ws"] is ws and stash["jsonl"] == view._jsonl
            assert injected.is_live is True          # marked live on inject
            assert injected in wired_app.state.sessions
            assert highlights == [SID]               # detail view highlight
            wsx = wired_app.state.store.get(ws.id)   # auto-linked
            assert any(l.kind == "claude-session" and l.value == SID
                       for l in wsx.links)

    async def test_natural_finish_notifies_and_injects(self, wired_app, monkeypatch):
        parsed = make_session(SID)
        import tui.views.claude_session as mod
        monkeypatch.setattr(mod, "parse_session", lambda p: parsed)
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(wired_app._ws, session_id=SID,
                                            cwd=wired_app._cwd)
            view = await wait_for_session_view(h)
            jp = claude_jsonl_path(wired_app._cwd, SID)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text("{}\n")
            view._jsonl = str(jp)
            view.claude_pane.on_finished()
            await h.pause()
            assert "msgs" in wired_app.toast_text
            assert parsed in wired_app.state.sessions

    async def test_exit_with_live_session_detaches_no_deadlock(self, wired_app):
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(wired_app._ws, session_id=SID,
                                            cwd=wired_app._cwd)
            view = await wait_for_session_view(h)
        # Headless __aexit__ ran app.exit() + awaited _main under a timeout:
        # returning at all proves no deadlock; the children were shut down.
        assert (view._claude_command, "detach_persistent") in wired_app.lifecycle
        assert ("tig", "stop") in wired_app.lifecycle
        assert ("tig status", "stop") in wired_app.lifecycle


@pytest.mark.asyncio
class TestTabSwitchResume:
    async def test_tab_switch_detaches_and_auto_resumes(self, wired_app):
        ws = wired_app._ws
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.open_detail(ws)
            await h.pause()
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            first = await wait_for_session_view(h)
            # ws tab is last → next wraps to home; the session detaches and
            # is remembered for this tab
            wired_app.action_next_tab()
            await h.pause()
            assert wired_app._tab_active_session[ws.id] == SID
            assert (first._claude_command, "detach_persistent") in wired_app.lifecycle
            assert h.app.top is wired_app.home
            # back to the ws tab: still alive in tmux → auto-resume
            wired_app.alive["value"] = True
            wired_app.action_prev_tab()
            resumed = await wait_for_session_view(h)
            assert resumed.session_id == SID
            assert (resumed._claude_command, "attach_persistent", SID) in wired_app.lifecycle
            assert ws.id not in wired_app._tab_active_session

    async def test_dead_session_not_resumed_on_tab_return(self, wired_app):
        ws = wired_app._ws
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.open_detail(ws)
            await h.pause()
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            await wait_for_session_view(h)
            wired_app.action_next_tab()
            await h.pause()
            wired_app.alive["value"] = False  # session died while away
            wired_app.action_prev_tab()
            await h.pause(0.1)
            from tui.views.detail import DetailView
            assert isinstance(h.app.top, DetailView)  # no session pushed
            assert ws.id not in wired_app._tab_active_session

    async def test_close_tab_forgets_tab_session(self, wired_app):
        ws = wired_app._ws
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.open_detail(ws)
            await h.pause()
            wired_app._tab_active_session[ws.id] = SID
            wired_app.action_close_tab()
            await h.pause()
            assert ws.id not in wired_app._tab_active_session


@pytest.mark.asyncio
class TestAutoModeWiring:
    async def test_auto_key_with_backlog_opens_start_view(self, wired_app):
        ws = wired_app._ws
        wired_app.state.add_todo(ws.id, "task one")
        started = []
        wired_app._start_auto_mode = lambda *a, **k: started.append((a, k))
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            await wait_for_session_view(h)
            await h.press(AUTO_KEY)
            from tui.views.auto_mode_start import AutoModeStartView
            assert isinstance(h.app.top, AutoModeStartView)
            await h.press("escape")  # cancel → nothing started
            assert not started

    async def test_auto_key_no_backlog_confirms_first(self, wired_app):
        ws = wired_app._ws
        started = []
        wired_app._start_auto_mode = (
            lambda ws_id, sid, skip_ids: started.append((ws_id, sid, skip_ids)))
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            await wait_for_session_view(h)
            await h.press(AUTO_KEY)
            from tui.views.confirm import ConfirmView
            assert isinstance(h.app.top, ConfirmView)
            assert not started  # the key alone starts nothing
            await h.press("y")
            assert started == [(ws.id, SID, set())]

    async def test_auto_key_no_backlog_confirm_declined(self, wired_app):
        ws = wired_app._ws
        started = []
        wired_app._start_auto_mode = (
            lambda ws_id, sid, skip_ids: started.append((ws_id, sid, skip_ids)))
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            await wait_for_session_view(h)
            await h.press(AUTO_KEY)
            await h.press("n")
            assert not started

    async def test_auto_key_cancels_running_loop(self, wired_app):
        ws = wired_app._ws
        cancelled = []
        wired_app._auto_modes[ws.id] = type(
            "Mode", (), {"cancel": lambda self: cancelled.append(True)})()
        async with Headless(wired_app, size=(140, 40)) as h:
            wired_app.launch_claude_session(ws, session_id=SID, cwd=wired_app._cwd)
            await wait_for_session_view(h)
            await h.press(AUTO_KEY)
            assert cancelled == [True]
            assert "[auto] canceling" in wired_app.toast_text


class TestHelpSessionContext:
    def test_session_context_items(self):
        from tui.views.help import HelpView
        view = HelpView(context="session")
        assert view.title.startswith("Claude Session")
        joined = " ".join(m for _, m in view._get_items())
        assert "Detach and go back" in joined
        assert "Extract a todo" in joined
