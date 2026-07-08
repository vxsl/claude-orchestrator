"""TermIO tests: OSC 52 sequence building (plain + tmux passthrough) and
idempotent restore. TermIO itself is real-TTY only, so everything else is
covered through FakeTermIO in the app harness tests."""

import base64

from tui.term import TermIO, osc52_sequence


# ── osc52_sequence ────────────────────────────────────────────────


def test_osc52_plain_exact():
    # base64("hi") == "aGk="
    assert osc52_sequence("hi", tmux=False) == b"\x1b]52;c;aGk=\x07"


def test_osc52_payload_round_trips_utf8():
    text = "héllo → 漢字 🎉"
    seq = osc52_sequence(text, tmux=False)
    assert seq.startswith(b"\x1b]52;c;") and seq.endswith(b"\x07")
    b64 = seq[len(b"\x1b]52;c;"):-1]
    assert base64.b64decode(b64).decode("utf-8") == text


def test_osc52_tmux_wrapped_exact():
    # tmux passthrough: ESC-P tmux; <inner with ESC doubled> ESC-backslash
    assert osc52_sequence("hi", tmux=True) == (
        b"\x1bPtmux;" + b"\x1b\x1b]52;c;aGk=\x07" + b"\x1b\\"
    )


def test_osc52_tmux_doubles_every_escape():
    inner = osc52_sequence("payload", tmux=False)
    wrapped = osc52_sequence("payload", tmux=True)
    body = wrapped[len(b"\x1bPtmux;"):-len(b"\x1b\\")]
    assert body == inner.replace(b"\x1b", b"\x1b\x1b")


# ── restore idempotency ───────────────────────────────────────────


def test_restore_safe_if_enter_never_ran():
    io = TermIO()
    io.restore()
    io.restore()  # must not raise, must not write mode sequences


def test_get_size_returns_positive_pair():
    cols, rows = TermIO().get_size()
    assert cols > 0 and rows > 0


def test_input_fileno_is_none_or_int():
    fd = TermIO().input_fileno()
    assert fd is None or isinstance(fd, int)
