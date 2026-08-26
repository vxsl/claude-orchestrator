"""cli.py — the machine-readable (`--json`) output and the `open` verb.

Two contracts are pinned here. First: a `--json` run puts the payload and
nothing else on stdout — no ANSI, no heading, no footer — so a consumer can
pipe it into jq without sanitizing. Second: `orch open` resolves a session id
against the store and lands the user in a tmux window whether that session is
still running, cold with a transcript, or being opened from outside tmux.

Every tmux call goes through an injected runner (FakeTmux), so no test speaks
to a real tmux server — the one the developer's own sessions live on.
"""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli import (
    _resolve_session,
    _session_json,
    _ws_json,
    cmd_list,
    cmd_sessions,
    open_session,
)
from models import Category, Store, TodoItem, Workstream
from sessions import ClaudeSession


SID = "11112222-3333-4444-5555-666677778888"


def _session(session_id=SID, **kw):
    kw.setdefault("project_path", "/home/kyle/dev/project")
    kw.setdefault("project_dir", "-home-kyle-dev-project")
    kw.setdefault("message_count", 12)
    return ClaudeSession(session_id=session_id, **kw)


def _list_args(**kw):
    args = SimpleNamespace(archived=False, category=None, search=None,
                           sort="updated", json=False)
    args.__dict__.update(kw)
    return args


def _sessions_args(**kw):
    args = SimpleNamespace(project="", limit=20, json=False)
    args.__dict__.update(kw)
    return args


