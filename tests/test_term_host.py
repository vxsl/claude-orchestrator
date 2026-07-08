"""Tests for term_host.py — the engine-neutral terminal host.

These lock the orch-sessions tmux contract (exact argv) and the byte
pipeline behaviors (OSC 52 forwarding, pyte sequence filtering) before
the tui engine migration builds on them.
"""

import asyncio
import base64
from unittest.mock import MagicMock, patch

import pytest

from term_host import (
    TMUX_NAV_KEYS,
    TerminalHost,
    _KEY_MAP,
    _SeqFilter,
)


class TestSeqFilter:
    def test_plain_text_passthrough(self):
        f = _SeqFilter()
        assert f.feed("hello world") == "hello world"

    def test_strips_dcs_sequence(self):
        f = _SeqFilter()
        assert f.feed("a\x1bPsome-dcs-payload\x1b\\b") == "ab"

    def test_strips_dcs_across_chunks(self):
        f = _SeqFilter()
        assert f.feed("a\x1bPpartial") == "a"
        assert f.feed("more\x1b\\b") == "b"

    def test_bare_esc_at_chunk_boundary_preserved(self):
        f = _SeqFilter()
        assert f.feed("a\x1b") == "a"
        # ESC + [ is a CSI opener, not a stripped sequence — must survive
        assert f.feed("[1mB") == "\x1b[1mB"

    def test_strips_kitty_csi(self):
        f = _SeqFilter()
        assert f.feed("x\x1b[>1u" + "y") == "xy"

    def test_bel_terminates_apc(self):
        f = _SeqFilter()
        assert f.feed("a\x1b_apc-data\x07b") == "ab"


class TestKeyMaps:
    def test_critical_keys(self):
        assert _KEY_MAP["enter"] == "\r"
        assert _KEY_MAP["escape"] == "\x1b"
        assert _KEY_MAP["backspace"] == "\x7f"
        assert _KEY_MAP["up"] == "\x1b[A"
        assert _KEY_MAP["shift+tab"] == "\x1b[Z"
        assert _KEY_MAP["f1"] == "\x1bOP"
        assert _KEY_MAP["f12"] == "\x1b[24~"

    def test_tmux_nav_keys(self):
        assert TMUX_NAV_KEYS["ctrl+u"] == "halfpage-up"
        assert TMUX_NAV_KEYS["shift+end"] == "history-bottom"


def _host_with_clipboard():
    host = TerminalHost("true")
    captured = []
    host._clipboard_write = captured.append
    return host, captured


class TestOsc52:
    def test_bel_terminated(self):
        host, captured = _host_with_clipboard()
        payload = base64.b64encode(b"hello clip").decode()
        host._scan_osc52(f"\x1b]52;c;{payload}\x07".encode())
        assert captured == ["hello clip"]

    def test_st_terminated(self):
        host, captured = _host_with_clipboard()
        payload = base64.b64encode(b"st text").decode()
        host._scan_osc52(f"\x1b]52;c;{payload}\x1b\\".encode())
        assert captured == ["st text"]

    def test_empty_target_field(self):
        # tmux emits ";;<b64>" — separator at index 0 must be accepted
        host, captured = _host_with_clipboard()
        payload = base64.b64encode(b"tmux yank").decode()
        host._scan_osc52(f"\x1b]52;;{payload}\x07".encode())
        assert captured == ["tmux yank"]

    def test_sequence_split_across_reads(self):
        host, captured = _host_with_clipboard()
        payload = base64.b64encode(b"split payload").decode()
        seq = f"\x1b]52;c;{payload}\x07".encode()
        host._scan_osc52(seq[:8])
        assert captured == []
        host._scan_osc52(seq[8:])
        assert captured == ["split payload"]

    def test_surrounding_data_ignored(self):
        host, captured = _host_with_clipboard()
        payload = base64.b64encode(b"x").decode()
        host._scan_osc52(b"before" + f"\x1b]52;c;{payload}\x07".encode() + b"after")
        assert captured == ["x"]


