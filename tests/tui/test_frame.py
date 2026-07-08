"""Frame/Painter tests: diff minimality, pyte round-trip, width overflow,
markup memoization, vterm style-key semantics."""

import random
import re

import pyte
import pytest

import tui.frame as frame_mod
from tui.frame import Frame, Painter
from tui.layout import Rect

CUP_RE = re.compile(rb"\x1b\[(\d+);(\d+)H")

W, H = 24, 6


def make_frame(lines: list[str], width: int = W, height: int = H) -> Frame:
    f = Frame(width, height)
    for y, markup in enumerate(lines):
        f.write_markup(0, y, width, markup)
    return f


def cups(data: bytes) -> list[tuple[int, int]]:
    return [(int(r), int(c)) for r, c in CUP_RE.findall(data)]


# ── diff minimality ───────────────────────────────────────────────


def test_first_paint_emits_all_rows():
    painter = Painter()
    out = painter.paint(make_frame(["a", "b", "c"], height=3))
    assert cups(out) == [(1, 1), (2, 1), (3, 1)]


def test_single_row_change_emits_single_cup():
    painter = Painter()
    painter.paint(make_frame(["one", "two", "three", "four", "five", "six"]))
    out = painter.paint(make_frame(["one", "two", "CHANGED", "four", "five", "six"]))
    assert cups(out) == [(3, 1)]


def test_no_change_no_cursor_move_returns_empty_bytes():
    painter = Painter()
    painter.paint(make_frame(["one", "two"]))
    assert painter.paint(make_frame(["one", "two"])) == b""


def test_change_after_empty_paint_still_diffs():
    painter = Painter()
    painter.paint(make_frame(["one", "two"]))
    assert painter.paint(make_frame(["one", "two"])) == b""
    out = painter.paint(make_frame(["one", "CHANGED"]))
    assert cups(out) == [(2, 1)]


def test_cursor_move_alone_emits_paint():
    painter = Painter()
    f1 = make_frame(["hello"])
    f1.cursor = (1, 0)
    painter.paint(f1)
    f2 = make_frame(["hello"])
    f2.cursor = (2, 0)
    out = painter.paint(f2)
    assert cups(out) == [(1, 3)]  # only the cursor reposition
    assert b"\x1b[?25h" in out


def test_cursor_hide_alone_emits_paint():
    painter = Painter()
    f1 = make_frame(["hello"])
    f1.cursor = (1, 0)
    painter.paint(f1)
    f2 = make_frame(["hello"])
    out = painter.paint(f2)
    assert out != b"" and b"\x1b[?25l" in out
    assert painter.paint(make_frame(["hello"])) == b""  # now steady state


def test_style_only_change_repaints_row():
    painter = Painter()
    painter.paint(make_frame(["plain text"]))
    out = painter.paint(make_frame(["[bold red]plain text[/]"]))
    assert cups(out) == [(1, 1)]


def test_invalidate_forces_full_repaint():
    painter = Painter()
    f = make_frame(["one", "two", "three", "four", "five", "six"])
    painter.paint(f)
    painter.invalidate()
    assert len(cups(painter.paint(f))) == H


def test_cursor_positioning():
    painter = Painter()
    f = make_frame(["hello"])
    f.cursor = (4, 2)
    out = painter.paint(f)
    assert b"\x1b[?25h" in out
    assert (3, 5) in cups(out)  # 1-based row;col
    f2 = make_frame(["hello"])
    assert b"\x1b[?25l" in painter.paint(f2)


# ── pyte round-trip ───────────────────────────────────────────────


def pyte_screen():
    screen = pyte.Screen(W, H)
    return screen, pyte.ByteStream(screen)


def test_pyte_round_trip_basic():
    screen, stream = pyte_screen()
    painter = Painter()
    f = make_frame(
        [
            "hello world",
            "[bold red]styled[/] text",
            "[#ff8800 on #1a1b26]palette[/]",
            "",
            "tail",
        ]
    )
    stream.feed(painter.paint(f))
    assert screen.display == f.plain_lines()


