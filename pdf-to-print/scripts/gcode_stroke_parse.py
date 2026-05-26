#!/usr/bin/env python3
"""Parse vpype `gwrite` pen strokes from G-code (travel G0 + pen-down Z + G1 XY … + pen-up Z).

Shared by svg_to_gcode (sanity checks), validate_reading_order_gcode.py, and gcode_experimental.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_RE_FLOAT = r"[-+]?(?:\d+\.?\d*|\d*\.?\d+)(?:[eE][-+]?\d+)?"

# Strokes appended by gcode_experimental.py start after this marker.
EXPERIMENTAL_STROKES_MARKER = "; === pdf-to-print experimental strokes ==="

Z_TOL = 0.05


def _extract_xy(line: str) -> tuple[float, float] | None:
    mx = re.search(r"(?<![A-Za-z_])X\s*(" + _RE_FLOAT + r")", line, re.I)
    my = re.search(r"(?<![A-Za-z_])Y\s*(" + _RE_FLOAT + r")", line, re.I)
    if mx and my:
        return float(mx.group(1)), float(my.group(1))
    return None


def _extract_z(line: str) -> float | None:
    m = re.search(r"(?<![A-Za-z_])Z\s*(" + _RE_FLOAT + r")", line, re.I)
    return float(m.group(1)) if m else None


def _extract_f(line: str) -> float | None:
    m = re.search(r"(?<![A-Za-z_])F\s*(" + _RE_FLOAT + r")", line, re.I)
    return float(m.group(1)) if m else None


def _is_g0(line: str) -> bool:
    return line.upper().startswith("G0")


def _is_g1(line: str) -> bool:
    return line.upper().startswith("G1")


def _strip_comment(line: str) -> str:
    return line.strip().split(";")[0].strip()


@dataclass
class StrokeBlock:
    """One vpype pen stroke: G0 travel, G1 Z down, G1 XY…, G1 Z up."""

    lines: list[str] = field(default_factory=list)
    travel_index: int | None = None  # G0 XY before pen-down
    drawing_indices: list[int] = field(default_factory=list)  # G1 XY while pen down

    def polyline_xy(self) -> np.ndarray:
        """Match parse_stroke_polylines: G0 target, then every drawing G1 XY."""
        pts: list[complex] = []
        if self.travel_index is not None:
            xy = _extract_xy(self.lines[self.travel_index])
            if xy is not None:
                pts.append(complex(xy[0], xy[1]))
        for idx in self.drawing_indices:
            xy = _extract_xy(self.lines[idx])
            if xy is not None:
                pts.append(complex(xy[0], xy[1]))
        return np.asarray(pts, dtype=np.complex128)

    def _line_index_for_vertex(self, vertex: int) -> int:
        n_draw = len(self.drawing_indices)
        n = (1 if self.travel_index is not None else 0) + n_draw
        if vertex < 0 or vertex >= n:
            raise IndexError(f"vertex {vertex} out of range (n={n})")
        if self.travel_index is not None and vertex == 0:
            return self.travel_index
        off = 1 if self.travel_index is not None else 0
        return self.drawing_indices[vertex - off]

    def set_vertex_xy(self, vertex: int, x: float, y: float) -> None:
        idx = self._line_index_for_vertex(vertex)
        line = self.lines[idx]
        if _is_g0(line):
            f = _extract_f(line)
            parts = ["G0", f"X{x:.3f}", f"Y{y:.3f}"]
            if f is not None:
                parts.append(f"F{int(round(f))}")
            self.lines[idx] = " ".join(parts)
        else:
            self.lines[idx] = _format_g1_line(
                line, x=x, y=y, z=_extract_z(line), f=_extract_f(line)
            )

    def set_drawing_z(self, vertex: int, z: float) -> None:
        """Z on drawing G1 only (vertex 0 = G0 has no Z)."""
        if self.travel_index is not None and vertex == 0:
            return
        idx = self._line_index_for_vertex(vertex)
        xy = _extract_xy(self.lines[idx])
        if xy is None:
            raise ValueError(f"line {idx} has no XY")
        self.lines[idx] = _format_g1_line(
            self.lines[idx], x=xy[0], y=xy[1], z=z, f=_extract_f(self.lines[idx])
        )

    def set_drawing_f(self, vertex: int, f: float) -> None:
        idx = self._line_index_for_vertex(vertex)
        line = self.lines[idx]
        xy = _extract_xy(line)
        if xy is None:
            raise ValueError(f"line {idx} has no XY")
        self.lines[idx] = _format_g1_line(
            line, x=xy[0], y=xy[1], z=_extract_z(line), f=f
        )


def _format_g1_line(
    template: str,
    *,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    f: float | None = None,
) -> str:
    """Rebuild G1 line preserving unspecified axes from template when possible."""
    if x is None:
        xy = _extract_xy(template)
        x = xy[0] if xy else 0.0
    if y is None:
        xy = _extract_xy(template)
        y = xy[1] if xy else 0.0
    parts = ["G1", f"X{x:.3f}", f"Y{y:.3f}"]
    if z is not None:
        parts.append(f"Z{z:.3f}")
    if f is not None:
        parts.append(f"F{int(round(f))}")
    return " ".join(parts)


def make_stroke_block_from_polyline(
    points: list[tuple[float, float, float | None, float | None]],
    *,
    z_pen: float,
    z_up: float,
    travel_f: int = 30000,
    z_feed: int = 1800,
) -> StrokeBlock:
    """Build vpype-style stroke from (x, y, optional z, optional f) vertices."""
    if len(points) < 1:
        raise ValueError("need at least one point")
    x0, y0 = points[0][0], points[0][1]
    lines = [
        f"G0 X{x0:.3f} Y{y0:.3f} F{travel_f}",
        f"G1 Z{z_pen:.3f} F{z_feed}",
    ]
    drawing_indices: list[int] = []
    for i, (x, y, z, f) in enumerate(points):
        zv = z if z is not None else z_pen
        parts = ["G1", f"X{x:.3f}", f"Y{y:.3f}", f"Z{zv:.3f}"]
        if f is not None:
            parts.append(f"F{int(round(f))}")
        lines.append(" ".join(parts))
        drawing_indices.append(len(lines) - 1)
    lines.append(f"G1 Z{z_up:.3f} F{z_feed}")
    return StrokeBlock(lines=lines, drawing_indices=drawing_indices)


def parse_stroke_blocks(
    path: Path,
    z_pen: float,
    z_up: float,
    *,
    stop_at_experimental_marker: bool = True,
) -> tuple[list[StrokeBlock], list[str]]:
    """Parse vpype strokes; return (blocks, trailing_lines after last stroke or from marker)."""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[StrokeBlock] = []
    trailing: list[str] = []
    pending_xy: tuple[float, float] | None = None
    current: StrokeBlock | None = None
    in_experimental = False

    for raw in text:
        if stop_at_experimental_marker and EXPERIMENTAL_STROKES_MARKER in raw:
            in_experimental = True
            trailing.append(raw)
            continue
        if in_experimental:
            trailing.append(raw)
            continue

        line = _strip_comment(raw)
        if not line:
            if current is not None:
                current.lines.append(raw)
            else:
                trailing.append(raw)
            continue

        ul = line.upper()
        if not (ul.startswith("G0") or ul.startswith("G1")):
            if current is not None:
                current.lines.append(raw)
            else:
                trailing.append(raw)
            continue

        if current is None:
            if ul.startswith("G0"):
                current = StrokeBlock()
                current.lines.append(raw if raw.strip() else line)
                current.travel_index = 0
                xy = _extract_xy(line)
                if xy is not None:
                    pending_xy = xy
            else:
                trailing.append(raw)
            continue

        xy = _extract_xy(line)

        if _is_g0(line) and xy is not None:
            pending_xy = xy
            current.lines.append(raw if raw.strip() else line)
            continue

        if _is_g1(line):
            zv = _extract_z(line)
            if xy is not None:
                current.lines.append(raw if raw.strip() else line)
                current.drawing_indices.append(len(current.lines) - 1)
                continue

            if zv is not None:
                if abs(zv - z_pen) <= Z_TOL and pending_xy is not None:
                    current.lines.append(raw if raw.strip() else line)
                    pending_xy = None
                elif abs(zv - z_up) <= Z_TOL:
                    current.lines.append(raw if raw.strip() else line)
                    blocks.append(current)
                    current = None
                else:
                    current.lines.append(raw if raw.strip() else line)
                continue

            current.lines.append(raw if raw.strip() else line)
            continue

        current.lines.append(raw if raw.strip() else line)

    if current is not None:
        blocks.append(current)

    return blocks, trailing


def emit_gcode(blocks: list[StrokeBlock], trailing: list[str] | None = None) -> str:
    """Serialize stroke blocks and optional trailing lines (e.g. experimental section)."""
    parts: list[str] = []
    for block in blocks:
        parts.extend(block.lines)
    if trailing:
        if parts and trailing and parts[-1] != "":
            parts.append("")
        parts.extend(trailing)
    return "\n".join(parts) + "\n"


def parse_stroke_polylines(path: Path, z_pen: float, z_up: float) -> list[np.ndarray]:
    """One complex ndarray per vpype line (first vertex from G0, then G1 XY until Z lift)."""
    blocks, _ = parse_stroke_blocks(path, z_pen, z_up, stop_at_experimental_marker=True)
    return [b.polyline_xy() for b in blocks if len(b.polyline_xy()) >= 1]