class FakeTmux:
    """Stand-in for subprocess.run that answers tmux queries from a script.

    `live` names the tmux sessions that exist (on either socket — the argv
    records which was asked); `fail` names the verbs that should come back
    nonzero, for the error paths.
    """

    def __init__(self, live=(), fail=()):
        self.live = set(live)
        self.fail = set(fail)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
        if self.fail & set(argv):
            return subprocess.CompletedProcess(argv, 1, "", "tmux: boom")
        if "has-session" in argv:
            target = argv[argv.index("-t") + 1]
            rc = 0 if target in self.live else 1
            return subprocess.CompletedProcess(argv, rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def find(self, verb: str):
        """First recorded argv containing `verb`, or None."""
        return next((c for c in self.calls if verb in c), None)


# ─── --json output ──────────────────────────────────────────────────


class TestSessionsJson:
    def test_payload_is_the_whole_of_stdout(self, capsys):
        s = _session(title="Fix the auth bug", git_branch="fix-auth",
                     is_live=True, last_activity="2026-08-26T07:00:00Z",
                     last_assistant_message_text="did the thing")
        with patch("sessions.discover_sessions", return_value=[s]):
            cmd_sessions(_sessions_args(json=True))
        out = capsys.readouterr().out
        assert "\x1b" not in out, "ANSI leaked into machine output"
        payload = json.loads(out)
        assert payload == [{
            "session_id": SID,
            "title": "Fix the auth bug",
            "project_path": "/home/kyle/dev/project",
            "git_branch": "fix-auth",
            "is_live": True,
            "last_activity": "2026-08-26T07:00:00Z",
            "message_count": 12,
            "last_assistant_message_text": "did the thing",
        }]

    def test_empty_is_an_empty_array_not_a_message(self, capsys):
        with patch("sessions.discover_sessions", return_value=[]):
            cmd_sessions(_sessions_args(json=True))
        assert json.loads(capsys.readouterr().out) == []

    def test_last_assistant_message_clipped_to_300(self):
        s = _session(last_assistant_message_text="x " * 400)
        assert len(_session_json(s)["last_assistant_message_text"]) == 300

    def test_newlines_collapsed_out_of_fields(self):
        s = _session(last_assistant_message_text="one\n\ntwo   three")
        assert _session_json(s)["last_assistant_message_text"] == "one two three"

    def test_title_falls_back_to_project_path(self):
        assert _session_json(_session())["title"].endswith("project")

    def test_human_output_unchanged_without_the_flag(self, capsys):
        with patch("sessions.discover_sessions", return_value=[_session(title="T")]):
            cmd_sessions(_sessions_args())
        out = capsys.readouterr().out
        assert "1 sessions" in out
        assert "\x1b" in out  # the human variant is still colored


class TestListJson:
    def _store(self, tmp_path):
        store = Store(path=tmp_path / "data.json")
        ws = Workstream(name="Machine readable", category=Category.WORK)
        ws.repo_path = "/home/kyle/dev/orch"
        ws.add_link("ticket", "UB-1234")
        ws.todos = [
            TodoItem(text="pending one"),
            TodoItem(text="pending two"),
            TodoItem(text="finished", done=True),
            TodoItem(text="off the board", archived=True),
        ]
        ws.auto_running = True
        ws.auto_current_todo_id = "abc12345"
        store.add(ws)
        return store, ws

    def test_payload_is_the_whole_of_stdout(self, tmp_path, capsys):
        store, ws = self._store(tmp_path)
        with patch("cli.Store", return_value=store):
            cmd_list(_list_args(json=True))
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "items" not in out  # no human footer
        payload = json.loads(out)
        assert payload == [{
            "id": ws.id,
            "name": "Machine readable",
            "category": "work",
            "archived": False,
            "repo_path": "/home/kyle/dev/orch",
            "links": [{"kind": "ticket", "label": "ticket", "value": "UB-1234"}],
            "todos": {"pending": 2, "done": 1},
            "auto_running": True,
            "auto_current_todo_id": "abc12345",
        }]

    def test_archived_todos_count_as_neither(self, tmp_path):
        _, ws = self._store(tmp_path)
        assert _ws_json(ws)["todos"] == {"pending": 2, "done": 1}

    def test_empty_is_an_empty_array_not_a_message(self, tmp_path, capsys):
        store = Store(path=tmp_path / "data.json")
        with patch("cli.Store", return_value=store):
            cmd_list(_list_args(json=True))
        assert json.loads(capsys.readouterr().out) == []

    def test_filters_still_apply(self, tmp_path, capsys):
        store, _ = self._store(tmp_path)
        store.add(Workstream(name="Personal thing", category=Category.PERSONAL))
        with patch("cli.Store", return_value=store):
            cmd_list(_list_args(json=True, category="personal"))
        payload = json.loads(capsys.readouterr().out)
        assert [w["name"] for w in payload] == ["Personal thing"]


# ─── orch open ──────────────────────────────────────────────────────


class TestResolveSession:
    def test_unique_prefix(self):
        s = _session()
        with patch("sessions.discover_sessions", return_value=[s]):
            assert _resolve_session("1111").session_id == SID

    def test_exact_id_beats_prefix_scan(self):
        other = _session(session_id=SID[:-1] + "9")
        with patch("sessions.discover_sessions", return_value=[other, _session()]):
            assert _resolve_session(SID).session_id == SID

    def test_matches_an_id_the_transcript_carries(self):
        s = _session(session_id="99998888-0000-0000-0000-000000000000",
                     all_session_ids=["99998888-0000-0000-0000-000000000000", SID])
        with patch("sessions.discover_sessions", return_value=[s]):
            assert _resolve_session("1111").session_id.startswith("9999")

    def test_ambiguous_prefix_lists_candidates_on_stderr(self, capsys):
        pair = [_session(), _session(session_id=SID[:4] + "aaaa" + SID[8:])]
        with patch("sessions.discover_sessions", return_value=pair), \
             pytest.raises(SystemExit):
            _resolve_session("1111")
        err = capsys.readouterr().err
        assert "Ambiguous" in err
        assert SID in err

    def test_falls_back_to_the_transcript_on_disk(self):
        s = _session()
        with patch("sessions.discover_sessions", return_value=[]), \
             patch("sessions.find_session", return_value=s):
            assert _resolve_session("1111").session_id == SID

    def test_miss_exits_with_a_stderr_message(self, capsys):
        with patch("sessions.discover_sessions", return_value=[]), \
             patch("sessions.find_session", return_value=None), \
             pytest.raises(SystemExit):
            _resolve_session("dead")
        assert "no session matching" in capsys.readouterr().err.lower()


@pytest.fixture
def no_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)


@pytest.fixture
def in_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")


class TestOpenLiveSession:
    def test_attaches_without_reviving(self, in_tmux, capsys):
        tmux = FakeTmux(live={SID})
        assert open_session(_session(title="Fix the auth bug"), run=tmux) == 0
        assert "Opened" in capsys.readouterr().out
        assert tmux.find("new-session") is None
        window = tmux.find("new-window")
        assert window == ["tmux", "new-window", "-n", "Fix the auth bug",
                          f"tmux -L orch-sessions attach -t {SID}"]

    def test_checks_the_orch_socket_not_the_default_one(self, in_tmux):
        tmux = FakeTmux(live={SID})
        open_session(_session(), run=tmux)
        assert tmux.calls[0] == ["tmux", "-L", "orch-sessions",
                                 "has-session", "-t", SID]

    def test_attaches_to_the_id_the_tmux_session_was_named_for(self, in_tmux):
        # A resumed session's file is named for the new id; tmux still knows
        # it by the original one.
        original = "aaaabbbb-0000-0000-0000-000000000000"
        s = _session(all_session_ids=[SID, original])
        tmux = FakeTmux(live={original})
        open_session(s, run=tmux)
        assert original in tmux.find("new-window")[-1]

    def test_window_label_clipped_to_20(self, in_tmux):
        tmux = FakeTmux(live={SID})
        open_session(_session(title="a title far longer than twenty chars"), run=tmux)
        label = tmux.find("new-window")[3]
        assert label == "a title far longer t"
        assert len(label) == 20

    def test_new_window_failure_is_not_silent(self, in_tmux, capsys):
        tmux = FakeTmux(live={SID}, fail={"new-window"})
        with pytest.raises(SystemExit):
            open_session(_session(), run=tmux)
        assert "new-window failed" in capsys.readouterr().err


