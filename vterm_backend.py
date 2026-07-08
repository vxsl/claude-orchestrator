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
import struct
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
_PROP_CURSORSHAPE = 7   # VTERM_PROP_CURSORSHAPE: 1=block, 2=underline, 3=bar
_PROP_MOUSE = 8
_CELL_SIZE = ctypes.sizeof(VTermScreenCell)

# Cache-miss sentinel (None is a legitimate cached value: default color)
_MISS = object()

# One-shot VTermScreenCell parse for the render hot loop, reading the ctypes
# struct's buffer directly: chars[0], width, attrs, fg bytes, bg bytes.
_CELL_PARSE = struct.Struct("<I20xB3xI4s4s").unpack_from
assert struct.calcsize("<I20xB3xI4s4s") == _CELL_SIZE


# ── Backend class ─────────────────────────────────────────────────

class VTermBackend:
    """Terminal emulation backend using libvterm.

    Handles all modern VT escape sequences. Used by TerminalWidget
    as a drop-in replacement for the pyte-based backend.
    """

    MAX_SCROLLBACK = 10000  # ~32MB max at 80 cols
    MOVES_CAP = 256  # unconsumed row-moves before overflow (see __init__)

    def __init__(self, cols: int, rows: int) -> None:
        # Init tracking state before callbacks can fire
        self.dirty_rows: set[int] = set()
        self.scrollback: list[tuple[int, bytes]] = []
        self.new_scrollback_lines = 0  # lines pushed since last check

        # Row-move tracking (opt-in, used by the tui engine's TerminalPane
        # run cache). When False — the default, and always for the Textual
        # widget — the moverect callback declines the event and libvterm
        # damages the destination rows exactly as before. When True,
        # full-width moves are recorded in `pending_moves` as
        # (dest_start, dest_end, delta) with delta = dest - src row, and
        # already-dirty rows inside the source span are renumbered, so a
        # scroll costs a cache shift instead of a full-grid re-render.
        # Past MOVES_CAP unconsumed moves (a firehose stream between
        # paints), moves_overflow is raised and tracking falls back to
        # damage until the consumer clears the backlog — per-scroll
        # bookkeeping for a cache that will be dropped anyway is waste.
        self.track_moves = False
        self.moves_overflow = False
        self.pending_moves: list[tuple[int, int, int]] = []

        self._vt = _vterm_new(rows, cols)
        self._screen = _vterm_obtain_screen(self._vt)
        self._state = _vterm_obtain_state(self._vt)
        self.lines = rows
        self.columns = cols
        self.cursor_y = 0
        self.cursor_x = 0
        self.cursor_shape = 1  # 1=block, 2=underline, 3=bar (VTERM_PROP_CURSORSHAPE)
        self.mouse_tracking = False

        # Prevent GC of callback pointers
        self._cb_damage = _damage_cb(self._on_damage)
        self._cb_moverect = _moverect_cb(self._on_moverect)
        self._cb_movecursor = _movecursor_cb(self._on_movecursor)
        self._cb_settermprop = _settermprop_cb(self._on_settermprop)
        self._cb_pushline = _sb_pushline_cb(self._on_sb_pushline)
        self._cb_popline = _sb_popline_cb(self._on_sb_popline)
        self._callbacks = VTermScreenCallbacks(
            damage=self._cb_damage,
            moverect=self._cb_moverect,
            movecursor=self._cb_movecursor,
            settermprop=self._cb_settermprop,
            sb_pushline=self._cb_pushline,
            sb_popline=self._cb_popline,
        )
        _vterm_screen_set_callbacks(
            self._screen, ctypes.byref(self._callbacks), None)

        _vterm_set_utf8(self._vt, 1)
        _vterm_screen_enable_altscreen(self._screen, 1)
        _vterm_screen_set_damage_merge(self._screen, _DAMAGE_ROW)
        _vterm_screen_reset(self._screen, 1)

        # Dirty row tracking from damage callback
        self.dirty_rows: set[int] = set()

        # Reusable structs to avoid per-call allocation
        self._cell = VTermScreenCell()
        self._pos = VTermPos()
        self._ctmp = VTermColor()
        self._output_buf = ctypes.create_string_buffer(4096)

        # (type, r, g, b) color bytes -> Rich color string | None. Keyed on
        # the raw 4-byte VTermColor so the render hot loop never touches
        # ctypes struct fields. (An OSC 4 palette redefinition mid-stream
        # would go stale here — nothing we run does that.)
        self._color_cache: dict[bytes, str | None] = {}

    def __del__(self) -> None:
        vt = getattr(self, "_vt", None)
        if vt:
            _vterm_free(vt)
            self._vt = None

    # ── Callbacks ──

    def _on_damage(self, rect, user):
        for row in range(rect.start_row, rect.end_row):
            self.dirty_rows.add(row)
        return 0

    def _on_moverect(self, dest, src, user):
        """Screen content moved (scroll). Returning 0 declines the event and
        libvterm damages the destination rows (the pre-tracking behavior);
        returning 1 means the embedder took responsibility for the move."""
        if not self.track_moves or self.moves_overflow:
            return 0
        if len(self.pending_moves) >= self.MOVES_CAP:
            self.moves_overflow = True
            return 0
        cols = self.columns
        if (src.start_col != 0 or src.end_col < cols
                or dest.start_col != 0 or dest.end_col < cols):
            return 0  # partial-width move (side margins): damage handles it
        delta = dest.start_row - src.start_row
        if delta == 0:
            return 1
        # Renumber pending dirty rows that live in the moved source span so
        # they refer to the rows' new positions.
        if self.dirty_rows:
            start, end = src.start_row, src.end_row
            moved = {r + delta for r in self.dirty_rows if start <= r < end}
            kept = {r for r in self.dirty_rows if not (start <= r < end)}
            lines = self.lines
            self.dirty_rows = kept | {r for r in moved if 0 <= r < lines}
        self.pending_moves.append((dest.start_row, dest.end_row, delta))
        return 1

    def _on_movecursor(self, pos, oldpos, visible, user):
        # Cursor moves don't trigger damage events, so mark the old and
        # new rows dirty ourselves — otherwise tmux copy-mode cursor
        # navigation (k/j/h/l) leaves a stale cursor block at the prior
        # position until some unrelated cell change repaints the row.
        if oldpos.row != pos.row or oldpos.col != pos.col:
            if 0 <= oldpos.row < self.lines:
                self.dirty_rows.add(oldpos.row)
            if 0 <= pos.row < self.lines:
                self.dirty_rows.add(pos.row)
        self.cursor_y = pos.row
        self.cursor_x = pos.col
        return 0

    def _on_settermprop(self, prop, val, user):
        if prop == _PROP_CURSORSHAPE and val:
            self.cursor_shape = ctypes.cast(val, POINTER(c_int))[0]
            self.dirty_rows.add(self.cursor_y)  # shape change has no damage event
        elif prop == _PROP_MOUSE and val:
            self.mouse_tracking = ctypes.cast(val, POINTER(c_int))[0] > 0
        return 0

    def _on_sb_pushline(self, cols, cells_ptr, user):
        """Called when a line scrolls off the top of the screen.
        Stores a raw memcpy of the VTermScreenCell array — no per-cell
        Python object creation."""
        nbytes = cols * _CELL_SIZE
        raw = ctypes.string_at(cells_ptr, nbytes)
        self.scrollback.append((cols, raw))
        self.new_scrollback_lines += 1
        if len(self.scrollback) > self.MAX_SCROLLBACK:
            del self.scrollback[0]
        return 1

    def _on_sb_popline(self, cols, cells_ptr, user):
        """Called when libvterm wants to restore a scrollback line."""
        if not self.scrollback:
            return 0
        stored_cols, raw = self.scrollback.pop()
        # Copy back as many cells as we can
        copy_bytes = min(stored_cols, cols) * _CELL_SIZE
        ctypes.memmove(cells_ptr, raw, copy_bytes)
        # Zero-fill any extra columns if screen is now wider
        if cols > stored_cols:
            extra_offset = stored_cols * _CELL_SIZE
            extra_bytes = (cols - stored_cols) * _CELL_SIZE
            ctypes.memset(
                ctypes.cast(cells_ptr, c_void_p).value + extra_offset,
                0, extra_bytes)
        return 1

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

    # libvterm marks the second column of a wide (width-2) character by
    # setting chars[0] to (uint32_t)-1 in that cell (screen.c). The same
    # sentinel appears in scrollback lines, which are pushed through the
    # equivalent of vterm_screen_get_cell.
    CONTINUATION = 0xFFFFFFFF

    def render_row_segments(self, row: int, cols: int,
                            cursor_x: int = -1) -> list[tuple[str, tuple]]:
        """Render an entire row as run-length-encoded (text, style_tuple) pairs.

        Returns a list of (text, (fg, bg, attrs)) tuples where fg/bg are
        Rich color strings or None, and attrs is the raw bitfield.
        This avoids per-cell Style object creation in Python.

        Wide characters (CJK, emoji) emit only their glyph; the
        continuation cell libvterm places in the next column is skipped,
        so the summed *cell* width of the returned runs always equals
        `cols`. (Emitting a spacer for the continuation cell — the old
        behavior — shifted everything after a wide char right by one
        column per wide char.)

        Hot-path note: each cell is read via one vterm_screen_get_cell
        call plus a single struct.unpack_from over the ctypes struct's
        buffer — per-field ctypes attribute access is ~5x slower.
        """
        pos = self._pos
        cell = self._cell
        cell_ref = ctypes.byref(cell)
        screen = self._screen
        get_cell = _vterm_screen_get_cell
        parse = _CELL_PARSE
        color_cache = self._color_cache
        convert = self._convert_color_bytes
        continuation = self.CONTINUATION
        miss = _MISS

        segments: list[tuple[str, tuple]] = []
        run_chars: list[str] = []
        run_key: tuple = ()  # (fg, bg, attrs) or ("cursor",)

        pos.row = row
        x = 0
        while x < cols:
            pos.col = x
            get_cell(screen, pos, cell_ref)
            cp, width, attrs, fg_bytes, bg_bytes = parse(cell)

            if cp == continuation:
                # Second column of a wide char: the glyph emitted for the
                # previous cell already covers this column.
                x += 1
                continue
            # No codepoint below U+1100 is double-width; skipping the
            # width check for ASCII/Latin keeps the hot path lean.
            wide = cp >= 0x1100 and width == 2 and x + 1 < cols

            fg_color = color_cache.get(fg_bytes, miss)
            if fg_color is miss:
                fg_color = convert(fg_bytes)
            bg_color = color_cache.get(bg_bytes, miss)
            if bg_color is miss:
                bg_color = convert(bg_bytes)
            attrs &= 0xFF  # style bits (bold/underline/italic/…) live here

            # A cursor reported on the continuation column highlights the
            # wide char that owns it.
            if x == cursor_x or (wide and x + 1 == cursor_x):
                if self.cursor_shape == 1:
                    key = ("cursor", 1)  # block: reverse video
                else:
                    key = ("cursor_bar", fg_color, bg_color, attrs)  # bar: underline
            else:
                key = (fg_color, bg_color, attrs)

            # Get character
            ch = chr(cp) if 0 < cp <= 0x10FFFF else " "

            if key != run_key:
                if run_chars:
                    segments.append(("".join(run_chars), run_key))
                run_chars = [ch]
                run_key = key
            else:
                run_chars.append(ch)

            x += 2 if wide else 1

        if run_chars:
            segments.append(("".join(run_chars), run_key))
        return segments

    def _convert_color_bytes(self, b: bytes) -> str | None:
        """Convert raw (type, r, g, b) VTermColor bytes to a Rich color
        string (None = default fg/bg) and memoize in _color_cache."""
        t = b[0]
        if t & 0x06:  # DEFAULT_FG / DEFAULT_BG
            result = None
        elif t & 0x01:  # indexed: palette lookup via libvterm
            ctmp = self._ctmp
            ctmp.type = t
            ctmp.red = b[1]
            ctmp.green = b[2]
            ctmp.blue = b[3]
            _vterm_screen_convert_color_to_rgb(self._screen, ctypes.byref(ctmp))
            result = f"#{ctmp.red:02x}{ctmp.green:02x}{ctmp.blue:02x}"
        else:  # direct RGB
            result = f"#{b[1]:02x}{b[2]:02x}{b[3]:02x}"
        if len(self._color_cache) >= 4096:
            self._color_cache.clear()
        self._color_cache[b] = result
        return result

    @staticmethod
    def _color_to_str(color: VTermColor, ctmp: VTermColor,
                      screen) -> str | None:
        """Fast inline color conversion without method dispatch."""
        t = color.type
        if t & 0x02 or t & 0x04:
            return None
        if t & 0x01:
            ctmp.type = t
            ctmp.red = color.red
            ctmp.green = color.green
            ctmp.blue = color.blue
            _vterm_screen_convert_color_to_rgb(screen, ctypes.byref(ctmp))
            return f"#{ctmp.red:02x}{ctmp.green:02x}{ctmp.blue:02x}"
        return f"#{color.red:02x}{color.green:02x}{color.blue:02x}"

    def get_scrollback_cell(self, sb_index: int, col: int) -> VTermScreenCell | None:
        """Read a cell from a scrollback line. Returns the internal reusable
        struct (same as get_cell) or None if out of bounds."""
        if sb_index < 0 or sb_index >= len(self.scrollback):
            return None
        stored_cols, raw = self.scrollback[sb_index]
        if col >= stored_cols:
            return None
        # Point _cell at the right offset within the raw bytes
        offset = col * _CELL_SIZE
        ctypes.memmove(ctypes.byref(self._cell), raw[offset:offset + _CELL_SIZE], _CELL_SIZE)
        return self._cell

    def cell_char(self, cell: VTermScreenCell) -> str:
        cp = cell.chars[0]
        if cp == 0 or cp > 0x10FFFF:
            return " "
        parts = [chr(cp)]
        for i in range(1, 6):
            cp = cell.chars[i]
            if cp == 0 or cp > 0x10FFFF:
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
