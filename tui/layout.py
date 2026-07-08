"""Rect + row/column splitting — the tui engine's whole layout system.

Split specs: int = fixed cells, float = weight over whatever remains after
fixed parts. Totals are always exact: fixed parts that don't fit are
truncated (in order), and leftover cells from weighted division go to the
earlier weighted parts. Weighted parts can be 0-sized.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


def _split(total: int, specs: tuple[int | float, ...]) -> list[int]:
    sizes = [0] * len(specs)
    remaining = max(0, total)
    for i, spec in enumerate(specs):
        if isinstance(spec, int):
            sizes[i] = max(0, min(spec, remaining))
            remaining -= sizes[i]
    weighted = [(i, float(spec)) for i, spec in enumerate(specs) if not isinstance(spec, int)]
    total_weight = sum(weight for _, weight in weighted)
    if weighted and total_weight > 0 and remaining > 0:
        alloc = [int(remaining * weight / total_weight) for _, weight in weighted]
        for j in range(remaining - sum(alloc)):
            alloc[j % len(alloc)] += 1
        for (i, _), cells in zip(weighted, alloc):
            sizes[i] = cells
    return sizes


def split_rows(rect: Rect, *specs: int | float) -> list[Rect]:
    heights = _split(rect.h, specs)
    rects = []
    y = rect.y
    for h in heights:
        rects.append(Rect(rect.x, y, rect.w, h))
        y += h
    return rects


def split_cols(rect: Rect, *specs: int | float) -> list[Rect]:
    widths = _split(rect.w, specs)
    rects = []
    x = rect.x
    for w in widths:
        rects.append(Rect(x, rect.y, w, rect.h))
        x += w
    return rects


def center(rect: Rect, w: int, h: int) -> Rect:
    w = max(0, min(w, rect.w))
    h = max(0, min(h, rect.h))
    return Rect(rect.x + (rect.w - w) // 2, rect.y + (rect.h - h) // 2, w, h)
