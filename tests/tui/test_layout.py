"""Layout tests: exact-total invariants, truncation, degenerate sizes."""

import pytest

from tui.layout import Rect, center, split_cols, split_rows


def heights(rects):
    return [r.h for r in rects]


def widths(rects):
    return [r.w for r in rects]


# ── exact totals ──────────────────────────────────────────────────


@pytest.mark.parametrize("total", range(0, 51))
@pytest.mark.parametrize(
    "specs",
    [
        (1.0,),
        (1.0, 1.0),
        (1.0, 2.0, 1.0),
        (3, 1.0),
        (1, 1.0, 2),
        (2, 1.0, 1.0, 3),
        (5, 5, 5),
        (0.5, 0.25, 0.25),
    ],
)
def test_split_rows_totals_exact(total, specs):
    rect = Rect(0, 0, 80, total)
    rects = split_rows(rect, *specs)
    assert len(rects) == len(specs)
    assert sum(heights(rects)) <= total
    if any(isinstance(s, float) for s in specs):
        assert sum(heights(rects)) == total  # weighted parts absorb the remainder
    assert all(r.h >= 0 for r in rects)
    # parts tile the rect contiguously
    y = rect.y
    for r in rects:
        assert r.y == y and r.x == rect.x and r.w == rect.w
        y += r.h


def test_split_rows_fixed_and_weight():
    rects = split_rows(Rect(0, 0, 80, 24), 1, 1.0, 2)
    assert heights(rects) == [1, 21, 2]


def test_split_cols_weights_remainder_to_earlier():
    # 10 cells over three equal weights: 4/3/3 — earlier parts get the extras
    rects = split_cols(Rect(0, 0, 10, 5), 1.0, 1.0, 1.0)
    assert widths(rects) == [4, 3, 3]
    assert [r.x for r in rects] == [0, 4, 7]


def test_weight_proportions():
    rects = split_cols(Rect(0, 0, 30, 5), 2.0, 1.0)
    assert widths(rects) == [20, 10]


# ── clamping / degenerate sizes ───────────────────────────────────


def test_fixed_parts_truncate_in_order():
    rects = split_rows(Rect(0, 0, 80, 5), 3, 4, 2)
    assert heights(rects) == [3, 2, 0]


def test_weighted_parts_can_be_zero():
    rects = split_rows(Rect(0, 0, 80, 2), 1, 1, 1.0)
    assert heights(rects) == [1, 1, 0]


def test_zero_size_rect():
    rects = split_rows(Rect(0, 0, 80, 0), 2, 1.0)
    assert heights(rects) == [0, 0]
    rects = split_cols(Rect(0, 0, 0, 24), 1.0, 3)
    assert widths(rects) == [0, 0]


def test_negative_fixed_spec_treated_as_zero():
    rects = split_rows(Rect(0, 0, 80, 10), -2, 1.0)
    assert heights(rects) == [0, 10]


def test_single_weight_takes_all():
    rects = split_cols(Rect(2, 3, 40, 7), 1.0)
    assert rects == [Rect(2, 3, 40, 7)]


def test_offsets_respected():
    rects = split_rows(Rect(5, 10, 20, 6), 2, 1.0)
    assert rects[0] == Rect(5, 10, 20, 2)
    assert rects[1] == Rect(5, 12, 20, 4)


# ── center ────────────────────────────────────────────────────────


def test_center_basic():
    assert center(Rect(0, 0, 80, 24), 40, 10) == Rect(20, 7, 40, 10)


def test_center_odd_remainder_floors():
    assert center(Rect(0, 0, 11, 5), 4, 2) == Rect(3, 1, 4, 2)


def test_center_clamps_to_rect():
    assert center(Rect(2, 2, 10, 4), 100, 100) == Rect(2, 2, 10, 4)


def test_rect_properties():
    r = Rect(2, 3, 10, 4)
    assert r.right == 12 and r.bottom == 7
