"""Real-terminal I/O — the ONLY tui module that touches the TTY.

TermIO's method set is the engine's terminal interface. It is duck-typed
(no ABC): tui/testing.py's FakeTermIO mirrors it for headless tests.

    enter_raw_alt()            raw termios + alt screen + hidden cursor +
                               bracketed paste + SGR mouse reporting
    exit_alt_cooked()          inverse of enter_raw_alt (for App.suspend)
    restore()                  idempotent full restore for crash paths
    write(data: bytes)         raw bytes to the terminal
    get_size() -> (cols, rows)
    input_fileno() -> int | None   None = no readable fd (headless)
    copy_to_clipboard(text)    OSC 52 to the outer terminal
"""

from __future__ import annotations

import base64
import os
import termios
import tty

# Alt screen, hidden cursor, bracketed paste, SGR mouse (1006) with drag
# reporting (1002). _EXIT_SEQ is the exact inverse, in reverse order.
_ENTER_SEQ = b"\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[?1006h\x1b[?1002h"
_EXIT_SEQ = b"\x1b[?1002l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[?1049l"


def osc52_sequence(text: str, tmux: bool) -> bytes:
    """OSC 52 clipboard-set sequence; tmux passthrough-wrapped (with ESC
    bytes doubled) when writing from inside tmux."""
    payload = b"\x1b]52;c;" + base64.b64encode(text.encode("utf-8")) + b"\x07"
    if tmux:
        return b"\x1bPtmux;" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
    return payload


def _is_tty(fd: int) -> bool:
    try:
        return os.isatty(fd)
    except OSError:
        return False


class TermIO:
    def __init__(self) -> None:
        self._in_fd: int | None = 0 if _is_tty(0) else None
        if _is_tty(1):
            self._out_fd = 1
        elif self._in_fd is not None:
            self._out_fd = self._in_fd  # stdout redirected: paint to the tty
        else:
            self._out_fd = 1
        self._saved: list | None = None  # termios attrs before raw mode
        self._entered = False  # alt-screen/input modes currently active

    # ── modes ─────────────────────────────────────────────────────

    def enter_raw_alt(self) -> None:
        if self._in_fd is not None and self._saved is None:
            self._saved = termios.tcgetattr(self._in_fd)
            tty.setraw(self._in_fd)
        self.write(_ENTER_SEQ)
        self._entered = True

    def exit_alt_cooked(self) -> None:
        if self._entered:
            self.write(_EXIT_SEQ)
            self._entered = False
        if self._saved is not None and self._in_fd is not None:
            termios.tcsetattr(self._in_fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def restore(self) -> None:
        """Crash-path restore: safe to call twice, safe if enter never ran."""
        try:
            self.exit_alt_cooked()
        except (OSError, termios.error):
            pass

    # ── I/O ───────────────────────────────────────────────────────

    def write(self, data: bytes) -> None:
        fd = self._out_fd
        while data:
            data = data[os.write(fd, data):]

    def get_size(self) -> tuple[int, int]:
        fd = self._in_fd if self._in_fd is not None else self._out_fd
        try:
            size = os.get_terminal_size(fd)
            return (size.columns, size.lines)
        except OSError:
            return (80, 24)

    def input_fileno(self) -> int | None:
        return self._in_fd

    def copy_to_clipboard(self, text: str) -> None:
        try:
            self.write(osc52_sequence(text, "TMUX" in os.environ))
        except OSError:
            pass
