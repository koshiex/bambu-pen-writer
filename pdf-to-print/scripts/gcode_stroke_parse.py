#!/usr/bin/env python3
"""Parse vpype `gwrite` pen strokes from G-code (travel G0 + pen-down Z + G1 XY … + pen-up Z).

Shared by svg_to_gcode (sanity checks) and validate_reading_order_gcode.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

_RE_FLOAT = r"[-+]?(?:\d+\.?\d*|\d*\.?\d+)(?:[eE][-+]?\d+)?"


def _extract_xy(line: str) -> tuple[float, float] | None:
    mx = re.search(r"(?<![A-Za-z_])X\s*(" + _RE_FLOAT + r")", line, re.I)
    my = re.search(r"(?<![A-Za-z_])Y\s*(" + _RE_FLOAT + r")", line, re.I)
    if mx and my:
        return float(mx.group(1)), float(my.group(1))
    return None


def _extract_z(line: str) -> float | None:
    m = re.search(r"(?<![A-Za-z_])Z\s*(" + _RE_FLOAT + r")", line, re.I)
    return float(m.group(1)) if m else None


Z_TOL = 0.05


def parse_stroke_polylines(path: Path, z_pen: float, z_up: float) -> list[np.ndarray]:
    """One complex ndarray per vpype line (first vertex from G0, then G1 XY until Z lift).

    Ignores lines that are not G0/G1 moves (M-code, comments already stripped).
    """
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    strokes: list[np.ndarray] = []
    pending_xy: tuple[float, float] | None = None
    current: list[complex] | None = None

    for raw in text:
        line = raw.strip().split(";")[0].strip()
        if not line:
            continue

        ul = line.upper()
        if not (ul.startswith("G0") or ul.startswith("G1")):
            continue

        xy = _extract_xy(line)

        if ul.startswith("G0") and xy is not None:
            pending_xy = xy
            continue

        if ul.startswith("G1"):
            zv = _extract_z(line)
            if xy is not None and current is not None:
                current.append(complex(xy[0], xy[1]))
                continue

            if xy is None and zv is not None:
                if abs(zv - z_pen) <= Z_TOL and pending_xy is not None:
                    x0, y0 = pending_xy
                    current = [complex(x0, y0)]
                    pending_xy = None
                elif abs(zv - z_up) <= Z_TOL and current is not None:
                    strokes.append(np.asarray(current, dtype=np.complex128))
                    current = None
            continue

    return strokes
