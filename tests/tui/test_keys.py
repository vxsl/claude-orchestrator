"""InputDecoder tests: Textual-compatible names, incremental/partial feeds,
kitty CSI-u, SGR mouse, bracketed paste, and garbage resilience."""

import pytest

import config
from tui.keys import InputDecoder, KeyEvent, MouseEvent, PasteEvent


def decode(*chunks: bytes):
    """Feed chunks through a fresh decoder, flushing any pending escape."""
    dec = InputDecoder()
    events = []
    for chunk in chunks:
        events.extend(dec.feed(chunk))
    if dec.pending_escape():
        events.extend(dec.flush_escape())
    return events


def keys_of(events) -> list[str]:
    return [ev.key for ev in events]


# ── DEFAULT_KEYS cross-check ──────────────────────────────────────
#
# Every key name bound in config.DEFAULT_KEYS must be producible by the
# decoder from its canonical byte sequence.

CANONICAL_BYTES: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "escape": b"\x1b",
    "backspace": b"\x7f",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "shift+tab": b"\x1b[Z",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    "slash": b"/",
    "colon": b":",
    "question_mark": b"?",
}


def canonical_bytes(name: str) -> bytes:
    if name in CANONICAL_BYTES:
        return CANONICAL_BYTES[name]
    if len(name) == 1:
        return name.encode()
    if name.startswith("ctrl+") and len(name) == len("ctrl+") + 1:
        return bytes([ord(name[-1]) - 96])
    raise AssertionError(f"no canonical byte sequence known for key {name!r}")


def default_keys_names() -> list[str]:
    names = set()
    for keys, _desc, _show, _priority in config.DEFAULT_KEYS.values():
        names.update(k.strip() for k in keys.split(","))
    for keys, _desc in config.SESSION_KEYS.values():
        names.update(k.strip() for k in keys.split(","))
    return sorted(names)


@pytest.mark.parametrize("name", default_keys_names())
def test_default_keys_decodable(name):
    events = decode(canonical_bytes(name))
    assert keys_of(events) == [name]


# ── plain characters ──────────────────────────────────────────────


def test_lowercase_letter():
    (ev,) = decode(b"j")
    assert ev == KeyEvent("j", "j", b"j")


def test_uppercase_letter_stays_uppercase():
    (ev,) = decode(b"G")
    assert ev.key == "G" and ev.char == "G"


def test_digit():
    (ev,) = decode(b"2")
    assert ev.key == "2" and ev.char == "2"


def test_punctuation_aliases():
    assert keys_of(decode(b"/?: ,")) == ["slash", "question_mark", "colon", "space", "comma"]


def test_utf8_char():
    (ev,) = decode("é".encode())
    assert ev.key == "é" and ev.char == "é" and ev.raw == "é".encode()


def test_utf8_split_across_feeds():
    raw = "漢".encode()
    assert len(raw) == 3
    dec = InputDecoder()
    assert dec.feed(raw[:1]) == []
    assert dec.feed(raw[1:2]) == []
    (ev,) = dec.feed(raw[2:])
    assert ev.char == "漢" and ev.raw == raw


# ── control characters ────────────────────────────────────────────


def test_ctrl_letters():
    assert keys_of(decode(b"\x04\x15\x0e\x10")) == ["ctrl+d", "ctrl+u", "ctrl+n", "ctrl+p"]


def test_enter_tab_newline_nul():
    assert keys_of(decode(b"\r")) == ["enter"]
    assert keys_of(decode(b"\t")) == ["tab"]
    assert keys_of(decode(b"\n")) == ["ctrl+j"]
    assert keys_of(decode(b"\x00")) == ["ctrl+@"]


def test_backspace_preserves_original_byte():
    (del_ev,) = decode(b"\x7f")
    (bs_ev,) = decode(b"\x08")
    assert del_ev.key == "backspace" and del_ev.char == "\x7f" and del_ev.raw == b"\x7f"
    assert bs_ev.key == "backspace" and bs_ev.char == "\x08" and bs_ev.raw == b"\x08"


# ── escape vs alt ─────────────────────────────────────────────────


def test_lone_escape_needs_flush():
    dec = InputDecoder()
    assert dec.feed(b"\x1b") == []
    assert dec.pending_escape()
    (ev,) = dec.flush_escape()
    assert ev.key == "escape"
    assert not dec.pending_escape()


