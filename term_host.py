"""Engine-neutral terminal host: PTY lifecycle, tmux-persistent sessions,
and terminal-emulator feeding (libvterm via ctypes, pyte fallback).

This is the framework-free half of the embedded terminal. TerminalWidget
(terminal.py, Textual) and the tui engine's TerminalPane both mix this in
and provide rendering + input on top. No textual imports allowed here —
tests/test_purity.py enforces it.

Override hooks for the UI layer (all safe no-ops by default):
- ``_handle_pty_eof()``     — child exited (falls back to ``on_finished`` callable)
- ``_clipboard_write(text)``— OSC 52 payload extracted from the PTY stream
- ``_on_frame_complete()``  — synchronized-output end (2026l): render now
- ``_request_render()``     — scrollback offset changed: repaint
- ``_release_fd_reader()``  — fd about to close/hand off: drop its loop reader
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import subprocess
import tempfile
import termios
from pathlib import Path

import pyte
from pyte.screens import Char
from rich.style import Style

try:
    from vterm_backend import VTermBackend
    _HAS_VTERM = True
except ImportError:
    _HAS_VTERM = False


# ── pyte subclasses ───────────────────────────────────────────────

class _Screen(pyte.Screen):
    """Pyte screen with extra tolerance for modern terminal sequences."""

    def set_margins(self, *args, **kwargs):
        kwargs.pop("private", None)
        return super().set_margins(*args, **kwargs)

    def _ignore(self, *args, **kwargs):
        """Silently ignore unsupported sequences."""
        pass

    def csi_save_cursor(self, *args):
        """CSI s — save cursor position (accepts and ignores params)."""
        self.save_cursor()

    def csi_restore_cursor(self, *args):
        """CSI u — restore cursor position (accepts and ignores params)."""
        self.restore_cursor()

    def scroll_up(self, count=1):
        """CSI S — scroll content up (new blank lines at bottom)."""
        saved_y = self.cursor.y
        bottom = self.margins.bottom if self.margins else self.lines - 1
        for _ in range(count):
            self.cursor.y = bottom
            self.index()
        self.cursor.y = saved_y

    def scroll_down(self, count=1):
        """CSI T — scroll content down (new blank lines at top)."""
        saved_y = self.cursor.y
        top = self.margins.top if self.margins else 0
        for _ in range(count):
            self.cursor.y = top
            self.reverse_index()
        self.cursor.y = saved_y


class _Stream(pyte.Stream):
    """Pyte stream with additional CSI handlers.

    Note: self.csi must be patched BEFORE super().__init__ because the
    parser FSM is baked during __init__ → attach() → _initialize_parser().
    """

    def __init__(self, *args, **kwargs):
        # Patch csi table before the parser FSM is created
        self.csi = dict(pyte.Stream.csi)
        self.csi["s"] = "csi_save_cursor"      # cursor save (ANSI.SYS)
        self.csi["u"] = "csi_restore_cursor"   # cursor restore (ANSI.SYS)
        self.csi["S"] = "scroll_up"        # scroll up
        self.csi["T"] = "scroll_down"      # scroll down
        self.csi["t"] = "_ignore"          # window manipulation
        self.csi["q"] = "_ignore"          # cursor shape
        super().__init__(*args, **kwargs)


# ── Escape sequence filter ─────────────────────────────────────────

# CSI sequences with intermediate bytes pyte can't parse:
# =/>/<  (kitty keyboard, DA2, etc.)
# space  (cursor shape \x1b[0 q, etc.)
_STRIP_CSI_EXT = re.compile(
    r"\x1b\[\??[\d;]*[=><][\d;]*[a-zA-Z]"
    r"|\x1b\[\??[\d;]* [a-zA-Z]"
)
# DECSCUSR: ESC [ Ps SP q — cursor shape (space is the intermediate byte)
_DECSCUSR = re.compile(r"\x1b\[(\d*) q")


class _SeqFilter:
    """Stateful filter that strips escape sequences pyte can't handle.

    Handles DCS (ESC P), APC (ESC _), PM (ESC ^), and SOS (ESC X)
    sequences even when they span multiple data chunks.  OSC (ESC ])
    is left alone — pyte handles it.
    """

    _OPENERS = frozenset("P_^X")

    def __init__(self) -> None:
        self._stripping = False   # inside a sequence to discard
        self._esc_pending = False  # last chunk ended with bare ESC

    def feed(self, data: str) -> str:
        # Fast path — no state and no ESC in data
        if not self._stripping and not self._esc_pending and "\x1b" not in data:
            return data

        out: list[str] = []
        i = 0
        n = len(data)

        while i < n:
            ch = data[i]

            # ── resolve a pending ESC from the previous chunk ──
            if self._esc_pending:
                self._esc_pending = False
                if self._stripping:
                    if ch == "\\":          # ST terminator → end strip
                        self._stripping = False
                        i += 1
                        continue
                    i += 1                  # still inside stripped seq
                    continue
                else:
                    if ch in self._OPENERS:
                        self._stripping = True
                        i += 1
                        continue
                    out.append("\x1b")      # wasn't an opener → emit ESC
                    out.append(ch)
                    i += 1
                    continue

            # ── stripping mode: consume until BEL or ST ──
            if self._stripping:
                if ch == "\x07":
                    self._stripping = False
                elif ch == "\x1b":
                    if i + 1 < n:
                        if data[i + 1] == "\\":
                            self._stripping = False
                            i += 2
                            continue
                        # ESC not followed by \ — still stripping
                    else:
                        self._esc_pending = True
                i += 1
                continue

            # ── normal mode ──
            if ch == "\x1b":
                if i + 1 < n:
                    if data[i + 1] in self._OPENERS:
                        self._stripping = True
                        i += 2
                        continue
                    out.append(ch)
                    i += 1
                    continue
                else:
                    self._esc_pending = True
                    i += 1
                    continue

            out.append(ch)
            i += 1

        result = "".join(out)
        return _STRIP_CSI_EXT.sub("", result)


# ── Color helpers ──────────────────────────────────────────────────

_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")

_COLOR_FIXES: dict[str, str] = {
    "brown": "yellow",
    "brightblack": "#808080",
}


def _pyte_color(color: str) -> str | None:
    """Convert a pyte fg/bg value to a Rich color string (or None for default)."""
    if color == "default":
        return None
    if color in _COLOR_FIXES:
        return _COLOR_FIXES[color]
    if _HEX_RE.match(color):
        return f"#{color}"
    return color


def _char_style(char: Char) -> Style:
    """Build a Rich Style from a pyte Char."""
    return Style(
        color=_pyte_color(char.fg),
        bgcolor=_pyte_color(char.bg),
        bold=char.bold,
        italic=char.italics,
        underline=char.underscore,
        strike=char.strikethrough,
        reverse=char.reverse,
    )


def _same_style(a: Char, b: Char) -> bool:
    return (
        a.fg == b.fg
        and a.bg == b.bg
        and a.bold == b.bold
        and a.italics == b.italics
        and a.underscore == b.underscore
        and a.strikethrough == b.strikethrough
        and a.reverse == b.reverse
    )


# ── ANSI passthrough detection ─────────────────────────────────────

_ANSI_SEQ = re.compile(r"\x1b\[\??[\d;]*[a-zA-Z]")
_DECSET_PREFIX = "\x1b[?"


# ── Key mapping ────────────────────────────────────────────────────

_KEY_MAP: dict[str, str] = {
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "delete": "\x1b[3~",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "shift+tab": "\x1b[Z",
    "insert": "\x1b[2~",
    "escape": "\x1b",
    "tab": "\t",
    "enter": "\r",
    "backspace": "\x7f",
}

# Function keys
for i in range(1, 13):
    _seqs = [
        "\x1bOP", "\x1bOQ", "\x1bOR", "\x1bOS",
        "\x1b[15~", "\x1b[17~", "\x1b[18~", "\x1b[19~",
        "\x1b[20~", "\x1b[21~", "\x1b[23~", "\x1b[24~",
    ]
    _KEY_MAP[f"f{i}"] = _seqs[i - 1]


# Map of Textual key names → tmux copy-mode commands.  When a
# persistent (tmux) session is attached, these keys auto-enter
# copy-mode so scrollback browsing and selection share one buffer.
TMUX_NAV_KEYS = {
    "ctrl+u": "halfpage-up",
    "shift+pageup": "halfpage-up",
    "ctrl+d": "halfpage-down",
    "shift+pagedown": "halfpage-down",
    "shift+up": "cursor-up",
    "shift+down": "cursor-down",
    "shift+home": "history-top",
    "shift+end": "history-bottom",
}


# ── Host ───────────────────────────────────────────────────────────

class TerminalHost:
    """PTY + terminal-backend host, shared by both UI engines (mixin).

    Owns the child process, the tmux-persistent lifecycle, and the byte
    pipeline into libvterm/pyte. Knows nothing about painting or key
    events — the UI layer overrides the ``_handle_pty_eof`` /
    ``_clipboard_write`` / ``_on_frame_complete`` / ``_request_render``
    hooks.
    """

    def __init__(
        self,
        command: str = "bash",
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._extra_env = env or {}
        self._cwd = cwd
        self._ncol = 80
        self._nrow = 24
        self._mouse_tracking = False
        self._sync_output = False  # DEC private mode 2026 (synchronized output)
        self._cursor_shape = 1  # DECSCUSR: 1/2=block, 3/4=underline, 5/6=bar

        # Scrollback scroll offset (0 = live screen, >0 = scrolled up)
        self._scroll_offset = 0

        # Terminal backend: libvterm (complete) or pyte (fallback)
        self._backend = VTermBackend(self._ncol, self._nrow) if _HAS_VTERM else None
        if not self._backend:
            self._screen = _Screen(self._ncol, self._nrow)
            self._stream = _Stream(self._screen)
            self._seq_filter = _SeqFilter()

        # PTY state
        self._pid: int | None = None
        self._fd: int | None = None
        self._p_out = None
        self._read_task: asyncio.Task | None = None
        self._persistent_session: str | None = None

        # Render rate-limiting: data processing and rendering are decoupled.
        # _has_dirty is set by the read loop; the UI's render tick flushes it.
        self._has_dirty = False

        # Buffer for OSC 52 (clipboard set) sequences that span multiple
        # PTY reads.  See _scan_osc52.
        self._osc52_buf: bytes = b""

        # Optional callback fired on child exit (UI may override
        # _handle_pty_eof instead).
        self.on_finished = None

    # ── UI override hooks ─────────────────────────────────────────

    def _handle_pty_eof(self) -> None:
        """Child process exited (EOF on the PTY master)."""
        if self.on_finished is not None:
            self.on_finished()

    def _clipboard_write(self, text: str) -> None:
        """OSC 52 payload extracted from the stream — forward to the outer
        terminal's clipboard. UI layer overrides."""

    def _on_frame_complete(self) -> None:
        """Synchronized output ended (2026l) — render immediately."""

    def _request_render(self) -> None:
        """Scrollback offset changed — repaint the pane."""

    def _release_fd_reader(self) -> None:
        """Called immediately before the master fd is closed or handed off
        (stop/detach), while it is still open. A UI layer that registered an
        event-loop reader on the fd MUST deregister it here: closing an fd
        out from under asyncio leaves a stale selector key that silently
        defeats the next ``add_reader`` on the reused fd number (epoll drops
        the closed fd, but asyncio's map keeps it, so the reuse is treated as
        a no-op ``modify`` and the new reader never fires). No-op for the
        base executor read loop, which owns no such reader."""

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Fork the PTY and begin reading."""
        if self._pid is not None:
            return

        self._pid, self._fd = pty.fork()
        if self._pid == 0:
            # Child — exec the command.
            # Safety: if exec fails, _exit immediately so we never
            # fall through into the parent's event loop.
            try:
                if self._cwd:
                    os.chdir(self._cwd)
                argv = shlex.split(self._command)
                env = os.environ.copy()
                env.update(TERM="xterm-256color", COLORTERM="truecolor")
                env.update(self._extra_env)
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(127)

        self._p_out = os.fdopen(self._fd, "w+b", 0)
        self._set_pty_size(self._nrow, self._ncol)
        self._read_task = asyncio.create_task(self._read_loop())

    def stop(self) -> None:
        """Kill the subprocess and clean up."""
        if self._pid is None:
            return
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        try:
            os.kill(self._pid, signal.SIGTERM)
            os.waitpid(self._pid, 0)
        except (OSError, ChildProcessError):
            pass
        # Deregister any event-loop reader while the fd is still open, then
        # close the PTY master fd to avoid leaking file descriptors.
        self._release_fd_reader()
        if self._p_out is not None:
            try:
                self._p_out.close()
            except OSError:
                pass
        self._pid = None
        self._fd = None
        self._p_out = None

    def detach(self) -> dict | None:
        """Stop reading but keep the process alive.  Returns state dict
        that can be passed to ``attach()`` on a new instance."""
        if self._pid is None:
            return None
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        self._release_fd_reader()  # fd is handed off, not closed — see hook
        state: dict = {
            "pid": self._pid,
            "fd": self._fd,
            "p_out": self._p_out,
        }
        if self._backend:
            state["backend"] = self._backend
        else:
            state["screen"] = self._screen
            state["stream"] = self._stream
            state["seq_filter"] = self._seq_filter
        # Neuter so a later stop() won't kill the process
        self._pid = None
        self._fd = None
        self._p_out = None
        return state

    def attach(self, state: dict) -> None:
        """Reattach to an existing PTY from a previous ``detach()``.
        Call this instead of ``start()``."""
        self._pid = state["pid"]
        self._fd = state["fd"]
        self._p_out = state["p_out"]
        if "backend" in state:
            self._backend = state["backend"]
            self._backend.resize(self._nrow, self._ncol)
        else:
            self._screen = state["screen"]
            self._stream = state["stream"]
            self._seq_filter = state["seq_filter"]
            self._screen.resize(self._nrow, self._ncol)
        self._set_pty_size(self._nrow, self._ncol)
        self._read_task = asyncio.create_task(self._read_loop())

    # ── Persistent (tmux-backed) lifecycle ───────────────────────

    TMUX_SOCKET = "orch-sessions"

    @staticmethod
    def _tmux_conf_path() -> str:
        """Return path to a minimal tmux config that makes tmux invisible."""
        conf = Path(tempfile.gettempdir()) / "orch-tmux.conf"
        _clip = (
            "wl-copy 2>/dev/null || "
            "xclip -selection clipboard 2>/dev/null || "
            "xsel --clipboard --input 2>/dev/null"
        )
        content = (
            "set -g status off\n"
            "set -g prefix None\n"
            "set -s escape-time 0\n"
            "set -g mouse on\n"
            "set -g history-limit 50000\n"
            # Make tmux emit OSC 52 on copy.  Our terminal widget
            # extracts these and forwards them to the outer TTY, so
            # yanks reach the user's local clipboard even over SSH.
            "set -g set-clipboard on\n"
            "setw -g mode-keys vi\n"
            "bind-key -T copy-mode-vi v send-keys -X begin-selection\n"
            "bind-key -T copy-mode-vi V send-keys -X select-line\n"
            "bind-key -T copy-mode-vi C-v send-keys -X rectangle-toggle\n"
            # `y` yanks and exits copy-mode (vim-style).
            # `Enter` yanks and keeps the selection active so you can
            # extend it and yank again without re-entering copy-mode.
            f"bind-key -T copy-mode-vi y send-keys -X copy-pipe-and-cancel '{_clip}'\n"
            f"bind-key -T copy-mode-vi Enter send-keys -X copy-pipe-no-clear '{_clip}'\n"
            "bind-key -T copy-mode-vi q send-keys -X cancel\n"
            # Vim-style two-stage Escape: first Escape with an active
            # selection clears it (still in copy-mode, cursor preserved);
            # second Escape (no selection) exits copy-mode.
            "bind-key -T copy-mode-vi Escape "
            "if-shell -F '#{selection_present}' "
            "'send-keys -X clear-selection' "
            "'send-keys -X cancel'\n"
        )
        # Always rewrite so config updates take effect across orch versions
        if not conf.exists() or conf.read_text() != content:
            conf.write_text(content)
        return str(conf)

    @classmethod
    def _reload_tmux_config(cls, env: dict | None = None) -> None:
        """Re-source the tmux config against a running server, if any.

        ``-f`` on ``new-session`` only applies when tmux bootstraps a new
        server; if an earlier orch run already started a server, its config
        is stale.  ``source-file`` updates it in place.
        """
        conf = cls._tmux_conf_path()
        try:
            subprocess.run(
                ["tmux", "-L", cls.TMUX_SOCKET, "source-file", conf],
                env=env, capture_output=True, timeout=2,
            )
        except Exception:
            pass

    def start_persistent(self, session_name: str) -> None:
        """Start the command inside a tmux session, then attach to it.

        The tmux server (on a dedicated socket) owns the PTY, so the
        child process survives if orch is killed.
        """
        if self._pid is not None:
            return

        conf = self._tmux_conf_path()
        argv = shlex.split(self._command)
        env = os.environ.copy()
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        env.update(self._extra_env)
        # Unset TMUX so nested tmux works
        env.pop("TMUX", None)

        # Build the inner command with env vars prepended
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in self._extra_env.items()
        )
        inner_cmd = f"env TERM=xterm-256color COLORTERM=truecolor {env_prefix} {self._command}"

        # Create the tmux session (detached) running our command
        tmux_cmd = [
            "tmux", "-L", self.TMUX_SOCKET, "-f", conf,
            "new-session", "-d",
            "-s", session_name,
            "-x", str(self._ncol), "-y", str(self._nrow),
        ]
        if self._cwd:
            tmux_cmd.extend(["-c", self._cwd])
        tmux_cmd.append(inner_cmd)
        result = subprocess.run(tmux_cmd, env=env, timeout=5, capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stderr or "").strip() or "(no stderr)"
            # The session already exists. The alive-check in app.py (a separate
            # `has-session` subprocess) raced against us or timed out and returned
            # a false negative, so we were told to create rather than reattach.
            # A session named after the UUID is always `claude --resume <uuid>`,
            # so attaching to the survivor is exactly what the caller wanted.
            if "duplicate session" in err.lower():
                self.attach_persistent(session_name)
                return
            # tmux silently fails here cause "can't find session" downstream when
            # the attach runs against a session that was never created. Common
            # culprit: inner_cmd over tmux's ~16KB command-length limit.
            raise RuntimeError(
                f"tmux new-session failed (rc={result.returncode}): {err} "
                f"[inner_cmd was {len(inner_cmd)} bytes]"
            )
        # Ensure the running server picks up any config updates since it started
        self._reload_tmux_config(env)

        # Now attach to it via pty.fork — the attach process is what we
        # manage; the actual claude process lives in the tmux server.
        self._persistent_session = session_name
        self._pid, self._fd = pty.fork()
        if self._pid == 0:
            try:
                os.execvpe("tmux", [
                    "tmux", "-L", self.TMUX_SOCKET,
                    "attach", "-t", session_name,
                ], env)
            except Exception:
                os._exit(127)

        self._p_out = os.fdopen(self._fd, "w+b", 0)
        self._set_pty_size(self._nrow, self._ncol)
        self._read_task = asyncio.create_task(self._read_loop())

    def attach_persistent(self, session_name: str) -> None:
        """Reattach to a surviving tmux session."""
        if self._pid is not None:
            return

        env = os.environ.copy()
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        env.pop("TMUX", None)

        # Ensure copy-mode / clipboard bindings are current on the server
        self._reload_tmux_config(env)

        self._persistent_session = session_name
        self._pid, self._fd = pty.fork()
        if self._pid == 0:
            try:
                os.execvpe("tmux", [
                    "tmux", "-L", self.TMUX_SOCKET,
                    "attach", "-t", session_name,
                ], env)
            except Exception:
                os._exit(127)

        self._p_out = os.fdopen(self._fd, "w+b", 0)
        self._set_pty_size(self._nrow, self._ncol)
        self._read_task = asyncio.create_task(self._read_loop())

    @classmethod
    def tmux_session_alive(cls, session_name: str) -> bool:
        """Check if a persistent tmux session is still running."""
        try:
            result = subprocess.run(
                ["tmux", "-L", cls.TMUX_SOCKET,
                 "has-session", "-t", session_name],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    @classmethod
    def list_tmux_sessions(cls) -> list[str]:
        """Return names of all live sessions on the orch tmux socket."""
        try:
            result = subprocess.run(
                ["tmux", "-L", cls.TMUX_SOCKET,
                 "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return [s.strip() for s in result.stdout.splitlines() if s.strip()]
        except Exception:
            pass
        return []

    def stop_persistent(self) -> None:
        """Kill the tmux session (and thus the child process), then clean up the attach process."""
        name = getattr(self, "_persistent_session", None)
        # Kill the attach client
        self.stop()
        # Kill the tmux session so the actual process dies
        if name:
            try:
                subprocess.run(
                    ["tmux", "-L", self.TMUX_SOCKET,
                     "kill-session", "-t", name],
                    capture_output=True, timeout=3,
                )
            except Exception:
                pass

    def detach_persistent(self) -> None:
        """Detach from the tmux session without killing it.

        The tmux server keeps the process alive.  We just kill the
        local attach client and clean up our PTY state.
        """
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        # Kill just the tmux attach client, not the session
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
                os.waitpid(self._pid, 0)
            except (OSError, ChildProcessError):
                pass
        # Deregister any event-loop reader while the fd is still open, then
        # close the PTY master fd to avoid leaking file descriptors.
        self._release_fd_reader()
        if self._p_out is not None:
            try:
                self._p_out.close()
            except OSError:
                pass
        self._pid = None
        self._fd = None
        self._p_out = None

    # ── PTY I/O ────────────────────────────────────────────────────

    def _set_pty_size(self, rows: int, cols: int) -> None:
        if self._fd is not None:
            winsize = struct.pack("HH", rows, cols)
            try:
                fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def _write_to_pty(self, data: str) -> None:
        if self._p_out is not None:
            try:
                self._p_out.write(data.encode())
            except OSError:
                pass

    def _blocking_pty_read(self) -> bytes | None:
        """Blocking read from the PTY master — runs in a thread pool worker.

        Returns raw bytes, empty bytes on EOF, or None on error.
        """
        try:
            p_out = self._p_out
            if p_out is None:
                return None
            return p_out.read(16384)
        except Exception:
            return None

    async def _read_loop(self) -> None:
        """Read PTY output and feed to terminal backend.

        Uses run_in_executor so the blocking read happens in a thread pool
        worker instead of an add_reader callback.  This means the event loop
        only wakes up when data actually arrives, keeping CPU idle between
        chunks and leaving headroom for keystroke processing.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(
                    None, self._blocking_pty_read
                )
                if not data:
                    # EOF or error — child process exited
                    self._handle_pty_eof()
                    return
                if self._backend:
                    self._process_output_vterm(data)
                else:
                    self._process_output(data.decode(errors="replace"))
                self._has_dirty = True
                # Yield so keystrokes and UI events aren't starved
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass

    def _process_output(self, data: str) -> None:
        """Feed data to pyte and detect mouse tracking changes."""
        for m in _ANSI_SEQ.finditer(data):
            seq = m.group(0)
            if seq.startswith(_DECSET_PREFIX):
                params = seq.removeprefix(_DECSET_PREFIX).split(";")
                if "1000h" in params:
                    self._mouse_tracking = True
                if "1000l" in params:
                    self._mouse_tracking = False
                if "2026h" in params:
                    self._sync_output = True
                if "2026l" in params:
                    self._sync_output = False
        for m in _DECSCUSR.finditer(data):
            ps = int(m.group(1)) if m.group(1) else 0
            self._cursor_shape = 1 if ps <= 2 else (2 if ps <= 4 else 3)
        data = self._seq_filter.feed(data)
        try:
            self._stream.feed(data)
        except Exception:
            pass

    def _scan_osc52(self, data: bytes) -> None:
        """Extract OSC 52 (set-clipboard) escapes from the PTY stream and
        forward the decoded payload to the outer terminal via the
        ``_clipboard_write`` hook.

        libvterm consumes OSC sequences internally with no passthrough,
        so without this scan tmux's set-clipboard emissions never reach
        the user's actual terminal — breaking yank-to-clipboard over
        SSH where local wl-copy/xclip have no display to write to.

        Sequences may straddle PTY reads, so we accumulate a partial
        match in ``self._osc52_buf`` until the terminator (BEL or ST)
        arrives.
        """
        buf = self._osc52_buf + data if self._osc52_buf else data
        self._osc52_buf = b""

        _DEBUG_LOG = "/tmp/orch-osc52.log"

        def _dbg(msg: str) -> None:
            try:
                with open(_DEBUG_LOG, "a") as f:
                    f.write(msg + "\n")
            except Exception:
                pass

        while True:
            start = buf.find(b"\x1b]52;")
            if start < 0:
                return

            _dbg(f"[scan] found OSC 52 prefix at offset {start}, buf len {len(buf)}")
            bel = buf.find(b"\x07", start)
            st = buf.find(b"\x1b\\", start)
            if bel >= 0 and (st < 0 or bel < st):
                end, term_len = bel + 1, 1
            elif st >= 0:
                end, term_len = st + 2, 2
            else:
                # Incomplete — stash the tail and wait for the next chunk.
                # Cap the buffer so a malformed/runaway sequence can't OOM.
                tail = buf[start:]
                if len(tail) > 10_000_000:
                    self._osc52_buf = b""
                else:
                    self._osc52_buf = tail
                return

            seq = buf[start:end]
            _dbg(f"[scan] complete OSC 52 found, seq len {len(seq)}, term_len {term_len}")
            try:
                inner = seq[5:-term_len]  # strip "ESC ] 5 2 ;" and terminator
                # Format: <selection-target>;<base64>.  The target field
                # is often empty (tmux emits ";;<b64>"), so the separator
                # may be at index 0 — accept sep >= 0, not > 0.
                sep = inner.find(b";")
                _dbg(f"[scan] inner len {len(inner)}, sep at {sep}, inner first 20: {inner[:20]!r}")
                if sep >= 0:
                    payload = inner[sep + 1:]
                    text = base64.b64decode(payload, validate=False).decode(
                        "utf-8", errors="replace")
                    _dbg(f"[scan] decoded text len {len(text)}: {text[:60]!r}")
                    self._clipboard_write(text)
                    _dbg("[scan] called _clipboard_write")
            except Exception as e:
                _dbg(f"[scan] EXCEPTION: {e!r}")

            buf = buf[end:]

    def _process_output_vterm(self, data: bytes) -> None:
        """Feed raw bytes to libvterm and detect sync output."""
        # Forward clipboard escapes before libvterm eats them
        self._scan_osc52(data)
        prev_sync = self._sync_output
        if b'\x1b[?2026h' in data:
            self._sync_output = True
        if b'\x1b[?2026l' in data:
            self._sync_output = False
        response = self._backend.feed(data)
        if response and self._p_out:
            try:
                self._p_out.write(response)
            except OSError:
                pass
        self._mouse_tracking = self._backend.mouse_tracking
        # Compensate scroll offset for new scrollback lines
        if self._scroll_offset > 0 and self._backend.new_scrollback_lines:
            self._scroll_offset += self._backend.new_scrollback_lines
            max_offset = len(self._backend.scrollback)
            self._scroll_offset = min(self._scroll_offset, max_offset)
        self._backend.new_scrollback_lines = 0
        # When synchronized output mode ends (2026l = "frame complete"),
        # render immediately rather than waiting for the next tick.
        if prev_sync and not self._sync_output:
            self._on_frame_complete()

    # ── Scrollback ────────────────────────────────────────────────

    def _scroll_up(self, lines: int = 1) -> None:
        """Scroll up into scrollback buffer."""
        if not self._backend or not self._backend.scrollback:
            return
        max_offset = len(self._backend.scrollback)
        self._scroll_offset = min(self._scroll_offset + lines, max_offset)
        self._request_render()

    def _scroll_down(self, lines: int = 1) -> None:
        """Scroll down toward live screen."""
        if self._scroll_offset <= 0:
            return
        self._scroll_offset = max(self._scroll_offset - lines, 0)
        self._request_render()

    def _tmux_copy_mode_nav(self, action: str | None = None) -> None:
        """Enter tmux copy-mode (idempotent) and optionally run a copy-mode command.

        With ``action=None`` this just enters copy-mode (useful as an
        explicit "I want to select on the current screen" trigger).  With
        an action like ``"halfpage-up"`` it scrolls without an extra
        keystroke from the user.
        """
        if not self._persistent_session:
            return
        sess = self._persistent_session
        try:
            subprocess.run(
                ["tmux", "-L", self.TMUX_SOCKET,
                 "copy-mode", "-t", sess],
                capture_output=True, timeout=2,
            )
            if action:
                subprocess.run(
                    ["tmux", "-L", self.TMUX_SOCKET,
                     "send-keys", "-t", sess, "-X", action],
                    capture_output=True, timeout=2,
                )
        except Exception:
            pass

    def search_backward(self, pattern: str) -> bool:
        """Enter copy-mode, jump to history bottom, and literal-search backward.

        Lands the user on the first match above current view so they can
        navigate further with the existing copy-mode bindings (n/N, hjkl,
        v to select, y to yank, q/Esc to leave). Returns False if no
        persistent tmux session is attached or the pattern is empty.
        """
        if not self._persistent_session or not pattern:
            return False
        sess = self._persistent_session
        try:
            subprocess.run(
                ["tmux", "-L", self.TMUX_SOCKET, "copy-mode", "-t", sess],
                capture_output=True, timeout=2,
            )
            subprocess.run(
                ["tmux", "-L", self.TMUX_SOCKET,
                 "send-keys", "-t", sess, "-X", "history-bottom"],
                capture_output=True, timeout=2,
            )
            subprocess.run(
                ["tmux", "-L", self.TMUX_SOCKET,
                 "send-keys", "-t", sess, "-X", "search-backward-text", pattern],
                capture_output=True, timeout=2,
            )
            return True
        except Exception:
            return False