class TestOpenColdSession:
    def _conf(self, tmp_path):
        conf = tmp_path / "orch-tmux.conf"
        conf.write_text("set -g status off\n")
        return patch("term_host.TerminalHost._tmux_conf_path", return_value=str(conf)), str(conf)

    def test_revives_then_attaches(self, in_tmux, tmp_path, capsys):
        conf_patch, conf = self._conf(tmp_path)
        tmux = FakeTmux()
        with conf_patch:
            assert open_session(_session(project_path=str(tmp_path)), run=tmux) == 0
        new = tmux.find("new-session")
        assert new[:4] == ["tmux", "-L", "orch-sessions", "-f"]
        assert new[4] == conf
        assert new[5:12] == ["new-session", "-d", "-s", SID, "-x", "200", "-y"]
        assert new[-3:-1] == ["-c", str(tmp_path)]
        assert new[-1].endswith(f"claude --resume {SID}")
        assert "TERM=xterm-256color" in new[-1]
        # and only then the attach
        assert tmux.calls.index(new) < tmux.calls.index(tmux.find("new-window"))
        assert "reviving" in capsys.readouterr().out

    def test_new_session_runs_without_the_outer_tmux_in_env(self, in_tmux, tmp_path):
        conf_patch, _ = self._conf(tmp_path)
        tmux = FakeTmux()
        with conf_patch:
            open_session(_session(project_path=str(tmp_path)), run=tmux)
        env = tmux.kwargs[tmux.calls.index(tmux.find("new-session"))]["env"]
        assert "TMUX" not in env
        assert env["TERM"] == "xterm-256color"

    def test_config_re_sourced_for_a_server_started_earlier(self, in_tmux, tmp_path):
        conf_patch, conf = self._conf(tmp_path)
        tmux = FakeTmux()
        with conf_patch:
            open_session(_session(project_path=str(tmp_path)), run=tmux)
        assert tmux.find("source-file") == ["tmux", "-L", "orch-sessions",
                                            "source-file", conf]

    def test_missing_project_path_still_resumes(self, in_tmux, tmp_path, capsys):
        conf_patch, _ = self._conf(tmp_path)
        tmux = FakeTmux()
        with conf_patch:
            open_session(_session(project_path="/gone/for/good"), run=tmux)
        assert "-c" not in tmux.find("new-session")
        assert "Project path is gone" in capsys.readouterr().out

    def test_no_transcript_no_revival(self, in_tmux, tmp_path, capsys):
        s = _session(jsonl_path=str(tmp_path / "not-there.jsonl"))
        tmux = FakeTmux()
        with pytest.raises(SystemExit):
            open_session(s, run=tmux)
        assert "no live tmux and no transcript" in capsys.readouterr().err
        assert tmux.find("new-session") is None

    def test_new_session_failure_is_not_silent(self, in_tmux, tmp_path, capsys):
        conf_patch, _ = self._conf(tmp_path)
        tmux = FakeTmux(fail={"new-session"})
        with conf_patch, pytest.raises(SystemExit):
            open_session(_session(project_path=str(tmp_path)), run=tmux)
        err = capsys.readouterr().err
        assert "new-session failed" in err and "boom" in err


class TestOpenFromOutsideTmux:
    def test_targets_the_orch_session_on_the_default_server(self, no_tmux):
        tmux = FakeTmux(live={SID, "orch"})
        assert open_session(_session(title="Fix auth"), run=tmux) == 0
        assert tmux.find("new-window") == [
            "tmux", "-L", "default", "new-window", "-t", "orch:",
            "-n", "Fix auth", f"tmux -L orch-sessions attach -t {SID}",
        ]

    def test_absent_orch_session_prints_the_attach_command(self, no_tmux, capsys):
        tmux = FakeTmux(live={SID})
        assert open_session(_session(), run=tmux) == 0
        out = capsys.readouterr().out
        assert f"tmux -L orch-sessions attach -t {SID}" in out
        assert tmux.find("new-window") is None