def test_esc_plus_char_in_one_feed_is_alt():
    (ev,) = decode(b"\x1bj")
    assert ev.key == "alt+j" and ev.raw == b"\x1bj"


def test_esc_plus_uppercase_is_alt_shift():
    (ev,) = decode(b"\x1bX")
    assert ev.key == "alt+shift+x"


def test_alt_backspace_and_alt_enter():
    assert keys_of(decode(b"\x1b\x7f")) == ["alt+backspace"]
    assert keys_of(decode(b"\x1b\r")) == ["alt+enter"]


def test_flush_escape_resolves_esc_plus_bracket_as_alt():
    dec = InputDecoder()
    assert dec.feed(b"\x1b[") == []
    assert dec.pending_escape()
    (ev,) = dec.flush_escape()
    assert ev.key == "alt+left_square_bracket"


def test_flush_escape_abandoned_csi_reissues_bytes():
    dec = InputDecoder()
    assert dec.feed(b"\x1b[1;5") == []
    events = dec.flush_escape()
    assert keys_of(events) == ["escape", "left_square_bracket", "1", "semicolon", "5"]


def test_double_escape():
    dec = InputDecoder()
    events = dec.feed(b"\x1b\x1b")
    assert keys_of(events) == ["escape"]
    assert dec.pending_escape()
    assert keys_of(dec.flush_escape()) == ["escape"]


def test_flush_escape_noop_when_nothing_pending():
    dec = InputDecoder()
    dec.feed(b"j")
    assert dec.flush_escape() == []


# ── CSI sequences ─────────────────────────────────────────────────


def test_modified_arrows():
    assert keys_of(decode(b"\x1b[1;5A")) == ["ctrl+up"]
    assert keys_of(decode(b"\x1b[1;2A")) == ["shift+up"]
    assert keys_of(decode(b"\x1b[1;6A")) == ["ctrl+shift+up"]
    assert keys_of(decode(b"\x1b[1;3B")) == ["alt+down"]
    assert keys_of(decode(b"\x1b[1;2H")) == ["shift+home"]
    assert keys_of(decode(b"\x1b[1;2F")) == ["shift+end"]


def test_modified_tilde_keys():
    # shift+pageup / shift+pagedown are load-bearing (TMUX_NAV_KEYS)
    assert keys_of(decode(b"\x1b[5;2~")) == ["shift+pageup"]
    assert keys_of(decode(b"\x1b[6;2~")) == ["shift+pagedown"]
    assert keys_of(decode(b"\x1b[3;5~")) == ["ctrl+delete"]


def test_rxvt_home_end_variants():
    assert keys_of(decode(b"\x1b[1~")) == ["home"]
    assert keys_of(decode(b"\x1b[4~")) == ["end"]
    assert keys_of(decode(b"\x1b[7~")) == ["home"]
    assert keys_of(decode(b"\x1b[8~")) == ["end"]


def test_csi_split_across_feeds():
    dec = InputDecoder()
    assert dec.feed(b"\x1b") == []
    assert dec.feed(b"[1;") == []
    assert dec.pending_escape()
    (ev,) = dec.feed(b"5A")
    assert ev.key == "ctrl+up" and ev.raw == b"\x1b[1;5A"


def test_ss3_application_arrows():
    assert keys_of(decode(b"\x1bOA\x1bOB\x1bOC\x1bOD")) == ["up", "down", "right", "left"]


def test_ss3_split_across_feeds():
    dec = InputDecoder()
    assert dec.feed(b"\x1bO") == []
    assert keys_of(dec.feed(b"P")) == ["f1"]


# ── kitty CSI-u ───────────────────────────────────────────────────


def test_kitty_orch_injected_sequences():
    # Injected by the orch launcher for pane switching — MUST decode.
    assert keys_of(decode(b"\x1b[106;6u")) == ["ctrl+shift+j"]
    assert keys_of(decode(b"\x1b[107;6u")) == ["ctrl+shift+k"]


def test_kitty_event_type_subparam():
    assert keys_of(decode(b"\x1b[106;6:1u")) == ["ctrl+shift+j"]
    # key release events are dropped
    assert decode(b"\x1b[106;6:3u") == []


def test_kitty_plain_and_modified():
    (ev,) = decode(b"\x1b[97u")
    assert ev.key == "a" and ev.char == "a"
    assert keys_of(decode(b"\x1b[106;2u")) == ["shift+j"]
    assert keys_of(decode(b"\x1b[13;5u")) == ["ctrl+enter"]
    assert keys_of(decode(b"\x1b[127u")) == ["backspace"]


