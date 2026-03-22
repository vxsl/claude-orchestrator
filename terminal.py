"""Embedded terminal emulator widget for Textual.

Uses pyte for VT100 emulation and ptyprocess for PTY management.
Renders the terminal screen via Textual's render_line / Strip API
for efficient partial updates.

Based on mitosch/textual-terminal, modernized for Textual 8.x.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
from pathlib import Path

import pyte
from pyte.screens import Char
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget


# ── pyte subclasses ───────────────────────────────────────────────

class _Screen(pyte.Screen):
    """Pyte screen with extra tolerance for modern terminal sequences."""

    def set_margins(self, *args, **kwargs):
        kwargs.pop("private", None)
        return super().set_margins(*args, **kwargs)

    def _ignore(self, *args, **kwargs):
        """Silently ignore unsupported sequences."""
        pass


class _Stream(pyte.Stream):
    """Pyte stream with additional CSI handlers for tmux compat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # CSI s/u — cursor save/restore (ANSI.SYS style, used by tmux)
        self.csi["s"] = "save_cursor"
        self.csi["u"] = "restore_cursor"
        # CSI t — window manipulation (xterm); ignore
        self.csi["t"] = "_ignore"
        # CSI q — cursor shape; ignore
        self.csi["q"] = "_ignore"


# ── Escape sequence filter ─────────────────────────────────────────

# CSI sequences with intermediate bytes pyte can't parse:
# =/>/<  (kitty keyboard, DA2, etc.)
# space  (cursor shape \x1b[0 q, etc.)
_STRIP_CSI_EXT = re.compile(
    r"\x1b\[\??[\d;]*[=><][\d;]*[a-zA-Z]"
    r"|\x1b\[\??[\d;]* [a-zA-Z]"
)


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


# ── Widget ─────────────────────────────────────────────────────────

class TerminalWidget(Widget, can_focus=True):
    """A terminal emulator widget that runs a command in a PTY."""

    DEFAULT_CSS = """
    TerminalWidget {
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(
        self,
        command: str = "bash",
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        passthrough_keys: set[str] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._command = command
        self._extra_env = env or {}
        self._cwd = cwd
        self._passthrough_keys = passthrough_keys or set()
        self._ncol = 80
        self._nrow = 24
        self._screen = _Screen(self._ncol, self._nrow)
        self._stream = _Stream(self._screen)
        self._seq_filter = _SeqFilter()
        self._mouse_tracking = False

        # PTY state
        self._pid: int | None = None
        self._fd: int | None = None
        self._p_out = None
        self._read_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Fork the PTY and begin reading."""
        if self._pid is not None:
            return

        self._pid, self._fd = pty.fork()
        if self._pid == 0:
            # Child — exec the command.
            # Safety: if exec fails, _exit immediately so we never
            # fall through into the parent's Textual event loop.
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
        self._pid = None
        self._fd = None
        self._p_out = None

    def detach(self) -> dict | None:
        """Stop reading but keep the process alive.  Returns state dict
        that can be passed to ``attach()`` on a new widget instance."""
        if self._pid is None:
            return None
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        state = {
            "pid": self._pid,
            "fd": self._fd,
            "p_out": self._p_out,
            "screen": self._screen,
            "stream": self._stream,
            "seq_filter": self._seq_filter,
        }
        # Neuter so on_unmount → stop() won't kill the process
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
        self._screen = state["screen"]
        self._stream = state["stream"]
        self._seq_filter = state["seq_filter"]
        # Resize pyte + PTY to match current widget size
        self._screen.resize(self._nrow, self._ncol)
        self._set_pty_size(self._nrow, self._ncol)
        self._read_task = asyncio.create_task(self._read_loop())

    def on_unmount(self) -> None:
        self.stop()

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

    async def _read_loop(self) -> None:
        """Read PTY output and feed to pyte."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        event = asyncio.Event()

        def _on_output():
            try:
                data = self._p_out.read(65536).decode(errors="replace")
                queue.put_nowait(data)
                event.set()
            except Exception:
                queue.put_nowait(None)
                event.set()

        loop.add_reader(self._p_out, _on_output)
        try:
            while True:
                await event.wait()
                event.clear()
                while not queue.empty():
                    data = queue.get_nowait()
                    if data is None:
                        self.post_message(self.Finished())
                        return
                    self._process_output(data)
                self.refresh()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                loop.remove_reader(self._p_out)
            except Exception:
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
        data = self._seq_filter.feed(data)
        try:
            self._stream.feed(data)
        except Exception:
            pass

    # ── Rendering ──────────────────────────────────────────────────

    def get_content_width(self, container, viewport):
        return self._ncol

    def get_content_height(self, container, viewport, width):
        return self._nrow

    def render_line(self, y: int) -> Strip:
        """Render a single terminal line as a Textual Strip."""
        if y >= self._screen.lines:
            return Strip.blank(self._ncol)

        line = self._screen.buffer[y]
        cursor_x = self._screen.cursor.x if self._screen.cursor.y == y else -1

        segments: list[Segment] = []
        run_text: list[str] = []
        run_style: Style | None = None

        def _flush():
            nonlocal run_text, run_style
            if run_text:
                segments.append(Segment("".join(run_text), run_style or Style()))
                run_text = []

        for x in range(self._screen.columns):
            char: Char = line[x]

            if x == cursor_x and self.has_focus:
                style = Style(reverse=True)
            else:
                style = _char_style(char)

            if style != run_style:
                _flush()
                run_style = style

            run_text.append(char.data)

        _flush()

        return Strip(segments, self._ncol)

    # ── Input ──────────────────────────────────────────────────────

    async def on_key(self, event: events.Key) -> None:
        if self._pid is None:
            return

        if event.key in self._passthrough_keys:
            return  # Let it bubble to parent screen

        event.stop()
        event.prevent_default()

        key = event.key

        # ctrl+letter → control character
        if key.startswith("ctrl+") and len(key) == 6:
            letter = key[-1]
            if letter.isalpha():
                code = ord(letter.lower()) - ord("a") + 1
                self._write_to_pty(chr(code))
                return

        mapped = _KEY_MAP.get(key)
        if mapped:
            self._write_to_pty(mapped)
            return

        if event.character:
            self._write_to_pty(event.character)

    async def on_paste(self, event: events.Paste) -> None:
        if self._pid is None:
            return
        event.stop()
        event.prevent_default()
        # Use bracketed paste mode so the terminal app knows it's a paste
        self._write_to_pty(f"\x1b[200~{event.text}\x1b[201~")

    async def on_resize(self, event: events.Resize) -> None:
        self._ncol = self.size.width
        self._nrow = self.size.height
        self._screen.resize(self._nrow, self._ncol)
        self._set_pty_size(self._nrow, self._ncol)
        self.refresh()

    async def on_click(self, event: events.Click) -> None:
        if not self._mouse_tracking or self._pid is None:
            return
        x, y = event.x + 1, event.y + 1
        self._write_to_pty(f"\x1b[<0;{x};{y}M")
        self._write_to_pty(f"\x1b[<0;{x};{y}m")

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not self._mouse_tracking or self._pid is None:
            return
        x, y = event.x + 1, event.y + 1
        self._write_to_pty(f"\x1b[<65;{x};{y}M")

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not self._mouse_tracking or self._pid is None:
            return
        x, y = event.x + 1, event.y + 1
        self._write_to_pty(f"\x1b[<64;{x};{y}M")

    # ── Messages ───────────────────────────────────────────────────

    class Finished(Message):
        """Posted when the subprocess exits."""
        pass