class TestTmuxQueries:
    def test_session_alive_argv(self):
        with patch("term_host.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            assert TerminalHost.tmux_session_alive("abc") is True
            argv = run.call_args[0][0]
            assert argv == ["tmux", "-L", "orch-sessions", "has-session", "-t", "abc"]

    def test_session_dead(self):
        with patch("term_host.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1)
            assert TerminalHost.tmux_session_alive("abc") is False

    def test_list_sessions_argv(self):
        with patch("term_host.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="one\ntwo\n")
            assert TerminalHost.list_tmux_sessions() == ["one", "two"]
            argv = run.call_args[0][0]
            assert argv == [
                "tmux", "-L", "orch-sessions",
                "list-sessions", "-F", "#{session_name}",
            ]


@pytest.mark.asyncio
class TestPersistentLifecycle:
    async def test_start_persistent_argv_contract(self, tmp_path):
        host = TerminalHost("claude --resume abc", env={"FOO": "bar baz"}, cwd="/tmp")
        finished = asyncio.Event()
        host.on_finished = finished.set

        fake_pty_out = MagicMock()
        fake_pty_out.read.return_value = b""  # immediate EOF

        with patch("term_host.subprocess.run") as run, \
             patch("term_host.pty.fork", return_value=(4242, 99)), \
             patch("term_host.os.fdopen", return_value=fake_pty_out), \
             patch("term_host.fcntl.ioctl"):
            run.return_value = MagicMock(returncode=0, stderr="")
            host.start_persistent("sess-1")

            new_session_argv = run.call_args_list[0][0][0]
            assert new_session_argv[:4] == ["tmux", "-L", "orch-sessions", "-f"]
            assert new_session_argv[5:13] == [
                "new-session", "-d", "-s", "sess-1", "-x", "80", "-y", "24",
            ]
            assert new_session_argv[13:15] == ["-c", "/tmp"]
            inner = new_session_argv[15]
            assert inner.startswith("env TERM=xterm-256color COLORTERM=truecolor")
            assert "FOO='bar baz'" in inner
            assert inner.endswith("claude --resume abc")
            # TMUX must be unset for the nested server
            env = run.call_args_list[0][1]["env"]
            assert "TMUX" not in env
            # config re-sourced against a possibly-running server
            reload_argv = run.call_args_list[1][0][0]
            assert reload_argv[:4] == ["tmux", "-L", "orch-sessions", "source-file"]

            assert host._persistent_session == "sess-1"
            # EOF from the fake PTY fires the finished hook
            await asyncio.wait_for(finished.wait(), timeout=2)

    async def test_duplicate_session_falls_back_to_attach(self):
        host = TerminalHost("claude")
        with patch("term_host.subprocess.run") as run, \
             patch.object(host, "attach_persistent") as attach:
            run.return_value = MagicMock(
                returncode=1, stderr="duplicate session: sess-1")
            host.start_persistent("sess-1")
            attach.assert_called_once_with("sess-1")

    async def test_new_session_failure_raises(self):
        host = TerminalHost("claude")
        with patch("term_host.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stderr="server exited")
            with pytest.raises(RuntimeError, match="tmux new-session failed"):
                host.start_persistent("sess-1")

    async def test_detach_persistent_keeps_session(self):
        host = TerminalHost("claude")
        host._persistent_session = "sess-1"
        host._pid, host._fd = 4242, 99
        host._p_out = MagicMock()

        with patch("term_host.os.kill") as kill, \
             patch("term_host.os.waitpid"), \
             patch("term_host.subprocess.run") as run:
            host.detach_persistent()
            kill.assert_called_once()          # attach client killed...
            run.assert_not_called()            # ...but no tmux kill-session
        assert host._pid is None and host._p_out is None

    async def test_stop_persistent_kills_session(self):
        host = TerminalHost("claude")
        host._persistent_session = "sess-1"
        with patch.object(host, "stop") as stop, \
             patch("term_host.subprocess.run") as run:
            host.stop_persistent()
            stop.assert_called_once()
            argv = run.call_args[0][0]
            assert argv == [
                "tmux", "-L", "orch-sessions", "kill-session", "-t", "sess-1",
            ]


class TestTmuxConf:
    def test_conf_written_and_stable(self, tmp_path):
        with patch("term_host.tempfile.gettempdir", return_value=str(tmp_path)):
            path1 = TerminalHost._tmux_conf_path()
            content = open(path1).read()
            assert "set -g status off" in content
            assert "set -g set-clipboard on" in content
            assert "mode-keys vi" in content
            # second call leaves the file byte-identical
            path2 = TerminalHost._tmux_conf_path()
            assert path1 == path2
            assert open(path2).read() == content
