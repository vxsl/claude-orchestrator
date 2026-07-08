"""session_launch.py — the extracted spawn helpers (P5).

Purity (no Textual on import) is covered by tests/test_purity.py; here we
pin the back-compat redirect (claude_session_screen re-exports the same
objects) and the helpers' own contracts, with HOME redirected so no test
touches the real ~/.cache spawn-args.
"""

import shlex

import pytest

import session_launch
from models import Category, Store, Workstream
from session_launch import (
    auto_link_session,
    build_claude_command,
    build_session_context,
    build_session_env,
    claude_jsonl_path,
)


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))


def test_frozen_module_reexports_same_objects():
    ccs = pytest.importorskip("claude_session_screen")
    for name in (
        "ORCH_DIR", "_git_status_snapshot", "auto_link_session",
        "build_claude_command", "build_session_context", "build_session_env",
        "claude_jsonl_path", "log_session_exit", "spawn_implementer_session",
    ):
        assert getattr(ccs, name) is getattr(session_launch, name), name


class TestBuildClaudeCommand:
    def test_new_session_flag_and_prompt(self, tmp_path):
        cmd = build_claude_command(
            session_id="sid-1", cwd=str(tmp_path), sys_prompt="ctx",
            prompt="do the thing", ws_name="my ws", is_new=True,
        )
        argv = shlex.split(cmd)
        assert argv[:3] == ["claude", "--session-id", "sid-1"]
        assert "--resume" not in argv
        assert argv[-1] == "do the thing"
        assert "orch:my ws" in argv
        # sys prompt spilled to the (redirected) spawn-args dir
        i = argv.index("--append-system-prompt-file")
        assert argv[i + 1].endswith("sid-1.sys")
        assert open(argv[i + 1]).read() == "ctx"

    def test_resume_flag(self, tmp_path):
        cmd = build_claude_command(
            session_id="sid-2", cwd=str(tmp_path), sys_prompt="",
            prompt=None, ws_name="w", is_new=False,
        )
        assert shlex.split(cmd)[:3] == ["claude", "--resume", "sid-2"]

    def test_long_prompt_spills_to_file(self, tmp_path):
        long_prompt = "x" * 5000
        cmd = build_claude_command(
            session_id="sid-3", cwd=str(tmp_path), sys_prompt="",
            prompt=long_prompt, ws_name="w", is_new=True,
        )
        assert '"$(cat ' in cmd and "sid-3.prompt" in cmd
        assert long_prompt not in cmd


class TestSessionEnvAndPaths:
    def test_env_vars(self):
        env = build_session_env("ws-1", "sid-1")
        assert env["ORCH_WS_ID"] == "ws-1"
        assert env["ORCH_SESSION_ID"] == "sid-1"
        assert env["CLAUDE_SESSION_ID"] == "sid-1"
        assert env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] == "1"

    def test_jsonl_path_encodes_slashes_and_dots(self, tmp_path):
        p = claude_jsonl_path("/home/u/dev/ul.UB-1-x", "sid")
        assert p.name == "sid.jsonl"
        assert p.parent.name == "-home-u-dev-ul-UB-1-x"

    def test_context_mentions_ws(self, tmp_path):
        ws = Workstream(name="ctx ws", description="d", category=Category.WORK)
        ctx = build_session_context(ws)
        assert 'brain workstream: "ctx ws"' in ctx
        assert "gitStatus:" in ctx


class TestAutoLinkSession:
    def test_links_when_no_dir_links(self, tmp_path):
        store = Store(path=tmp_path / "d.json")
        ws = Workstream(name="w")
        store.add(ws)
        auto_link_session(store, ws.id, "sid-9")
        links = store.get(ws.id).links
        assert [(l.kind, l.value) for l in links] == [("claude-session", "sid-9")]
