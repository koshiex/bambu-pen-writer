#!/usr/bin/env python3
"""Page ordering for merge_pages: sequential PDF order vs unfolded spread."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 24-page saddle-stitch notebook (12 sheets): 6 spreads × 4 pages.
# Per spread s (1..6): left-out, left-in, right-in, right-out = [2s-1, 2s, 25-2s, 26-2s]
SPREAD_ORDER_24: list[int] = [
    1, 2, 23, 24,
    3, 4, 21, 22,
    5, 6, 19, 20,
    7, 8, 17, 18,
    9, 10, 15, 16,
    11, 12, 13, 14,
]

# Sheet-swap points: first page of each loose-sheet group (start of each spread).
SPREAD_BOUNDARIES: frozenset[int] = frozenset({1, 3, 5, 7, 9, 11})

PAGE_NUM_RE = re.compile(r"page_(\d+)\.gcode$", re.IGNORECASE)


def is_spread_boundary(p: int) -> bool:
    return p in SPREAD_BOUNDARIES


def page_num_from_path(path: Path) -> int:
    m = PAGE_NUM_RE.search(path.name)
    if not m:
        raise ValueError(f"not a page gcode file: {path.name}")
    return int(m.group(1))


def spread_order(n_pages: int, start_page: int = 1) -> list[int]:
    if n_pages != 24:
        sys.exit(
            f"ERROR: spread page order supports 24 pages only (got {n_pages}). "
            "Use --page-order sequential for other PDFs."
        )
    if start_page <= 1:
        return list(SPREAD_ORDER_24)
    return [p for p in SPREAD_ORDER_24 if p >= start_page]


def page_paths(gcode_dir: Path, order: str = "sequential", start_page: int = 1) -> list[Path]:
    """Return page_*.gcode paths in merge order."""
    gcode_dir = Path(gcode_dir)
    found = sorted(gcode_dir.glob("page_*.gcode"))
    if not found:
        sys.exit(f"ERROR: no page_*.gcode in {gcode_dir}")

    by_num = {page_num_from_path(p): p for p in found}
    n = len(by_num)
    if n != len(found):
        sys.exit(f"ERROR: duplicate page numbers in {gcode_dir}")

    if not (1 <= start_page <= n):
        sys.exit(f"ERROR: --start-page {start_page} out of range 1..{n}")

    if order == "sequential":
        return [by_num[i] for i in range(start_page, n + 1)]

    if order == "spread":
        nums = spread_order(n, start_page)
        missing = [i for i in nums if i not in by_num]
        if missing:
            sys.exit(f"ERROR: spread order missing pages: {missing}")
        return [by_num[i] for i in nums]

    sys.exit(f"ERROR: unknown page order {order!r} (use sequential or spread)")


def _self_test() -> None:
    assert len(SPREAD_ORDER_24) == 24
    assert sorted(SPREAD_ORDER_24) == list(range(1, 25))
    assert SPREAD_ORDER_24[:4] == [1, 2, 23, 24]
    assert SPREAD_ORDER_24[-4:] == [11, 12, 13, 14]
    for s in range(1, 7):
        chunk = SPREAD_ORDER_24[(s - 1) * 4 : s * 4]
        assert chunk == [2 * s - 1, 2 * s, 25 - 2 * s, 26 - 2 * s], s

    assert spread_order(24, 1) == list(SPREAD_ORDER_24)
    assert spread_order(24, 2)[:3] == [2, 23, 24]
    assert spread_order(24, 13) == [23, 24, 21, 22, 19, 20, 17, 18, 15, 16, 13, 14]
    assert spread_order(24, 24) == [24]

    assert is_spread_boundary(1)
    assert is_spread_boundary(3)
    assert is_spread_boundary(7)
    assert is_spread_boundary(11)
    assert not is_spread_boundary(2)
    assert not is_spread_boundary(4)

    print("page_order: OK")


if __name__ == "__main__":
    _self_test()