def test_pyte_round_trip_partial_repaint():
    screen, stream = pyte_screen()
    painter = Painter()
    f1 = make_frame(["aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff"])
    stream.feed(painter.paint(f1))
    f2 = make_frame(["aaaa", "bbbb", "[green]XXXX[/]", "dddd", "eeee", "ffff"])
    stream.feed(painter.paint(f2))
    assert screen.display == f2.plain_lines()


def test_pyte_round_trip_cjk_and_emoji():
    screen, stream = pyte_screen()
    painter = Painter()
    f1 = make_frame(["漢字 wide", "a🎉b", "plain"])
    stream.feed(painter.paint(f1))
    assert screen.display == f1.plain_lines()
    f2 = make_frame(["漢字 wide", "[bold]🎉🎉🎉[/]", "plain"])
    stream.feed(painter.paint(f2))
    assert screen.display == f2.plain_lines()


def test_pyte_round_trip_property():
    """Seeded random frames painted incrementally must always match pyte."""
    rng = random.Random(1234)
    pool = ["a", "b", "Z", " ", "|", "→", "…", "漢", "字", "🎉", "/", "9"]
    styles = ["", "bold", "red", "#ff8800 on #24283b", "italic underline"]

    def random_line():
        cells = "".join(rng.choice(pool) for _ in range(rng.randint(0, W)))
        style = rng.choice(styles)
        return f"[{style}]{cells}[/]" if style else cells

    screen, stream = pyte_screen()
    painter = Painter()
    lines = [random_line() for _ in range(H)]
    for step in range(25):
        # mutate a couple of rows between paints
        for _ in range(rng.randint(0, 2)):
            lines[rng.randrange(H)] = random_line()
        f = make_frame(lines)
        stream.feed(painter.paint(f))
        assert screen.display == f.plain_lines(), f"mismatch at step {step}"


# ── width overflow / cropping ─────────────────────────────────────


def test_markup_wider_than_width_does_not_bleed():
    f = Frame(20, 2)
    f.write_markup(5, 0, 8, "[red]" + "X" * 30)
    assert f.plain_lines()[0] == " " * 5 + "X" * 8 + " " * 7
    assert f.plain_lines()[1] == " " * 20


def test_markup_shorter_than_width_pads():
    f = Frame(10, 1)
    f.write_markup(2, 0, 6, "ab")
    assert f.plain_lines()[0] == "  ab      "


def test_wide_char_cut_at_crop_boundary_becomes_space():
    f = Frame(10, 1)
    f.write_markup(0, 0, 3, "漢漢")  # second 漢 straddles the crop
    assert f.plain_lines()[0] == "漢 " + " " * 7


def test_write_beyond_frame_edge_is_clamped():
    f = Frame(10, 1)
    f.write_markup(8, 0, 50, "abcdef")
    assert f.plain_lines()[0] == " " * 8 + "ab"


def test_out_of_range_writes_ignored():
    f = Frame(10, 2)
    f.write_markup(0, 5, 10, "nope")
    f.write_markup(12, 0, 5, "nope")
    f.write_runs(0, -1, [("nope", (None, None, 0))])
    assert f.plain_lines() == [" " * 10, " " * 10]


def test_write_runs_overflow_cropped():
    f = Frame(10, 1)
    f.write_runs(4, 0, [("abcdefghij", (None, None, 0))])
    assert f.plain_lines()[0] == "    abcdef"


def test_splice_preserves_neighbours():
    f = Frame(12, 1)
    f.write_markup(0, 0, 12, "0123456789ab")
    f.write_markup(4, 0, 3, "[red]XYZ[/]")
    assert f.plain_lines()[0] == "0123XYZ789ab"


def test_newline_in_markup_takes_first_line():
    f = Frame(10, 2)
    f.write_markup(0, 0, 10, "top\nbottom")
    assert f.plain_lines()[0] == "top       "
    assert f.plain_lines()[1] == " " * 10


