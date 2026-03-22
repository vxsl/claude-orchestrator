"""libvterm-based terminal backend via ctypes.

Provides complete VT terminal emulation using libvterm (the same library
used by neovim). Handles ALL modern escape sequences including alternate
screen buffer, synchronized output, mouse tracking, wide characters,
kitty keyboard protocol, etc.

If libvterm is not available, importing this module raises ImportError
and the caller should fall back to the pyte-based backend.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import (
    CFUNCTYPE, POINTER, Structure,
    c_char_p, c_int, c_size_t, c_uint8, c_uint32, c_void_p,
)

# ── Load library ──────────────────────────────────────────────────

_lib_path = ctypes.util.find_library("vterm")
if not _lib_path:
    raise ImportError("libvterm not found")

_lib = ctypes.CDLL(_lib_path)


# ── Structs ───────────────────────────────────────────────────────

class VTermPos(Structure):
    _fields_ = [("row", c_int), ("col", c_int)]


class VTermRect(Structure):
    _fields_ = [
        ("start_row", c_int), ("end_row", c_int),
        ("start_col", c_int), ("end_col", c_int),
    ]


class VTermColor(Structure):
    _fields_ = [
        ("type", c_uint8),
        ("red", c_uint8),   # also 'idx' when indexed
        ("green", c_uint8),
        ("blue", c_uint8),
    ]


class VTermScreenCell(Structure):
    _fields_ = [
        ("chars", c_uint32 * 6),   # VTERM_MAX_CHARS_PER_CELL
        ("width", c_uint8),
        ("_pad", c_uint8 * 3),
        ("attrs", c_uint32),       # bitfield packed into uint32
        ("fg", VTermColor),
        ("bg", VTermColor),
    ]


# ── Callback types ────────────────────────────────────────────────

_damage_cb = CFUNCTYPE(c_int, VTermRect, c_void_p)
_moverect_cb = CFUNCTYPE(c_int, VTermRect, VTermRect, c_void_p)
_movecursor_cb = CFUNCTYPE(c_int, VTermPos, VTermPos, c_int, c_void_p)
_settermprop_cb = CFUNCTYPE(c_int, c_int, c_void_p, c_void_p)
_bell_cb = CFUNCTYPE(c_int, c_void_p)
_resize_cb = CFUNCTYPE(c_int, c_int, c_int, c_void_p)
_sb_pushline_cb = CFUNCTYPE(c_int, c_int, c_void_p, c_void_p)
_sb_popline_cb = CFUNCTYPE(c_int, c_int, c_void_p, c_void_p)
_sb_clear_cb = CFUNCTYPE(c_int, c_void_p)


class VTermScreenCallbacks(Structure):
    _fields_ = [
        ("damage", _damage_cb),
        ("moverect", _moverect_cb),
        ("movecursor", _movecursor_cb),
        ("settermprop", _settermprop_cb),
        ("bell", _bell_cb),
        ("resize", _resize_cb),
        ("sb_pushline", _sb_pushline_cb),
        ("sb_popline", _sb_popline_cb),
        ("sb_clear", _sb_clear_cb),
    ]


# ── Function signatures ──────────────────────────────────────────

def _sig(name, restype, argtypes):
    fn = getattr(_lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_vterm_new = _sig("vterm_new", c_void_p, [c_int, c_int])
_vterm_free = _sig("vterm_free", None, [c_void_p])
_vterm_set_size = _sig("vterm_set_size", None, [c_void_p, c_int, c_int])
_vterm_input_write = _sig("vterm_input_write", c_size_t,
                          [c_void_p, c_char_p, c_size_t])
_vterm_output_read = _sig("vterm_output_read", c_size_t,
                          [c_void_p, c_char_p, c_size_t])
_vterm_obtain_screen = _sig("vterm_obtain_screen", c_void_p, [c_void_p])
_vterm_obtain_state = _sig("vterm_obtain_state", c_void_p, [c_void_p])
_vterm_screen_reset = _sig("vterm_screen_reset", None, [c_void_p, c_int])
_vterm_screen_enable_altscreen = _sig("vterm_screen_enable_altscreen", None,
                                      [c_void_p, c_int])
_vterm_screen_get_cell = _sig("vterm_screen_get_cell", c_int,
                              [c_void_p, VTermPos, POINTER(VTermScreenCell)])
_vterm_screen_set_callbacks = _sig("vterm_screen_set_callbacks", None,
                                   [c_void_p, POINTER(VTermScreenCallbacks),
                                    c_void_p])
_vterm_screen_set_damage_merge = _sig("vterm_screen_set_damage_merge", None,
                                      [c_void_p, c_int])
_vterm_screen_flush_damage = _sig("vterm_screen_flush_damage", None,
                                  [c_void_p])
_vterm_screen_convert_color_to_rgb = _sig(
    "vterm_screen_convert_color_to_rgb", None,
    [c_void_p, POINTER(VTermColor)])
_vterm_set_utf8 = _sig("vterm_set_utf8", None, [c_void_p, c_int])

# Constants
_DAMAGE_ROW = 1
_PROP_MOUSE = 8


# ── Backend class ─────────────────────────────────────────────────

class VTermBackend:
    """Terminal emulation backend using libvterm.

    Handles all modern VT escape sequences. Used by TerminalWidget
    as a drop-in replacement for the pyte-based backend.
    """

    def __init__(self, cols: int, rows: int) -> None:
        self._vt = _vterm_new(rows, cols)
        self._screen = _vterm_obtain_screen(self._vt)
        self._state = _vterm_obtain_state(self._vt)
        self.lines = rows
        self.columns = cols
        self.cursor_y = 0
        self.cursor_x = 0
        self.mouse_tracking = False

        # Prevent GC of callback pointers
        self._cb_damage = _damage_cb(self._on_damage)
        self._cb_movecursor = _movecursor_cb(self._on_movecursor)
        self._cb_settermprop = _settermprop_cb(self._on_settermprop)
        self._callbacks = VTermScreenCallbacks(
            damage=self._cb_damage,
            movecursor=self._cb_movecursor,
            settermprop=self._cb_settermprop,
        )
        _vterm_screen_set_callbacks(
            self._screen, ctypes.byref(self._callbacks), None)

        _vterm_set_utf8(self._vt, 1)
        _vterm_screen_enable_altscreen(self._screen, 1)
        _vterm_screen_set_damage_merge(self._screen, _DAMAGE_ROW)
        _vterm_screen_reset(self._screen, 1)

        # Reusable structs to avoid per-call allocation
        self._cell = VTermScreenCell()
        self._pos = VTermPos()
        self._ctmp = VTermColor()
        self._output_buf = ctypes.create_string_buffer(4096)

    def __del__(self) -> None:
        vt = getattr(self, "_vt", None)
        if vt:
            _vterm_free(vt)
            self._vt = None

    # ── Callbacks ──

    def _on_damage(self, rect, user):
        return 0

    def _on_movecursor(self, pos, oldpos, visible, user):
        self.cursor_y = pos.row
        self.cursor_x = pos.col
        return 0

    def _on_settermprop(self, prop, val, user):
        if prop == _PROP_MOUSE and val:
            self.mouse_tracking = ctypes.cast(val, POINTER(c_int))[0] > 0
        return 0

    # ── Public API ──

    def feed(self, data: bytes) -> bytes:
        """Feed raw PTY output. Returns any response to write back to PTY."""
        if not data:
            return b""
        _vterm_input_write(self._vt, data, len(data))
        _vterm_screen_flush_damage(self._screen)
        n = _vterm_output_read(self._vt, self._output_buf, 4096)
        return self._output_buf.raw[:n] if n > 0 else b""

    def resize(self, rows: int, cols: int) -> None:
        self.lines = rows
        self.columns = cols
        _vterm_set_size(self._vt, rows, cols)

    def get_cell(self, row: int, col: int) -> VTermScreenCell:
        """Read cell at (row, col). Returns internal reusable struct —
        copy data before calling again."""
        self._pos.row = row
        self._pos.col = col
        _vterm_screen_get_cell(
            self._screen, self._pos, ctypes.byref(self._cell))
        return self._cell

    def cell_char(self, cell: VTermScreenCell) -> str:
        cp = cell.chars[0]
        if cp == 0:
            return " "
        parts = [chr(cp)]
        for i in range(1, 6):
            cp = cell.chars[i]
            if cp == 0:
                break
            parts.append(chr(cp))
        return "".join(parts)

    def color_to_rich(self, color: VTermColor) -> str | None:
        """Convert VTermColor to a Rich color string, or None for default."""
        t = color.type
        if t & 0x02 or t & 0x04:  # DEFAULT_FG / DEFAULT_BG
            return None
        if t & 0x01:  # INDEXED — convert palette index to RGB
            self._ctmp.type = t
            self._ctmp.red = color.red
            self._ctmp.green = color.green
            self._ctmp.blue = color.blue
            _vterm_screen_convert_color_to_rgb(
                self._screen, ctypes.byref(self._ctmp))
            return f"#{self._ctmp.red:02x}{self._ctmp.green:02x}{self._ctmp.blue:02x}"
        return f"#{color.red:02x}{color.green:02x}{color.blue:02x}"
