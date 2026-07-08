"""Terminal input events and decoder for the tui engine.

`InputDecoder` is a pure, clockless, incremental byte-fed state machine:
feed it whatever chunks arrive on stdin and it emits events, buffering
partial escape/UTF-8/paste sequences across feeds. It has no notion of
time — the caller resolves the lone-ESC-vs-alt ambiguity by arming a short
timer when `pending_escape()` is true and calling `flush_escape()` if no
further bytes arrive (this replaces TEXTUAL_ESCAPE_DELAY).

Key names are Textual-compatible so config.py DEFAULT_KEYS and user
config.toml overrides work verbatim on both engines.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyEvent:
    key: str
    char: str | None = None
    raw: bytes = b""  # exact bytes that produced the event (PTY passthrough)


@dataclass(frozen=True)
class MouseEvent:
    kind: str  # "press" | "release" | "move" | "scroll_up" | "scroll_down"
    x: int  # 0-based column
    y: int  # 0-based row
    button: int = -1  # 0=left 1=middle 2=right, -1=none
    shift: bool = False
    alt: bool = False
    ctrl: bool = False
    raw: bytes = b""


@dataclass(frozen=True)
class PasteEvent:
    text: str


Event = KeyEvent | MouseEvent | PasteEvent


# Textual's names for ASCII punctuation (ported from textual.keys so this
# module stays framework-free).
_CHAR_NAMES: dict[str, str] = {
    " ": "space",
    "!": "exclamation_mark",
    '"': "quotation_mark",
    "#": "number_sign",
    "$": "dollar_sign",
    "%": "percent_sign",
    "&": "ampersand",
    "'": "apostrophe",
    "(": "left_parenthesis",
    ")": "right_parenthesis",
    "*": "asterisk",
    "+": "plus",
    ",": "comma",
    "-": "minus",
    ".": "full_stop",
    "/": "slash",
    ":": "colon",
    ";": "semicolon",
    "<": "less_than_sign",
    "=": "equals_sign",
    ">": "greater_than_sign",
    "?": "question_mark",
    "@": "at",
    "[": "left_square_bracket",
    "\\": "backslash",
    "]": "right_square_bracket",
    "^": "circumflex_accent",
    "_": "underscore",
    "`": "grave_accent",
    "{": "left_curly_bracket",
    "|": "vertical_line",
    "}": "right_curly_bracket",
    "~": "tilde",
}

_CTRL_NAMES: dict[int, str] = {i: f"ctrl+{chr(i + 96)}" for i in range(1, 27)}
_CTRL_NAMES.update(
    {
        0x00: "ctrl+@",
        0x09: "tab",
        0x0D: "enter",
        0x1C: "ctrl+backslash",
        0x1D: "ctrl+right_square_bracket",
        0x1E: "ctrl+circumflex_accent",
        0x1F: "ctrl+underscore",
    }
)

_CSI_FINAL_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}

_CSI_TILDE_KEYS = {
    1: "home", 2: "insert", 3: "delete", 4: "end", 5: "pageup", 6: "pagedown",
    7: "home", 8: "end", 11: "f1", 12: "f2", 13: "f3", 14: "f4", 15: "f5",
    17: "f6", 18: "f7", 19: "f8", 20: "f9", 21: "f10", 23: "f11", 24: "f12",
}

_SS3_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
    "P": "f1", "Q": "f2", "R": "f3", "S": "f4", "M": "enter",
}

_CSI_U_FUNCTIONAL = {9: "tab", 13: "enter", 27: "escape", 127: "backspace"}

_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_MAX_CSI_PARAMS = 64  # parameter bytes before a sequence is called garbage


def _char_key_name(ch: str) -> str:
    """Textual-compatible key name for a single character."""
    if ch.isalnum():
        return ch
    name = _CHAR_NAMES.get(ch)
    if name is not None:
        return name
    try:
        return unicodedata.name(ch).lower().replace("-", "_").replace(" ", "_")
    except ValueError:
        return ch


def _with_mods(name: str, mods: int) -> str:
    """Apply an xterm/kitty modifier param (mods-1 bitmask: 1=shift 2=alt 4=ctrl)."""
    bits = mods - 1
    tokens = []
    if bits & 1:
        tokens.append("shift")
    if bits & 2:
        tokens.append("alt")
    if bits & 4:
        tokens.append("ctrl")
    if not tokens:
        return name
    tokens.sort()  # Textual sorts modifier tokens alphabetically: alt < ctrl < shift
    return "+".join(tokens + [name])


def _utf8_len(lead: int) -> int:
    """Total byte length of a UTF-8 char from its lead byte (0 = invalid lead)."""
    if lead < 0x80:
        return 1
    if lead < 0xC0:
        return 0
    if lead < 0xE0:
        return 2
    if lead < 0xF0:
        return 3
    if lead < 0xF8:
        return 4
    return 0


def _decode_csi_u(params: str, raw: bytes) -> KeyEvent | None:
    """kitty CSI-u: ESC [ codepoint[:alternates] ; mods[:event-type] u"""
    fields = params.split(";")
    try:
        codepoint = int(fields[0].split(":")[0])
    except (ValueError, IndexError):
        return None
    mods = 1
    event_type = 1
    if len(fields) > 1 and fields[1]:
        sub = fields[1].split(":")
        try:
            mods = int(sub[0]) if sub[0] else 1
            if len(sub) > 1 and sub[1]:
                event_type = int(sub[1])
        except ValueError:
            return None
    if event_type == 3:
        return None  # key release
    name = _CSI_U_FUNCTIONAL.get(codepoint)
    char = None
    if name is None:
        try:
            ch = chr(codepoint)
        except ValueError:
            return None
        name = _char_key_name(ch).lower()
        if mods == 1 and codepoint >= 0x20 and codepoint != 0x7F:
            char = ch
    return KeyEvent(_with_mods(name, mods), char, raw)


def _decode_sgr_mouse(params: str, final: str, raw: bytes) -> MouseEvent | None:
    """SGR mouse: ESC [ < b ; x ; y M/m — x/y converted to 0-based."""
    try:
        b_s, x_s, y_s = params[1:].split(";")
        b, x, y = int(b_s), int(x_s), int(y_s)
    except ValueError:
        return None
    shift = bool(b & 4)
    alt = bool(b & 8)
    ctrl = bool(b & 16)
    button = b & 3
    if b & 64:
        if button == 0:
            kind = "scroll_up"
        elif button == 1:
            kind = "scroll_down"
        else:
            return None  # horizontal scroll: unused
        button = -1
    elif b & 32:
        kind = "move"
        if button == 3:
            button = -1
    else:
        kind = "press" if final == "M" else "release"
        if button == 3:
            button = -1
    return MouseEvent(kind, x - 1, y - 1, button, shift, alt, ctrl, raw)


class InputDecoder:
    def __init__(self) -> None:
        self._buf = b""
        self._pasting = False
        self._paste_buf = b""

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        return self._drain()

    def pending_escape(self) -> bool:
        """True when the buffer holds an unresolved ESC-led prefix."""
        return not self._pasting and self._buf.startswith(b"\x1b")

    def flush_escape(self) -> list[Event]:
        """Resolve a pending ESC-led prefix after the caller's grace timer.

        Lone ESC becomes "escape"; ESC + one char becomes alt+char; a longer
        abandoned sequence becomes "escape" plus its remaining bytes
        re-decoded as ordinary input.
        """
        if not self.pending_escape():
            return []
        buf = self._buf
        if buf == b"\x1b":
            self._buf = b""
            return [KeyEvent("escape", None, b"\x1b")]
        if len(buf) == 2 and buf[1] < 0x80:
            events: list[Event] = []
            consumed = self._decode_alt(events)
            self._buf = self._buf[consumed:]
            return events
        self._buf = buf[1:]
        events = [KeyEvent("escape", None, b"\x1b")]
        events.extend(self._drain())
        return events

    # ── internals ─────────────────────────────────────────────────

    def _drain(self) -> list[Event]:
        events: list[Event] = []
        while True:
            if self._pasting:
                if not self._drain_paste(events):
                    break
                continue
            if not self._buf:
                break
            consumed = self._decode_one(events)
            if consumed == 0:
                break  # incomplete sequence: wait for more bytes
            self._buf = self._buf[consumed:]
        return events

    def _drain_paste(self, events: list[Event]) -> bool:
        """Accumulate paste bytes; emit PasteEvent when the terminator arrives.

        Returns True if the paste closed (buffered input may follow it).
        """
        self._paste_buf += self._buf
        self._buf = b""
        idx = self._paste_buf.find(_PASTE_END)
        if idx < 0:
            return False
        text = self._paste_buf[:idx].decode("utf-8", errors="replace")
        self._buf = self._paste_buf[idx + len(_PASTE_END):]
        self._paste_buf = b""
        self._pasting = False
        events.append(PasteEvent(text))
        return True

    def _decode_one(self, events: list[Event]) -> int:
        """Decode one event from the front of the buffer; return bytes consumed
        (0 = incomplete). Invalid bytes are consumed without emitting."""
        buf = self._buf
        b0 = buf[0]
        if b0 == 0x1B:
            return self._decode_escape(events)
        if b0 in (0x7F, 0x08):
            # Both mean backspace; char preserves the original byte — the
            # \x7f-vs-\x08 distinction is load-bearing in pickers.
            events.append(KeyEvent("backspace", chr(b0), bytes([b0])))
            return 1
        if b0 < 0x20:
            events.append(KeyEvent(_CTRL_NAMES[b0], chr(b0), bytes([b0])))
            return 1
        n = _utf8_len(b0)
        if n == 0:
            return 1  # invalid lead byte: drop and resync
        if len(buf) < n:
            # Wait only while the bytes so far could still become a valid
            # char; a non-continuation byte means they never will.
            if all(0x80 <= b <= 0xBF for b in buf[1:n]):
                return 0
            return 1
        raw = buf[:n]
        try:
            ch = raw.decode("utf-8")
        except UnicodeDecodeError:
            return 1
        events.append(KeyEvent(_char_key_name(ch), ch, raw))
        return n

    def _decode_escape(self, events: list[Event]) -> int:
        buf = self._buf
        if len(buf) == 1:
            return 0  # lone ESC: ambiguous until flush_escape()
        b1 = buf[1]
        if b1 == 0x5B:  # [
            return self._decode_csi(events)
        if b1 == 0x4F:  # O
            return self._decode_ss3(events)
        if b1 == 0x1B:
            events.append(KeyEvent("escape", None, b"\x1b"))
            return 1  # second ESC re-examined on the next pass
        return self._decode_alt(events)

    def _decode_alt(self, events: list[Event]) -> int:
        buf = self._buf
        b1 = buf[1]
        if b1 in (0x7F, 0x08):
            events.append(KeyEvent("alt+backspace", None, buf[:2]))
            return 2
        if b1 < 0x20:
            events.append(KeyEvent(f"alt+{_CTRL_NAMES[b1]}", None, buf[:2]))
            return 2
        n = _utf8_len(b1)
        if n == 0:
            return 2  # ESC + invalid byte: drop both
        if len(buf) < 1 + n:
            if all(0x80 <= b <= 0xBF for b in buf[2 : 1 + n]):
                return 0
            return 2
        raw = buf[: 1 + n]
        try:
            ch = raw[1:].decode("utf-8")
        except UnicodeDecodeError:
            return 2
        name = _char_key_name(ch)
        if len(name) == 1 and name.isupper():
            name = f"shift+{name.lower()}"  # mirrors Textual's alt+uppercase naming
        events.append(KeyEvent(f"alt+{name}", ch, raw))
        return 1 + n

    def _decode_csi(self, events: list[Event]) -> int:
        buf = self._buf
        i = 2
        n = len(buf)
        while i < n and 0x20 <= buf[i] <= 0x3F:
            i += 1
            if i - 2 > _MAX_CSI_PARAMS:
                return i  # runaway parameter string: drop it
        if i >= n:
            return 0  # incomplete CSI
        final = buf[i]
        if not (0x40 <= final <= 0x7E):
            return i + 1  # malformed: drop
        params = buf[2:i].decode("ascii", errors="replace")
        self._dispatch_csi(params, chr(final), buf[: i + 1], events)
        return i + 1

    def _dispatch_csi(
        self, params: str, final: str, raw: bytes, events: list[Event]
    ) -> None:
        if final == "~":
            if params == "200":
                self._pasting = True
                return
            if params == "201":
                return  # stray paste terminator
            head, _, mod_s = params.partition(";")
            try:
                num = int(head)
                mods = int(mod_s.split(":")[0]) if mod_s else 1
            except ValueError:
                return
            name = _CSI_TILDE_KEYS.get(num)
            if name is not None:
                events.append(KeyEvent(_with_mods(name, mods), None, raw))
            return
        if final in "Mm" and params.startswith("<"):
            mouse = _decode_sgr_mouse(params, final, raw)
            if mouse is not None:
                events.append(mouse)
            return
        if final == "u":
            key = _decode_csi_u(params, raw)
            if key is not None:
                events.append(key)
            return
        if final == "Z":
            events.append(KeyEvent("shift+tab", None, raw))
            return
        name = _CSI_FINAL_KEYS.get(final)
        if name is not None:
            mods = 1
            _, _, mod_s = params.partition(";")
            if mod_s:
                try:
                    mods = int(mod_s.split(":")[0])
                except ValueError:
                    mods = 1
            events.append(KeyEvent(_with_mods(name, mods), None, raw))
        # anything else: unknown sequence, drop

    def _decode_ss3(self, events: list[Event]) -> int:
        buf = self._buf
        if len(buf) < 3:
            return 0
        name = _SS3_KEYS.get(chr(buf[2]))
        if name is not None:
            events.append(KeyEvent(name, None, buf[:3]))
        return 3