# ── markup memoization ────────────────────────────────────────────


def test_markup_cache_hit(monkeypatch):
    frame_mod._MARKUP_CACHE.clear()
    calls = {"n": 0}
    real = frame_mod._CONSOLE.render_str

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(frame_mod._CONSOLE, "render_str", counting)
    markup = "[bold]cache-me-test-unique[/]"
    Frame(30, 1).write_markup(0, 0, 30, markup)
    Frame(30, 1).write_markup(0, 0, 30, markup)
    assert calls["n"] == 1
    # different width is a different cache entry
    Frame(30, 1).write_markup(0, 0, 20, markup)
    assert calls["n"] == 2


def test_markup_cache_overflow_clears(monkeypatch):
    frame_mod._MARKUP_CACHE.clear()
    monkeypatch.setattr(frame_mod, "_MARKUP_CACHE_MAX", 4)
    f = Frame(10, 1)
    for i in range(6):
        f.write_markup(0, 0, 10, f"line-{i}")
    assert len(frame_mod._MARKUP_CACHE) <= 4


# ── vterm run styles (terminal.py _get_style semantics) ───────────


def styled_segment(f: Frame, y: int = 0):
    return f.rows[y][0]


def test_run_style_attr_bits():
    f = Frame(10, 1)
    # attrs: 0x01 bold | 0x08 italic | (attrs >> 1) & 0x03 underline |
    #        0x80 strike | 0x20 reverse
    f.write_runs(0, 0, [("x", ("#ff0000", "#000000", 0x01 | 0x02 | 0x08 | 0x80 | 0x20))])
    style = styled_segment(f).style
    assert style.bold and style.italic and style.underline and style.strike and style.reverse
    assert style.color.name == "#ff0000" and style.bgcolor.name == "#000000"


def test_run_style_plain():
    f = Frame(10, 1)
    f.write_runs(0, 0, [("x", ("#aabbcc", None, 0))])
    style = styled_segment(f).style
    assert not style.bold and not style.reverse and style.color.name == "#aabbcc"


def test_cursor_style_keys():
    f = Frame(10, 1)
    f.write_runs(0, 0, [("x", ("cursor", 1))])
    assert styled_segment(f).style.reverse
    f2 = Frame(10, 1)
    f2.write_runs(0, 0, [("x", ("cursor_bar", "#ffffff", None, 0x01))])
    style = f2.rows[0][0].style
    assert style.underline and style.bold


def test_run_text_is_verbatim_not_markup():
    f = Frame(20, 1)
    f.write_runs(0, 0, [("[red]literal[/]", (None, None, 0))])
    assert f.plain_lines()[0].startswith("[red]literal[/]")


def test_style_cache_reuses_objects():
    key = ("#123456", "#654321", 0x01)
    assert frame_mod._style_from_key(key) is frame_mod._style_from_key(key)


# ── fill ──────────────────────────────────────────────────────────


def test_fill_rect():
    f = make_frame(["aaaaaaaaaaaaaaaaaaaaaaaa"] * H)
    f.fill(Rect(2, 1, 4, 2), "on blue")
    lines = f.plain_lines()
    assert lines[1] == "aa    " + "a" * 18
    assert lines[2] == "aa    " + "a" * 18
    assert lines[0] == "a" * W


def test_fill_clamps_to_frame():
    f = Frame(10, 2)
    f.fill(Rect(6, 0, 100, 100), None)
    assert f.plain_lines() == [" " * 10, " " * 10]  # spaces over spaces


# ── painter row diffing with pyte over resize-ish invalidation ────


def test_paint_after_height_change_repaints_everything():
    painter = Painter()
    painter.paint(make_frame(["a", "b"], height=2))
    out = painter.paint(make_frame(["a", "b", "c"], height=3))
    assert len(cups(out)) == 3