def test_kitty_split_across_feeds():
    dec = InputDecoder()
    assert dec.feed(b"\x1b[106;") == []
    (ev,) = dec.feed(b"6u")
    assert ev.key == "ctrl+shift+j"


# ── SGR mouse ─────────────────────────────────────────────────────


def test_mouse_press_release():
    (press,) = decode(b"\x1b[<0;10;5M")
    assert isinstance(press, MouseEvent)
    assert (press.kind, press.button, press.x, press.y) == ("press", 0, 9, 4)
    (release,) = decode(b"\x1b[<0;10;5m")
    assert release.kind == "release"


def test_mouse_scroll():
    (up,) = decode(b"\x1b[<64;3;4M")
    (down,) = decode(b"\x1b[<65;3;4M")
    assert up.kind == "scroll_up" and (up.x, up.y) == (2, 3)
    assert down.kind == "scroll_down"


def test_mouse_modifiers_and_move():
    (ev,) = decode(b"\x1b[<16;1;1M")
    assert ev.kind == "press" and ev.ctrl and not ev.shift
    (move,) = decode(b"\x1b[<35;2;2M")
    assert move.kind == "move" and move.button == -1


# ── bracketed paste ───────────────────────────────────────────────


def test_paste_single_feed():
    events = decode(b"\x1b[200~hello world\x1b[201~")
    assert events == [PasteEvent("hello world")]


def test_paste_stateful_across_feeds():
    dec = InputDecoder()
    assert dec.feed(b"\x1b[200~hel") == []
    assert dec.feed(b"lo \x1b[201") == []  # partial terminator held
    events = dec.feed(b"~j")
    assert events == [PasteEvent("hello "), KeyEvent("j", "j", b"j")]


def test_paste_containing_escape_sequences():
    events = decode(b"\x1b[200~a\x1b[Ab\x1b[201~")
    assert events == [PasteEvent("a\x1b[Ab")]


def test_paste_utf8():
    events = decode("\x1b[200~漢字🎉\x1b[201~".encode())
    assert events == [PasteEvent("漢字🎉")]


# ── garbage resilience ────────────────────────────────────────────


def test_unknown_csi_dropped_then_decoding_continues():
    assert keys_of(decode(b"\x1b[999z" + b"j")) == ["j"]


def test_malformed_csi_final_dropped():
    assert keys_of(decode(b"\x1b[12\x01j")) == ["j"]


def test_unknown_ss3_dropped():
    assert keys_of(decode(b"\x1bOZj")) == ["j"]


def test_invalid_utf8_dropped():
    assert keys_of(decode(b"\xff\xfej")) == ["j"]


def test_truncated_utf8_resyncs():
    # lead byte for a 3-byte char followed by an ASCII byte: drop, resync
    assert keys_of(decode(b"\xe6j")) == ["j"]


def test_runaway_csi_params_dropped():
    data = b"\x1b[" + b"1;" * 40 + b"j"
    events = decode(data)
    # the runaway parameter string is dropped; trailing bytes still decode
    assert "j" in keys_of(events)


def test_mixed_stream():
    events = decode(b"j\x1b[Ak\x04\x1b[<64;1;1M\x1b[200~x\x1b[201~G")
    kinds = [
        ev.key if isinstance(ev, KeyEvent) else ev.kind if isinstance(ev, MouseEvent) else "paste"
        for ev in events
    ]
    assert kinds == ["j", "up", "k", "ctrl+d", "scroll_up", "paste", "G"]


def test_byte_at_a_time_feeding():
    data = b"\x1b[1;6Aj\x1b[106;6u"
    dec = InputDecoder()
    events = []
    for i in range(len(data)):
        events.extend(dec.feed(data[i : i + 1]))
    assert keys_of(events) == ["ctrl+shift+up", "j", "ctrl+shift+j"]


# ── key_name_for_char (public helper) ─────────────────────────────


def test_key_name_for_char_matches_decoder_names():
    from tui.keys import key_name_for_char

    assert key_name_for_char("j") == "j"
    assert key_name_for_char("G") == "G"
    assert key_name_for_char("?") == "question_mark"
    assert key_name_for_char("/") == "slash"
    assert key_name_for_char(" ") == "space"
    # agrees with what the decoder emits for the same char
    (ev,) = InputDecoder().feed(b"?")
    assert ev.key == key_name_for_char("?")
